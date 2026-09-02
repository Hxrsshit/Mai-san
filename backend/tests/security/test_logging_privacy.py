"""Stage 3D: what actually reaches the logs.

Source inspection alone is not enough -- a formatter or an `extra` dict can
leak what a `logger.info` call looks innocent about. These tests drive real
requests and real failures, then read the emitted records.

Both formatters are exercised: JSON puts every `extra` field into the payload,
and console renders them as `key=value`, so a field safe in one is not
automatically safe in the other.
"""

import ast
import json
import logging
import pathlib

import pytest
from httpx import AsyncClient

from app.core.errors import LLMRateLimitError, LLMTimeoutError
from app.core.logging import ConsoleFormatter, JsonFormatter

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

PRIVATE_MESSAGE = "My bank account number is 12345678 and my password is hunter2."
PRIVATE_MEMORY = "User's bank account number is 12345678."


def rendered(caplog) -> str:
    """Every captured record through both formatters, concatenated.

    Testing the formatted output rather than the record object is the point:
    a leak that only appears after formatting is still a leak.
    """
    json_formatter = JsonFormatter()
    console_formatter = ConsoleFormatter()
    parts = []
    for record in caplog.records:
        parts.append(json_formatter.format(record))
        parts.append(console_formatter.format(record))
    return "\n".join(parts)


@pytest.fixture
def logs(caplog):
    """Capture at DEBUG, the worst realistic case.

    `configure_logging` is applied first so the third-party logger pins are in
    force, exactly as they are at runtime. Capturing without it would test a
    configuration the application never actually runs in.
    """
    from app.core.logging import configure_logging

    configure_logging(level="DEBUG", log_format="json")
    caplog.set_level(logging.DEBUG)
    yield caplog
    configure_logging(level="INFO", log_format="console")


# --- Secrets ----------------------------------------------------------------


async def test_a_successful_turn_logs_no_credential(
    client: AsyncClient, conversation_id, fake_provider, settings, logs
) -> None:
    fake_provider.extraction_reply = '{"should_store_memory": false, "memories": []}'

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello Mai."},
    )

    output = rendered(logs)
    assert settings.GROQ_API_KEY not in output
    assert "test-key" not in output
    # `api_key` as a *substring* is no longer a useful proxy. Stage 4F-A
    # logs the credential identifier (`web_search.api_key`) and the setting
    # name (`SEARCH_API_KEY`) at DEBUG, deliberately: naming what is missing
    # is what makes a misconfiguration debuggable, and neither is a value.
    #
    # So the assertion moved from "the word never appears" to "only these
    # two known names appear" -- which is stricter about the thing that
    # matters and honest about the thing that does not.
    lowered = output.lower()
    known_names = ("web_search.api_key", "search_api_key")
    residue = lowered
    for name in known_names:
        residue = residue.replace(name, "")
    assert "api_key" not in residue
    assert "apikey" not in residue
    assert settings.DATABASE_URL not in output


@pytest.mark.parametrize(
    "error",
    [
        LLMTimeoutError("The model did not respond in time."),
        LLMRateLimitError("Rate limited."),
    ],
)
async def test_a_provider_failure_logs_no_credential(
    client: AsyncClient, conversation_id, fake_provider, settings, logs, error
) -> None:
    """The path most likely to serialise provider configuration by accident."""
    fake_provider.raise_error = error

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello Mai."},
    )
    # 429 for a rate limit, 504 for a timeout -- both are handled classifications.
    assert response.status_code >= 400

    output = rendered(logs)
    assert settings.GROQ_API_KEY not in output
    assert "test-key" not in output
    assert "gsk_" not in output


async def test_a_database_failure_logs_no_connection_string(
    client: AsyncClient, conversation_id, settings, logs, monkeypatch
) -> None:
    """The failure is induced at the driver boundary, so the real
    `_DB_ERRORS` handling path runs rather than being bypassed."""
    from sqlalchemy.ext.asyncio import AsyncSession

    # A DSN carrying a password, which is what a PostgreSQL deployment uses.
    # The SQLite test URL has no credentials and would prove nothing.
    dsn = "postgresql+asyncpg://mai:sup3r-s3cret-pw@db:5432/mai"

    async def boom(*args, **kwargs):
        raise OSError(f"could not connect to {dsn}")

    monkeypatch.setattr(AsyncSession, "flush", boom)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello Mai."},
    )
    assert response.status_code >= 400
    assert "sup3r-s3cret-pw" not in response.text

    # 33 call sites copy `str(exc)` into a structured field and 19 attach a
    # traceback. The driver's exception text embeds the DSN, so the password
    # reaches the log record -- and must not survive formatting.
    output = rendered(logs)
    assert "sup3r-s3cret-pw" not in output, (
        "the database password reached the rendered log output"
    )
    assert "mai:***@db" in output, "redaction did not run on this path"


# --- Private content --------------------------------------------------------


