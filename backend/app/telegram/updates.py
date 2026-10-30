"""Stage 7A: classify one raw Telegram update. Pure: standard library only.

No database, settings, logging or I/O. The webhook authenticates the request
and validates its minimal shape first; this module then decides, from the
payload alone, which of three things the update is:

- LINK_COMMAND: a private, first-hand message from a human sender whose
  first token starts with `/link`, in any case;
- PRIVATE_TEXT: any other private, first-hand text from a human sender;
- IGNORED: everything else.

Before tokenising, leading whitespace and leading format characters (Unicode
category Cf, for example U+200B and U+200E) are removed, and Cf characters
are disregarded when the first token is compared with `/link`. Only those two
classes are handled: other characters that merely look invisible are not, and
a message they disguise is ordinary text. Only a first
token that fullmatches `LINK_COMMAND` is redeemable: its `code_text` is the
rest of the message. Any other `/link...` token (`/linkABC`, `/link:ABC`) is
still a LINK_COMMAND, but its `code_text` is the whole message, which starts
with `/` -- a symbol no code contains -- so redemption refuses it as
malformed and the sender gets the generic reply. Text in which `/link` is not
the first token is ordinary text.

It is an allow-list. Only `update["message"]` is considered, only a private
chat whose id is the sender's own id, only a sender that is not a bot, and
never a forward or a message sent through a bot or on behalf of a chat.
Anything not explicitly allowed is IGNORED, and a `/link` message that fails
any rule is IGNORED too, never PRIVATE_TEXT, so a link command can never fall
through to the chat path. Whether a PRIVATE_TEXT sender is allowed a turn is the
webhook's decision, not this module's.

Reasons are short codes for logs. Nothing here returns or records anything a
log may not carry; the caller decides what to log, and logs the reason only.
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional


class UpdateKind(str, Enum):
    LINK_COMMAND = "link_command"
    PRIVATE_TEXT = "private_text"
    IGNORED = "ignored"


@dataclass(frozen=True)
class ClassifiedUpdate:
    kind: UpdateKind
    update_id: int
    chat_id: Optional[int]
    sender_id: Optional[int]
    text: Optional[str]        # PRIVATE_TEXT only
    code_text: Optional[str]   # LINK_COMMAND only: the text after the first token, stripped ("" if none)
    reason: str                # a short reason code for logs


#: Used with `fullmatch` on the first whitespace-separated token of the text.
#: `/link`, in any case, optionally addressed to a bot (`/link@SomeBot`).
LINK_COMMAND = re.compile(r"/link(?:@[A-Za-z0-9_]{1,64})?", re.IGNORECASE)

#: Fields that mean the text is not the sender's own, first-hand message: a
#: forward, a message sent through an inline bot, or one sent on behalf of a
#: chat or channel. Their presence alone is enough to ignore the update.
_SECOND_HAND_FIELDS = (
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "forward_date",
    "via_bot",
    "sender_chat",
)

#: Chat types that are shared by several people.
_GROUP_TYPES = frozenset({"group", "supergroup", "channel"})


#: The prefix that makes a first token a link command, compared case-insensitively.
_LINK_PREFIX = "/link"


def _is_format_character(ch: str) -> bool:
    return unicodedata.category(ch) == "Cf"


def _strip_leading_invisible(text: str) -> str:
    """`text` without its leading whitespace and format characters."""
    start = 0
    while start < len(text) and (text[start].isspace() or _is_format_character(text[start])):
        start += 1
    return text[start:]


def _looks_like_link(token: str) -> bool:
    """True when `token`, ignoring format characters and case, starts with `/link`."""
    visible = "".join(ch for ch in token if not _is_format_character(ch))
    return visible[: len(_LINK_PREFIX)].lower() == _LINK_PREFIX


def _positive_int(value: Any) -> bool:
    """A real int above zero. `bool` is an int subclass and is refused."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _update_id(update: Mapping[str, Any]) -> int:
    value = update.get("update_id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _ignored(
    update_id: int,
    reason: str,
    chat_id: Optional[int] = None,
    sender_id: Optional[int] = None,
) -> ClassifiedUpdate:
    return ClassifiedUpdate(
        kind=UpdateKind.IGNORED,
        update_id=update_id,
        chat_id=chat_id,
        sender_id=sender_id,
        text=None,
        code_text=None,
        reason=reason,
    )


def classify(update: Mapping[str, Any]) -> ClassifiedUpdate:
    """What this update is. Never raises for a malformed payload: it is IGNORED."""
    if not isinstance(update, Mapping):
        return _ignored(0, "malformed")
    update_id = _update_id(update)

    # Only a new message. Edits, channel posts, callbacks and every other
    # update kind carry no `message` and are ignored.
    message = update.get("message")
    if not isinstance(message, Mapping):
        return _ignored(update_id, "not_message")

    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        return _ignored(update_id, "no_chat")
    chat_type = chat.get("type")
    if isinstance(chat_type, str) and chat_type in _GROUP_TYPES:
        return _ignored(update_id, "group")
    if chat_type != "private":
        return _ignored(update_id, "not_private")

    sender = message.get("from")
    if not isinstance(sender, Mapping):
        return _ignored(update_id, "no_sender")
    if sender.get("is_bot") is not False:
        return _ignored(update_id, "bot")

    chat_id = chat.get("id")
    sender_id = sender.get("id")
    if not _positive_int(chat_id) or not _positive_int(sender_id):
        return _ignored(update_id, "invalid_id")
    if sender_id != chat_id:
        return _ignored(update_id, "sender_mismatch")

    if any(field in message for field in _SECOND_HAND_FIELDS):
        return _ignored(update_id, "second_hand", chat_id=chat_id, sender_id=sender_id)

    text = message.get("text")
    if not isinstance(text, str) or not text:
        return _ignored(update_id, "no_text", chat_id=chat_id, sender_id=sender_id)

    stripped = _strip_leading_invisible(text).strip()
    parts = stripped.split(None, 1)
    if parts and _looks_like_link(parts[0]):
        if LINK_COMMAND.fullmatch(parts[0]) is not None:
            code_text = parts[1].strip() if len(parts) > 1 else ""
        else:
            # A near miss: never redeemable. The whole message starts with
            # "/", which no code contains, so redemption refuses it.
            code_text = stripped
        return ClassifiedUpdate(
            kind=UpdateKind.LINK_COMMAND,
            update_id=update_id,
            chat_id=chat_id,
            sender_id=sender_id,
            text=None,
            code_text=code_text,
            reason="link_command",
        )

    return ClassifiedUpdate(
        kind=UpdateKind.PRIVATE_TEXT,
        update_id=update_id,
        chat_id=chat_id,
        sender_id=sender_id,
        text=text,
        code_text=None,
        reason="private_text",
    )


__all__ = ["ClassifiedUpdate", "LINK_COMMAND", "UpdateKind", "classify"]
