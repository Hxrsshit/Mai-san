"""Stage 7A: Telegram linking and update classification, through the real app.

The webhook has three outcomes: a private `/link <code>` redeems a code and
never reaches the chat path; a private message from the Admin's configured
chat is the one way to a turn; everything else is acknowledged and ignored.
A code, a Telegram id and message text never reach a log record.
"""

import ast
import itertools
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, update as sql_update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.routes.telegram as telegram_route
from app.api.routes.telegram import (
    FailureReplyLimiter,
    INVALID_LINK_REPLY,
    LINKED_REPLY,
    get_telegram_sender,
)
from app.database.metadata import Base
from app.identity.constants import (
    DEFAULT_ORGANISATION_ID,
    TELEGRAM_PROVIDER,
    MemberStatus,
    OrganisationStatus,
)
from app.identity.link_codes import code_sha256
from app.identity.models import LinkCode, Organisation, OrganisationMember, User, UserIdentity
from app.identity.service import MembershipService
from app.telegram.updates import LINK_COMMAND, UpdateKind, classify

BACKEND = Path(__file__).resolve().parents[1]
WEBHOOK_SOURCE = BACKEND / "app" / "api" / "routes" / "telegram.py"
UPDATES_SOURCE = BACKEND / "app" / "telegram" / "updates.py"

SECRET = "webhook-secret-7a-sentinel"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
WEBHOOK = "/api/telegram/webhook"

#: Distinctive ids, so a log search for them cannot match by accident.
ADMIN_CHAT = 5550001234
MEMBER_A = 5550007771
MEMBER_B = 5550007772
STRANGER = 5550007773
GROUP_CHAT = -1005550009999

PLAIN_TEXT = "sentinel-plain-text-7a"
TURN_REPLY = "Hello from Mai."