async def test_message_bodies_are_not_logged(
    client: AsyncClient, conversation_id, fake_provider, logs
) -> None:
    """Metadata about a turn, never the turn itself."""
    fake_provider.extraction_reply = '{"should_store_memory": false, "memories": []}'
    fake_provider.reply = "An assistant reply containing SENSITIVE-REPLY-TOKEN."

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": PRIVATE_MESSAGE},
    )

    output = rendered(logs)
    assert PRIVATE_MESSAGE not in output
    assert "hunter2" not in output
    assert "12345678" not in output
    assert "SENSITIVE-REPLY-TOKEN" not in output


async def test_memory_content_is_not_logged_when_stored(
    client: AsyncClient, conversation_id, fake_provider, logs
) -> None:
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": PRIVATE_MEMORY,
                    "memory_type": "semantic",
                    "importance_score": 9,
                    "confidence_score": 0.95,
                }
            ],
        }
    )

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "My bank account number is 12345678."},
    )
    assert (await client.get("/api/memories")).json()["total"] == 1

    output = rendered(logs)
    assert PRIVATE_MEMORY not in output
    assert "12345678" not in output


async def test_retrieved_knowledge_is_not_logged(
    client: AsyncClient, fake_provider, session_factory, logs
) -> None:
    from tests.test_retrieval_integration import seed_knowledge

    await seed_knowledge(session_factory)
    fake_provider.extraction_reply = '{"should_store_memory": false, "memories": []}'
    logs.clear()

    await client.post(
        f"/api/conversations/{(await client.post('/api/conversations', json={})).json()['id']}/messages",
        json={"content": "What database does Mai use?"},
    )

    output = rendered(logs)
    assert "User selected PostgreSQL for local storage in Mai." not in output
    assert "REFERENCE KNOWLEDGE" not in output


async def test_a_conversation_title_is_not_logged(
    client: AsyncClient, fake_provider, logs
) -> None:
    """Titles are auto-generated from the first user message."""
    fake_provider.extraction_reply = '{"should_store_memory": false, "memories": []}'
    secret_title = "Divorce settlement strategy notes"

    conversation = (
        await client.post("/api/conversations", json={"title": secret_title})
    ).json()["id"]
    await client.post(
        f"/api/conversations/{conversation}/messages", json={"content": "Hello."}
    )

    assert secret_title not in rendered(logs)


# --- Validation errors ------------------------------------------------------


async def test_a_validation_failure_does_not_log_the_rejected_content(
    client: AsyncClient, conversation_id, logs
) -> None:
    """Rejected input is still user input."""
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "X" * 40_000 + "REJECTED-SECRET-TOKEN"},
    )

    assert "REJECTED-SECRET-TOKEN" not in rendered(logs)


# --- Source-level policy ----------------------------------------------------


def test_no_print_statement_survives_in_application_code() -> None:
    """`print` bypasses the formatters, the level filter and the policy."""
    offenders = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert offenders == [], f"print() in application code: {offenders}"


def test_no_log_call_passes_message_or_memory_content_directly() -> None:
    """A structural check on the shape of `extra` dictionaries.

    Field *names* that would carry private text are banned outright, so a
    future `extra={"user_message": ...}` fails here rather than in a leak.
    """
    banned_keys = {
        "content", "user_message", "assistant_message", "message",
        "memory_content", "text", "prompt", "query_text", "title",
        "api_key", "token", "secret", "password", "database_url",
    }
    offenders = []

    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and key.value in banned_keys:
                        offenders.append(
                            f"{path.relative_to(APP)}:{node.lineno}:{key.value}"
                        )

    assert offenders == [], f"private-looking log fields: {offenders}"


def test_sqlalchemy_echo_is_off_by_default() -> None:
    """`DB_ECHO` writes every statement, including parameter values, to logs."""
    from app.core.config import Settings

    settings = Settings(_env_file=None, GROQ_API_KEY="x")
    assert settings.DB_ECHO is False


# --- Third-party logger containment (regression) ----------------------------
# Finding S-01: with LOG_LEVEL=DEBUG the database driver logged every statement
# together with its bound parameters -- the full text of every message, memory,
# title and entity name. `configure_logging` now pins those loggers.


@pytest.mark.parametrize(
    "name",
    [
        "aiosqlite",
        "asyncpg",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
        "sqlalchemy.dialects",
        "httpx",
        "httpcore",
    ],
)
def test_data_carrying_loggers_stay_pinned_at_debug(name) -> None:
    from app.core.logging import configure_logging

    try:
        configure_logging(level="DEBUG", log_format="json")
        level = logging.getLogger(name).getEffectiveLevel()
        assert level >= logging.WARNING, (
            f"{name} would emit row data at DEBUG"
        )
    finally:
        configure_logging(level="INFO", log_format="console")


def test_application_loggers_are_not_pinned() -> None:
    """The containment must not silence Mai's own diagnostics."""
    from app.core.logging import configure_logging

    try:
        configure_logging(level="DEBUG", log_format="json")
        assert (
            logging.getLogger("app.services.chat_service").getEffectiveLevel()
            == logging.DEBUG
        )
    finally:
        configure_logging(level="INFO", log_format="console")