class RecordingSender:
    def __init__(self) -> None:
        self.messages: List[Tuple[int, str]] = []

    async def send_text(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def sensitive(caplog):
    """Values that must appear in no log record of the test: every code, every
    Telegram id and every text the test sends. Checked after the test."""
    caplog.set_level(logging.INFO)
    values: List[str] = []
    yield values
    records = caplog.get_records("setup") + caplog.get_records("call")
    blob = "\n".join(r.getMessage() + repr(r.__dict__) for r in records)
    for value in values:
        assert value not in blob, "a sensitive value reached a log record"
        assert value.lower() not in blob.lower(), "a sensitive value reached a log record"


@pytest_asyncio.fixture
async def telegram(client: AsyncClient, conversation_id, settings, sensitive):
    settings.TELEGRAM_BOT_TOKEN = "12345:telegram-token-7a-not-for-logs"
    settings.TELEGRAM_WEBHOOK_SECRET = SECRET
    settings.TELEGRAM_ALLOWED_CHAT_ID = str(ADMIN_CHAT)
    settings.TELEGRAM_CONVERSATION_ID = str(conversation_id)
    sensitive.extend(str(v) for v in (ADMIN_CHAT, MEMBER_A, MEMBER_B, STRANGER, GROUP_CHAT))
    sensitive.append(PLAIN_TEXT)
    sender = RecordingSender()
    app = client._transport.app  # in-process test transport
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    yield sender
    app.dependency_overrides.pop(get_telegram_sender, None)


_update_ids = itertools.count(70_000)


def message(
    sender_id: Optional[int],
    text: Optional[str],
    *,
    chat_id: Optional[int] = None,
    chat_type: Optional[str] = "private",
    is_bot: bool = False,
    update_id: Optional[int] = None,
    kind: str = "message",
    **fields,
) -> dict:
    chat = {"id": sender_id if chat_id is None else chat_id}
    if chat_type is not None:
        chat["type"] = chat_type
    body = {"message_id": 1, "chat": chat}
    if sender_id is not None:
        body["from"] = {"id": sender_id, "is_bot": is_bot, "first_name": "Sentinel"}
    if text is not None:
        body["text"] = text
    body.update(fields)
    return {"update_id": next(_update_ids) if update_id is None else update_id, kind: body}


async def post(client: AsyncClient, payload: dict):
    return await client.post(WEBHOOK, json=payload, headers=HEADERS)


IGNORED = {"ok": True, "ignored": True}


# --- Database helpers -------------------------------------------------------


async def invite(session_factory, sensitive, name: str = "Member") -> Tuple[object, str]:
    async with session_factory() as session:
        view, issued = await MembershipService(session).invite(DEFAULT_ORGANISATION_ID, name)
        await session.commit()
    sensitive.append(issued.code)
    return view.id, issued.code


async def code_consumed(session_factory, code: str) -> bool:
    async with session_factory() as session:
        consumed_at = (
            await session.execute(
                select(LinkCode.consumed_at).where(LinkCode.code_sha256 == code_sha256(code))
            )
        ).scalar_one()
    return consumed_at is not None


async def attached(session_factory, subject: int) -> bool:
    async with session_factory() as session:
        return await MembershipService(session).has_attached_identity(
            TELEGRAM_PROVIDER, str(subject)
        )


async def member_status(session_factory, member_id) -> str:
    async with session_factory() as session:
        return (
            await session.execute(
                select(OrganisationMember.status).where(OrganisationMember.id == member_id)
            )
        ).scalar_one()


async def row_counts(session_factory) -> dict:
    counts = {}
    async with session_factory() as session:
        for name in ("messages", "conversations", "memories"):
            table = Base.metadata.tables[name]
            counts[name] = (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one()
    return counts


# ============================================================================
# A. Groups, supergroups and channels are never a turn and never a link
# ============================================================================


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
@pytest.mark.parametrize("chat_id", [ADMIN_CHAT, GROUP_CHAT])
@pytest.mark.parametrize("as_link", [False, True])
async def test_group_updates_from_the_admin_are_ignored(
    client, telegram, fake_provider, session_factory, sensitive, chat_type, chat_id, as_link
) -> None:
    _, code = await invite(session_factory, sensitive)
    before = await row_counts(session_factory)
    text = f"/link {code}" if as_link else PLAIN_TEXT

    response = await post(client, message(ADMIN_CHAT, text, chat_id=chat_id, chat_type=chat_type))

    assert response.status_code == 200
    assert response.json() == IGNORED
    assert fake_provider.calls == []
    assert fake_provider.intent_calls == []
    assert telegram.messages == []
    assert await row_counts(session_factory) == before
    assert not await code_consumed(session_factory, code)
    assert not await attached(session_factory, ADMIN_CHAT)


async def test_a_channel_type_and_a_channel_post_are_ignored(
    client, telegram, fake_provider
) -> None:
    for payload in (
        message(ADMIN_CHAT, PLAIN_TEXT, chat_type="channel"),
        message(ADMIN_CHAT, PLAIN_TEXT, kind="channel_post"),
    ):
        response = await post(client, payload)
        assert response.status_code == 200
        assert response.json() == IGNORED
    assert fake_provider.calls == []
    assert telegram.messages == []


# ============================================================================
# B. Private updates that are not the Admin's first-hand message
# ============================================================================


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(message(STRANGER, PLAIN_TEXT), id="unlinked_sender"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, is_bot=True), id="is_bot"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, forward_origin={"type": "user"}),
                     id="forward_origin"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, forward_from={"id": STRANGER}),
                     id="forward_from"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, forward_from_chat={"id": GROUP_CHAT}),
                     id="forward_from_chat"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, forward_sender_name="Someone"),
                     id="forward_sender_name"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, forward_date=1700000000),
                     id="forward_date"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, via_bot={"id": STRANGER, "is_bot": True}),
                     id="via_bot"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, sender_chat={"id": GROUP_CHAT}),
                     id="sender_chat"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, kind="edited_message"), id="edited_message"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, kind="channel_post"), id="channel_post"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, chat_type=None), id="missing_type"),
        pytest.param(message(None, PLAIN_TEXT, chat_id=ADMIN_CHAT), id="missing_from"),
        pytest.param(message(STRANGER, PLAIN_TEXT, chat_id=ADMIN_CHAT), id="from_not_chat"),
        pytest.param(message(ADMIN_CHAT, PLAIN_TEXT, chat_id=STRANGER), id="chat_not_from"),
    ],
)
async def test_private_updates_that_are_not_the_admins_are_ignored(
    client, telegram, fake_provider, payload
) -> None:
    response = await post(client, payload)
    assert response.status_code == 200
    assert response.json() == IGNORED
    assert fake_provider.calls == []
    assert telegram.messages == []


async def test_a_linked_member_gets_no_turn(
    client, telegram, fake_provider, session_factory, sensitive
) -> None:
    _, code = await invite(session_factory, sensitive)
    assert (await post(client, message(MEMBER_A, f"/link {code}"))).json() == {"ok": True}
    assert telegram.messages == [(MEMBER_A, LINKED_REPLY)]

    response = await post(client, message(MEMBER_A, PLAIN_TEXT))
    assert response.status_code == 200
    assert response.json() == IGNORED
    assert fake_provider.calls == []
    assert telegram.messages == [(MEMBER_A, LINKED_REPLY)]


async def test_a_revoked_member_gets_no_turn(
    client, telegram, fake_provider, session_factory, sensitive
) -> None:
    member_id, code = await invite(session_factory, sensitive)
    await post(client, message(MEMBER_A, f"/link {code}"))
    async with session_factory() as session:
        await MembershipService(session).mark_revoked(DEFAULT_ORGANISATION_ID, member_id)
        await session.commit()
    telegram.messages.clear()

    response = await post(client, message(MEMBER_A, PLAIN_TEXT))
    assert response.status_code == 200
    assert response.json() == IGNORED
    assert fake_provider.calls == []
    assert telegram.messages == []


# ============================================================================
# C. /link
# ============================================================================


async def test_a_valid_code_links_and_activates_the_member(
    client, telegram, fake_provider, session_factory, sensitive
) -> None:
    member_id, code = await invite(session_factory, sensitive)
    assert await member_status(session_factory, member_id) == MemberStatus.INVITED.value
    before = await row_counts(session_factory)

    response = await post(client, message(MEMBER_A, f"/link {code}"))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert telegram.messages == [(MEMBER_A, LINKED_REPLY)]
    assert await attached(session_factory, MEMBER_A)
    assert await member_status(session_factory, member_id) == MemberStatus.ACTIVE.value
    assert await code_consumed(session_factory, code)
    async with session_factory() as session:
        principal = await MembershipService(session).principal_for_identity(
            TELEGRAM_PROVIDER, str(MEMBER_A)
        )
    assert principal is not None and principal.member_id == member_id
    # Never a turn: no model call, no message, conversation or memory row.
    assert fake_provider.calls == [] and fake_provider.intent_calls == []
    assert fake_provider.extraction_calls == []
    assert await row_counts(session_factory) == before


async def test_the_same_code_twice_gets_the_generic_reply(
    client, telegram, fake_provider, session_factory, sensitive
) -> None:
    _, code = await invite(session_factory, sensitive)
    await post(client, message(MEMBER_A, f"/link {code}"))
    again = await post(client, message(MEMBER_A, f"/link {code}"))
    other = await post(client, message(MEMBER_B, f"/link {code}"))

    assert again.json() == other.json() == {"ok": True}
    assert telegram.messages == [
        (MEMBER_A, LINKED_REPLY),
        (MEMBER_A, INVALID_LINK_REPLY),
        (MEMBER_B, INVALID_LINK_REPLY),
    ]
    assert not await attached(session_factory, MEMBER_B)
    assert fake_provider.calls == []


@pytest.mark.parametrize(
    "template",
    ["/LINK {code}", "/link@SomeBot {code}", "/link {lower}", "  /Link   {code}  ", "/link\t{code}",
     "\u200b/link {code}", "\u200e\ufeff /link {code}"],
)
async def test_command_variants_and_a_lower_case_code_link(
    client, telegram, fake_provider, session_factory, sensitive, template
) -> None:
    member_id, code = await invite(session_factory, sensitive)
    text = template.format(code=code, lower=code.lower())

    response = await post(client, message(MEMBER_A, text))

    assert response.json() == {"ok": True}
    assert telegram.messages == [(MEMBER_A, LINKED_REPLY)]
    assert await attached(session_factory, MEMBER_A)
    assert await member_status(session_factory, member_id) == MemberStatus.ACTIVE.value
    assert fake_provider.calls == []


@pytest.mark.parametrize(
    "template",
    [
        "/link",
        "/link   ",
        "/link bad-code",
        "/link {code} extra",
        "/link {code}\nthanks",
        "/link {code}{code}",
        "/link {short}",
    ],
)
async def test_malformed_link_commands_get_the_generic_reply_and_no_turn(
    client, telegram, fake_provider, session_factory, sensitive, template
) -> None:
    _, code = await invite(session_factory, sensitive)
    before = await row_counts(session_factory)

    response = await post(client, message(MEMBER_A, template.format(code=code, short=code[:-1])))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert telegram.messages == [(MEMBER_A, INVALID_LINK_REPLY)]
    assert fake_provider.calls == [] and fake_provider.intent_calls == []
    assert not await code_consumed(session_factory, code)
    assert not await attached(session_factory, MEMBER_A)
    assert await row_counts(session_factory) == before


async def test_a_valid_code_from_the_admins_own_chat_is_refused(
    client, telegram, fake_provider, session_factory, sensitive
) -> None:
    member_id, code = await invite(session_factory, sensitive)

    response = await post(client, message(ADMIN_CHAT, f"/link {code}"))

    assert response.json() == {"ok": True}
    assert telegram.messages == [(ADMIN_CHAT, INVALID_LINK_REPLY)]
    assert not await code_consumed(session_factory, code)
    assert not await attached(session_factory, ADMIN_CHAT)
    assert await member_status(session_factory, member_id) == MemberStatus.INVITED.value
    assert fake_provider.calls == []


NEAR_MISSES = [
    "/link{code}",
    "/LINK{code}",
    "/link:{code}",
    "/link-{code}",
    "/link\u200b {code}",
    "/li\u200bnk {code}",
    "/linked {code}",
    "/link@ {code}",
    "/link@bad-bot {code}",
    "\u200b/link {code}",
    "\u200e/link {code}",
]


@pytest.mark.parametrize("template", NEAR_MISSES)
async def test_a_near_miss_link_from_the_admins_chat_never_reaches_the_model(
    client, telegram, fake_provider, session_factory, sensitive, template
) -> None:
    """From the Admin's own chat, every message whose first visible token
    starts with `/link` is a link command: the generic reply, no turn, no
    message row, and the code is not consumed."""
    _, code = await invite(session_factory, sensitive)
    before = await row_counts(session_factory)

    response = await post(client, message(ADMIN_CHAT, template.format(code=code)))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert telegram.messages == [(ADMIN_CHAT, INVALID_LINK_REPLY)]
    assert fake_provider.calls == [] and fake_provider.intent_calls == []
    assert await row_counts(session_factory) == before
    assert not await code_consumed(session_factory, code)


@pytest.mark.parametrize(
    "template", [t for t in NEAR_MISSES if not t.startswith(("\u200b/", "\u200e/"))]
)
async def test_a_near_miss_link_is_never_redeemable(
    client, telegram, fake_provider, session_factory, sensitive, template
) -> None:
    """Only a token that fullmatches the command redeems. `/link<CODE>` must
    not be read as `/link` followed by a well-formed code."""
    member_id, code = await invite(session_factory, sensitive)

    response = await post(client, message(MEMBER_A, template.format(code=code)))

    assert response.json() == {"ok": True}
    assert telegram.messages == [(MEMBER_A, INVALID_LINK_REPLY)]
    assert not await code_consumed(session_factory, code)
    assert not await attached(session_factory, MEMBER_A)
    assert await member_status(session_factory, member_id) == MemberStatus.INVITED.value
    assert fake_provider.calls == []


@pytest.mark.parametrize(
    "extra",
    [{"forward_origin": {"type": "user"}}, {"via_bot": {"id": 1, "is_bot": True}},
     {"sender_chat": {"id": GROUP_CHAT}}],
)
async def test_a_second_hand_link_command_is_ignored_and_redeems_nothing(
    client, telegram, fake_provider, session_factory, sensitive, extra
) -> None:
    _, code = await invite(session_factory, sensitive)
    response = await post(client, message(MEMBER_A, f"/link {code}", **extra))
    assert response.json() == IGNORED
    assert telegram.messages == []
    assert not await code_consumed(session_factory, code)
    assert fake_provider.calls == []


# ============================================================================
# D. The Admin path
# ============================================================================


async def test_the_admins_private_chat_gets_one_turn(
    client, telegram, fake_provider, session_factory
) -> None:
    response = await post(client, message(ADMIN_CHAT, PLAIN_TEXT))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(fake_provider.calls) == 1
    assert fake_provider.last_call[-1].content == PLAIN_TEXT
    assert telegram.messages == [(ADMIN_CHAT, TURN_REPLY)]


async def test_a_member_identity_on_the_admins_chat_blocks_the_turn(
    client, telegram, fake_provider, session_factory
) -> None:
    async with session_factory() as session:
        person = User(display_name="Impostor")
        session.add(person)
        await session.flush()
        session.add(UserIdentity(user_id=person.id, provider=TELEGRAM_PROVIDER,
                                 subject=str(ADMIN_CHAT)))
        await session.commit()

    response = await post(client, message(ADMIN_CHAT, PLAIN_TEXT))

    assert response.json() == IGNORED
    assert fake_provider.calls == []
    assert telegram.messages == []


async def test_a_suspended_organisation_blocks_the_admin_turn(
    client, telegram, fake_provider, session_factory
) -> None:
    async with session_factory() as session:
        await session.execute(
            sql_update(Organisation)
            .where(Organisation.id == DEFAULT_ORGANISATION_ID)
            .values(status=OrganisationStatus.SUSPENDED.value)
        )
        await session.commit()

    response = await post(client, message(ADMIN_CHAT, PLAIN_TEXT))

    assert response.json() == IGNORED
    assert fake_provider.calls == []
    assert telegram.messages == []


def _database_error() -> OperationalError:
    """A driver error whose text quotes a Telegram id, as a real one would."""
    return OperationalError("SELECT", {"subject": str(ADMIN_CHAT)}, Exception("boom"))


def _assert_no_error_detail_logged(caplog, *values: str) -> None:
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.__dict__)
        for value in ("boom", "parameters", "SELECT", "ROLLBACK", "COMMIT") + values:
            assert value not in rendered, value
        assert record.exc_info is None


@pytest.mark.parametrize("method", ["has_attached_identity", "active_principal"])
@pytest.mark.parametrize("error", ["operational", "os"])
async def test_a_database_error_in_the_admin_check_is_never_a_turn(
    client, telegram, fake_provider, monkeypatch, caplog, method, error
) -> None:
    async def failing(self, *args, **kwargs):
        if error == "os":
            raise OSError("boom")
        raise _database_error()

    monkeypatch.setattr(MembershipService, method, failing)

    response = await post(client, message(ADMIN_CHAT, PLAIN_TEXT))

    assert response.status_code == 200
    assert response.json() == IGNORED
    assert fake_provider.calls == [] and fake_provider.intent_calls == []
    assert telegram.messages == []
    assert [getattr(r, "category", None) for r in caplog.records
            if r.getMessage() == "Telegram admin check failed"] == ["database"]
    _assert_no_error_detail_logged(caplog, str(ADMIN_CHAT))


async def test_a_failing_rollback_after_an_admin_check_error_is_quiet(
    client, telegram, fake_provider, monkeypatch, caplog
) -> None:
    armed = _fail_next_rollback(monkeypatch)

    async def failing(self, *args, **kwargs):
        armed["on"] = True
        raise _database_error()

    monkeypatch.setattr(MembershipService, "has_attached_identity", failing)

    response = await post(client, message(ADMIN_CHAT, PLAIN_TEXT))

    assert response.status_code == 200
    assert response.json() == IGNORED
    assert armed["fired"] == 1
    assert fake_provider.calls == []
    assert telegram.messages == []
    _assert_no_error_detail_logged(caplog, str(ADMIN_CHAT))


@pytest.mark.parametrize("path", ["link_database", "link_unexpected", "admin_check"])
async def test_the_quiet_rollback_runs_while_no_exception_is_being_handled(
    client, telegram, fake_provider, session_factory, sensitive, monkeypatch, path
) -> None:
    """The rollback after a failure runs outside any `except`, so nothing
    logged during it (a pool reset failure, say) can carry the original,
    parameter-bearing error as its `__context__`."""
    real_quiet = telegram_route._rollback_quietly
    seen = []

    async def recording(session):
        seen.append(sys.exc_info())
        await real_quiet(session)

    monkeypatch.setattr(telegram_route, "_rollback_quietly", recording)

    async def failing(self, *args, **kwargs):
        if path == "link_unexpected":
            raise RuntimeError("boom")
        raise _database_error()

    if path == "admin_check":
        monkeypatch.setattr(MembershipService, "has_attached_identity", failing)
        payload = message(ADMIN_CHAT, PLAIN_TEXT)
    else:
        monkeypatch.setattr(MembershipService, "redeem_link_code", failing)
        _, code = await invite(session_factory, sensitive)
        payload = message(MEMBER_A, f"/link {code}")

    response = await post(client, payload)

    assert response.status_code == 200
    assert seen == [(None, None, None)]
    assert fake_provider.calls == []


async def test_an_unconfigured_or_invalid_admin_chat_never_takes_a_turn(
    client, telegram, fake_provider, settings
) -> None:
    settings.TELEGRAM_ALLOWED_CHAT_ID = str(-ADMIN_CHAT)
    response = await post(client, message(ADMIN_CHAT, PLAIN_TEXT))
    assert response.json() == IGNORED
    assert fake_provider.calls == []


# ============================================================================
# E. Robustness
# ============================================================================


async def test_a_duplicate_update_is_neither_redeemed_nor_answered_twice(
    client, telegram, session_factory, sensitive
) -> None:
    _, code = await invite(session_factory, sensitive)
    payload = message(MEMBER_A, f"/link {code}")

    first = await post(client, payload)
    second = await post(client, payload)

    assert first.json() == {"ok": True}
    assert second.status_code == 200
    assert second.json() == {"ok": True, "duplicate": True}
    assert telegram.messages == [(MEMBER_A, LINKED_REPLY)]


async def test_a_database_error_during_linking_is_generic_and_unlogged(
    client, telegram, fake_provider, session_factory, sensitive, monkeypatch, caplog
) -> None:
    _, code = await invite(session_factory, sensitive)

    async def failing(self, code, provider, subject, now=None):
        raise OperationalError("SELECT", {"subject": str(MEMBER_A)}, Exception("boom"))

    monkeypatch.setattr(MembershipService, "redeem_link_code", failing)

    response = await post(client, message(MEMBER_A, f"/link {code}"))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert telegram.messages == [(MEMBER_A, INVALID_LINK_REPLY)]
    assert fake_provider.calls == []
    assert [getattr(r, "category", None) for r in caplog.records
            if r.getMessage() == "Telegram link failed"] == ["database"]
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.__dict__)
        assert str(MEMBER_A) not in rendered
        assert "boom" not in rendered
        assert record.exc_info is None
    assert not await code_consumed(session_factory, code)


async def test_an_unexpected_error_after_redemption_commits_nothing(
    client, telegram, fake_provider, session_factory, sensitive, monkeypatch, caplog
) -> None:
    """The real redemption runs (identity flushed, code consumed, member
    activated), then something that is not a database error escapes. None of
    it may become durable."""
    member_id, code = await invite(session_factory, sensitive)
    real = MembershipService.redeem_link_code

    async def redeem_then_fail(self, code, provider, subject, now=None):
        await real(self, code, provider, subject, now)
        raise RuntimeError("code_race")

    monkeypatch.setattr(MembershipService, "redeem_link_code", redeem_then_fail)
    response = await post(client, message(MEMBER_A, f"/link {code}"))

    assert response.status_code == 200
    assert telegram.messages == [(MEMBER_A, INVALID_LINK_REPLY)]
    assert not await code_consumed(session_factory, code)
    assert not await attached(session_factory, MEMBER_A)
    assert await member_status(session_factory, member_id) == MemberStatus.INVITED.value
    for record in caplog.records:
        assert record.exc_info is None or record.levelno < logging.WARNING
    assert [getattr(r, "category", None) for r in caplog.records
            if r.getMessage() == "Telegram link failed"] == ["unexpected"]
    assert [getattr(r, "error_class", None) for r in caplog.records
            if r.getMessage() == "Telegram link failed"] == ["RuntimeError"]

    # Nothing is stuck: the same code still links once the fault is gone.
    monkeypatch.setattr(MembershipService, "redeem_link_code", real)
    await post(client, message(MEMBER_A, f"/link {code}"))
    assert telegram.messages[-1] == (MEMBER_A, LINKED_REPLY)
    assert await attached(session_factory, MEMBER_A)


def _fail_next_rollback(monkeypatch) -> dict:
    """Make the next `AsyncSession.rollback` after arming raise a driver error
    that quotes a Telegram id, once. Every other rollback is the real one."""
    real = AsyncSession.rollback
    state = {"on": False, "fired": 0}

    async def rollback(self):
        if state["on"]:
            state["on"] = False
            state["fired"] += 1
            raise OperationalError("ROLLBACK", {"subject": str(MEMBER_A)}, Exception("boom"))
        return await real(self)

    monkeypatch.setattr(AsyncSession, "rollback", rollback)
    return state


def _fail_next_commit(monkeypatch) -> dict:
    real = AsyncSession.commit
    state = {"on": False, "fired": 0}

    async def commit(self):
        if state["on"]:
            state["on"] = False
            state["fired"] += 1
            raise OperationalError("COMMIT", {"subject": str(MEMBER_A)}, Exception("boom"))
        return await real(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    return state


@pytest.mark.parametrize("redeem_first", [False, True])
async def test_a_failing_rollback_after_a_link_error_is_quiet_and_commits_nothing(
    client, telegram, fake_provider, session_factory, sensitive, monkeypatch, caplog, redeem_first
) -> None:
    """Redemption fails with a database error and then the rollback fails too.
    Still 200 and the generic reply; no parameters in any log record. With
    `redeem_first`, the real redemption has already flushed the identity and
    consumed the code: none of it may be committed afterwards."""
    member_id, code = await invite(session_factory, sensitive)
    real = MembershipService.redeem_link_code
    armed = _fail_next_rollback(monkeypatch)

    async def failing(self, code, provider, subject, now=None):
        if redeem_first:
            assert await real(self, code, provider, subject, now) == "linked"
        armed["on"] = True
        raise OperationalError("SELECT", {"subject": str(MEMBER_A)}, Exception("boom"))

    monkeypatch.setattr(MembershipService, "redeem_link_code", failing)

    response = await post(client, message(MEMBER_A, f"/link {code}"))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert armed["fired"] == 1
    assert telegram.messages == [(MEMBER_A, INVALID_LINK_REPLY)]
    assert fake_provider.calls == []
    _assert_no_error_detail_logged(caplog, str(MEMBER_A))
    assert not await code_consumed(session_factory, code)
    assert not await attached(session_factory, MEMBER_A)
    assert await member_status(session_factory, member_id) == MemberStatus.INVITED.value


async def test_a_failing_commit_of_a_valid_link_commits_nothing(
    client, telegram, fake_provider, session_factory, sensitive, monkeypatch, caplog
) -> None:
    member_id, code = await invite(session_factory, sensitive)
    real = MembershipService.redeem_link_code
    armed = _fail_next_commit(monkeypatch)

    async def redeem_then_arm(self, code, provider, subject, now=None):
        result = await real(self, code, provider, subject, now)
        armed["on"] = True  # the webhook's own commit is next
        return result

    monkeypatch.setattr(MembershipService, "redeem_link_code", redeem_then_arm)

    response = await post(client, message(MEMBER_A, f"/link {code}"))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert armed["fired"] == 1
    assert telegram.messages == [(MEMBER_A, INVALID_LINK_REPLY)]
    assert fake_provider.calls == []
    assert [getattr(r, "category", None) for r in caplog.records
            if r.getMessage() == "Telegram link failed"] == ["database"]
    _assert_no_error_detail_logged(caplog, str(MEMBER_A))
    assert not await code_consumed(session_factory, code)
    assert not await attached(session_factory, MEMBER_A)
    assert await member_status(session_factory, member_id) == MemberStatus.INVITED.value


async def test_failure_replies_are_capped_per_window(
    client, telegram, fake_provider, session_factory, sensitive
) -> None:
    for index in range(25):
        response = await post(client, message(STRANGER, f"/link bad-code-{index}"))
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    assert telegram.messages == [(STRANGER, INVALID_LINK_REPLY)] * 20

    # A success is never suppressed by the cap.
    _, code = await invite(session_factory, sensitive)
    await post(client, message(MEMBER_A, f"/link {code}"))
    assert telegram.messages[-1] == (MEMBER_A, LINKED_REPLY)
    assert fake_provider.calls == []


def test_the_failure_reply_window_slides() -> None:
    now = [100.0]
    limiter = FailureReplyLimiter(limit=2, window_seconds=60.0, clock=lambda: now[0])
    assert limiter.allow() and limiter.allow()
    assert not limiter.allow()
    now[0] = 159.9
    assert not limiter.allow()
    now[0] = 160.0  # both earlier replies have left the window
    assert limiter.allow() and limiter.allow()
    assert not limiter.allow()


# ============================================================================
# F. The pure classifier
# ============================================================================


def test_classify_extracts_the_code_text_and_nothing_else() -> None:
    result = classify(message(MEMBER_A, "  /link@Mai_bot   ABCD  ", update_id=5))
    assert result.kind is UpdateKind.LINK_COMMAND
    assert (result.update_id, result.chat_id, result.sender_id) == (5, MEMBER_A, MEMBER_A)
    assert result.code_text == "ABCD" and result.text is None

    bare = classify(message(MEMBER_A, "/link"))
    assert bare.kind is UpdateKind.LINK_COMMAND and bare.code_text == ""

    text = classify(message(MEMBER_A, "please /link ABCD"))
    assert text.kind is UpdateKind.PRIVATE_TEXT
    assert text.text == "please /link ABCD" and text.code_text is None

    hidden = classify(message(MEMBER_A, "​‎ /LINK ABCD"))
    assert hidden.kind is UpdateKind.LINK_COMMAND and hidden.code_text == "ABCD"


@pytest.mark.parametrize("text", ["/linkABCD", "/link:ABCD", "/LINK-x ABCD", "/link​ ABCD",
                                  "​/li​nkABCD"])
def test_a_near_miss_is_a_link_command_whose_code_text_is_never_a_code(text) -> None:
    result = classify(message(MEMBER_A, text))
    assert result.kind is UpdateKind.LINK_COMMAND
    assert result.code_text is not None and result.code_text.startswith("/")
    assert result.text is None


@pytest.mark.parametrize(
    "payload,reason",
    [
        (message(MEMBER_A, "/link ABCD", chat_type="group"), "group"),
        (message(MEMBER_A, "/link ABCD", chat_type="supergroup"), "group"),
        (message(MEMBER_A, "/link ABCD", chat_type="channel"), "group"),
        (message(MEMBER_A, "/link ABCD", chat_type=None), "not_private"),
        (message(MEMBER_A, "/link ABCD", chat_type="Private"), "not_private"),
        (message(MEMBER_A, "/link ABCD", is_bot=True), None),
        (message(MEMBER_A, "/link ABCD", forward_date=1), None),
        (message(MEMBER_A, "/link ABCD", kind="edited_message"), None),
        (message(MEMBER_A, ""), None),
        ({"update_id": 1, "message": {"chat": {"id": True, "type": "private"},
                                      "from": {"id": True, "is_bot": False}, "text": "x"}}, None),
        ({"update_id": 1, "message": {"chat": {"id": "5", "type": "private"},
                                      "from": {"id": "5", "is_bot": False}, "text": "x"}}, None),
        ({"update_id": 1, "message": {"chat": {"id": 0, "type": "private"},
                                      "from": {"id": 0, "is_bot": False}, "text": "x"}}, None),
        ({"update_id": 1, "message": {"chat": {"id": 5, "type": "private"},
                                      "from": {"id": 5}, "text": "x"}}, None),
        ([], None),
    ],
)
def test_classify_ignores_everything_not_allowed(payload, reason) -> None:
    result = classify(payload)
    assert result.kind is UpdateKind.IGNORED
    assert result.text is None and result.code_text is None
    if reason is not None:
        assert result.reason == reason


def test_the_link_command_pattern_is_anchored_by_fullmatch() -> None:
    for token in ("/link", "/LINK", "/Link@SomeBot", "/link@a_b_9"):
        assert LINK_COMMAND.fullmatch(token)
    for token in ("/linked", "/link@", "/link@bad-bot", "link", "//link", "/link@" + "a" * 65):
        assert not LINK_COMMAND.fullmatch(token)


# ============================================================================
# G. Structure
# ============================================================================


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imported_modules(tree: ast.Module) -> List[Tuple[str, int]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def test_the_classifier_imports_only_the_standard_library() -> None:
    imported = _imported_modules(_parse(UPDATES_SOURCE))
    assert all(level == 0 for _, level in imported)
    assert {name for name, _ in imported} == {
        "re", "unicodedata", "dataclasses", "enum", "typing",
    }


def test_the_webhook_imports_no_organisation_task_or_authorization_module() -> None:
    for name, level in _imported_modules(_parse(WEBHOOK_SOURCE)):
        assert level == 0
        for prohibited in ("app.organisation", "app.tasks", "app.authorization"):
            assert name != prohibited and not name.startswith(prohibited + "."), name


def _parents(tree: ast.Module) -> dict:
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _calls(tree: ast.Module, name: str) -> list:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_ = node.func
            called = func_.id if isinstance(func_, ast.Name) else getattr(func_, "attr", None)
            if called == name:
                out.append(node)
    return out


def _enclosing(node, parents, kind):
    current = parents.get(node)
    while current is not None and not isinstance(current, kind):
        current = parents.get(current)
    return current


def test_the_turn_is_reached_only_from_the_private_text_branch() -> None:
    tree = _parse(WEBHOOK_SOURCE)
    parents = _parents(tree)

    turns = _calls(tree, "send_conversation_turn")
    assert len(turns) == 1
    helper = _enclosing(turns[0], parents, ast.AsyncFunctionDef)
    assert helper is not None and helper.name == "_admin_turn"

    uses = _calls(tree, "_admin_turn")
    assert len(uses) == 1
    use = uses[0]
    assert _enclosing(use, parents, ast.AsyncFunctionDef).name == "telegram_webhook"
    branch = _enclosing(use, parents, ast.If)
    assert ast.unparse(branch.test) == "classified.kind is UpdateKind.PRIVATE_TEXT"
    # In the branch's body, not its `else`.
    node = use
    while parents[node] is not branch:
        node = parents[node]
    assert node in branch.body

    # `_admin_turn` and `send_conversation_turn` are never reachable from `_link`.
    link = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_link")
    names = {n.id for n in ast.walk(link) if isinstance(n, ast.Name)}
    assert not names & {"send_conversation_turn", "_admin_turn", "MessageCreate"}


def test_redemption_and_the_admin_check_never_run_inside_the_exception_logging_block() -> None:
    tree = _parse(WEBHOOK_SOURCE)
    assert len(_calls(tree, "exception")) == 1
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        logs_exception = any(_calls(ast.Module(body=h.body, type_ignores=[]), "exception")
                             for h in node.handlers)
        if not logs_exception:
            continue
        guarded = ast.Module(body=node.body, type_ignores=[])
        for name in ("redeem_link_code", "has_attached_identity", "active_principal"):
            assert not _calls(guarded, name), name
    # No exception detail is attached to any other log record in the webhook.
    for call in ast.walk(tree):
        if isinstance(call, ast.Call):
            assert not any(k.arg == "exc_info" for k in call.keywords)
