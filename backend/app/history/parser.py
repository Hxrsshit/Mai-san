"""Bounded, defensive parsing of a ChatGPT export.

An export is a large document produced outside this system. It is treated the
way every other external document is: detect the shape first, refuse anything
unrecognised, and bound every dimension that could grow -- file size,
uncompressed size, member count, conversation count, message count, message
length, and the depth of the node graph.

Nothing here touches the database, makes a model call, or opens a socket. It
turns bytes into validated value objects, or it raises. A structural test
asserts that isolation.

### The node graph

ChatGPT does not store a conversation as a list. `mapping` is a dict of nodes
keyed by id, each with a `parent` and `children`, forming a tree -- branches
exist because a user can edit a message and regenerate. `current_node` names
the leaf of the branch that was actually left on screen.

Walking parent links up from `current_node` yields that branch in reverse,
which is the honest reconstruction of "the conversation as the user last saw
it". Iterating `mapping.values()` instead would interleave abandoned branches
with the real one and invent a conversation that never happened.

The walk is depth-capped and visited-guarded, because a malformed export can
present a cycle and `parent` chains are attacker-influenced data.
"""

import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import Settings
from app.history.models import ImportFormat, ImportedRole
from app.history.sanitise import scrub

#: The member the archive must contain to be a ChatGPT export.
CONVERSATIONS_MEMBER = "conversations.json"

#: Export role strings this system recognises, mapped to the closed vocabulary.
#: Anything else becomes UNKNOWN -- archived, never treated as the user.
_ROLE_MAP = {
    "user": ImportedRole.USER,
    "assistant": ImportedRole.ASSISTANT,
    "system": ImportedRole.SYSTEM,
    "tool": ImportedRole.TOOL,
}


class ImportParseError(Exception):
    """Parsing refused the document.

    Carries an application reason code, never a fragment of the document: a
    parser exception that quotes what broke it is a way to get archive content
    into a log line with different retention from the database.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ParsedMessage:
    role: ImportedRole
    content: str
    content_type: str
    external_id: str
    sequence: int
    created_at: Optional[datetime]
    redactions: int
    truncated: bool


@dataclass
class ParsedConversation:
    external_id: str
    title: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    messages: List[ParsedMessage] = field(default_factory=list)


@dataclass
class ParseOutcome:
    """Everything the parser produced, plus what the bounds stopped."""

    conversations: List[ParsedConversation]
    conversations_skipped: int = 0
    messages_skipped: int = 0
    redactions: int = 0
    #: True when a limit was reached, so the caller can record PARTIAL rather
    #: than claiming a complete import.
    truncated_by_limit: bool = False


# --- Format detection --------------------------------------------------------


def detect_format(path, settings: Settings) -> ImportFormat:
    """Identify the export shape, or refuse it.

    Runs before any parsing, and reads as little as possible: the zip central
    directory, or the first byte of a JSON file. An unrecognised file is
    rejected without its contents ever being interpreted.
    """
    try:
        size = path.stat().st_size
    except OSError:
        raise ImportParseError("source_unreadable")

    if size == 0:
        raise ImportParseError("source_empty")
    if size > settings.IMPORT_MAX_FILE_BYTES:
        raise ImportParseError("source_too_large")

    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, OSError):
            raise ImportParseError("zip_unreadable")
        if len(names) > settings.IMPORT_MAX_ZIP_MEMBERS:
            raise ImportParseError("zip_too_many_members")
        if _find_member(names) is None:
            raise ImportParseError("zip_missing_conversations")
        return ImportFormat.CHATGPT_ZIP

    if path.suffix.lower() == ".json":
        return ImportFormat.CHATGPT_JSON

    raise ImportParseError("unsupported_format")


def _find_member(names) -> Optional[str]:
    """`conversations.json`, at the root or one directory down.

    Exports have shipped both ways. Deeper paths are not searched: an export
    does not nest it further, and accepting any depth would mean honouring a
    path the archive chose.
    """
    for name in names:
        if name == CONVERSATIONS_MEMBER:
            return name
    for name in names:
        parts = name.split("/")
        if len(parts) == 2 and parts[1] == CONVERSATIONS_MEMBER:
            return name
    return None


# --- Reading -----------------------------------------------------------------


def read_document(path, import_format: ImportFormat, settings: Settings) -> Any:
    """Load the conversations document, with size checked before extraction."""
    if import_format is ImportFormat.CHATGPT_JSON:
        try:
            raw = path.read_bytes()
        except OSError:
            raise ImportParseError("source_unreadable")
        return _load_json(raw)

    try:
        with zipfile.ZipFile(path) as archive:
            member = _find_member(archive.namelist())
            if member is None:  # pragma: no cover - detection already checked
                raise ImportParseError("zip_missing_conversations")

            # The declared uncompressed size is checked *before* reading, so a
            # zip bomb is refused rather than expanded. The declared size is
            # then verified against what actually came out, because the header
            # is part of the untrusted document.
            info = archive.getinfo(member)
            total = sum(i.file_size for i in archive.infolist())
            if (
                info.file_size > settings.IMPORT_MAX_UNCOMPRESSED_BYTES
                or total > settings.IMPORT_MAX_UNCOMPRESSED_BYTES
            ):
                raise ImportParseError("uncompressed_too_large")

            with archive.open(member) as handle:
                raw = handle.read(settings.IMPORT_MAX_UNCOMPRESSED_BYTES + 1)
            if len(raw) > settings.IMPORT_MAX_UNCOMPRESSED_BYTES:
                raise ImportParseError("uncompressed_too_large")
    except zipfile.BadZipFile:
        raise ImportParseError("zip_unreadable")
    except OSError:
        raise ImportParseError("source_unreadable")

    return _load_json(raw)


def _load_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ImportParseError("not_utf8")
    except (ValueError, RecursionError):
        # RecursionError included deliberately: json's C parser raises it on a
        # deeply nested document, and an unhandled one would be a crash rather
        # than a refusal.
        raise ImportParseError("malformed_json")


# --- Parsing ------------------------------------------------------------------


def parse_conversations(document: Any, settings: Settings) -> ParseOutcome:
    """Turn a loaded export into bounded, scrubbed value objects."""
    if not isinstance(document, list):
        raise ImportParseError("unexpected_document_shape")

    outcome = ParseOutcome(conversations=[])
    total_messages = 0

    for index, entry in enumerate(document):
        if len(outcome.conversations) >= settings.IMPORT_MAX_CONVERSATIONS:
            outcome.conversations_skipped += len(document) - index
            outcome.truncated_by_limit = True
            break
        if not isinstance(entry, dict):
            outcome.conversations_skipped += 1
            continue

        remaining = settings.IMPORT_MAX_TOTAL_MESSAGES - total_messages
        if remaining <= 0:
            outcome.conversations_skipped += len(document) - index
            outcome.truncated_by_limit = True
            break

        conversation = _parse_one(entry, index, remaining, settings, outcome)
        if conversation is None:
            outcome.conversations_skipped += 1
            continue

        total_messages += len(conversation.messages)
        outcome.conversations.append(conversation)

    return outcome


def _parse_one(
    entry: Dict[str, Any],
    index: int,
    remaining: int,
    settings: Settings,
    outcome: ParseOutcome,
) -> Optional[ParsedConversation]:
    external_id = _text(entry.get("conversation_id") or entry.get("id"), 128)
    if not external_id:
        # Position is a stable fallback within one archive, and the archive is
        # the uniqueness scope.
        external_id = f"position-{index}"

    mapping = entry.get("mapping")
    if not isinstance(mapping, dict):
        return None

    nodes = _ordered_nodes(mapping, entry.get("current_node"), settings)

    messages: List[ParsedMessage] = []
    per_conversation_cap = min(
        settings.IMPORT_MAX_MESSAGES_PER_CONVERSATION, remaining
    )

    for node in nodes:
        if len(messages) >= per_conversation_cap:
            outcome.messages_skipped += 1
            outcome.truncated_by_limit = True
            continue
        parsed = _parse_message(node, len(messages), settings)
        if parsed is None:
            continue
        outcome.redactions += parsed.redactions
        messages.append(parsed)

    return ParsedConversation(
        external_id=external_id,
        title=_text(entry.get("title"), 500),
        created_at=_timestamp(entry.get("create_time")),
        updated_at=_timestamp(entry.get("update_time")),
        messages=messages,
    )


def _ordered_nodes(
    mapping: Dict[str, Any], current_node: Any, settings: Settings
) -> List[Dict[str, Any]]:
    """The displayed branch, oldest first.

    Walks `parent` links up from `current_node`. Falls back to the mapping's
    insertion order only when `current_node` is missing or unusable, which is
    the best available guess for an export that does not name a leaf.
    """
    chain: List[Dict[str, Any]] = []
    seen = set()
    node_id = current_node if isinstance(current_node, str) else None
    depth = 0

    while node_id is not None and depth < settings.IMPORT_MAX_THREAD_DEPTH:
        if node_id in seen:
            # A cycle. Stop rather than loop: `parent` is untrusted data.
            break
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            break
        chain.append(node)
        parent = node.get("parent")
        node_id = parent if isinstance(parent, str) else None
        depth += 1

    if chain:
        chain.reverse()
        return chain

    return [node for node in mapping.values() if isinstance(node, dict)]


def _parse_message(
    node: Dict[str, Any], sequence: int, settings: Settings
) -> Optional[ParsedMessage]:
    message = node.get("message")
    if not isinstance(message, dict):
        return None

    author = message.get("author")
    role_text = author.get("role") if isinstance(author, dict) else None
    role = _ROLE_MAP.get(role_text if isinstance(role_text, str) else "", ImportedRole.UNKNOWN)

    content = message.get("content")
    if not isinstance(content, dict):
        return None
    content_type = _text(content.get("content_type"), 64)

    text = _parts_to_text(content.get("parts"), settings)
    if not text.strip():
        # Empty turns are structural padding in an export -- a root node, or a
        # placeholder for a tool call. Archiving them adds rows and no history.
        return None

    truncated = False
    if len(text) > settings.IMPORT_MAX_MESSAGE_CHARS:
        text = text[: settings.IMPORT_MAX_MESSAGE_CHARS]
        truncated = True

    scrubbed = scrub(text)

    external_id = _text(message.get("id") or node.get("id"), 128)
    if not external_id:
        external_id = f"sequence-{sequence}"

    return ParsedMessage(
        role=role,
        content=scrubbed.text,
        content_type=content_type,
        external_id=external_id,
        sequence=sequence,
        created_at=_timestamp(message.get("create_time")),
        redactions=scrubbed.count,
        truncated=truncated,
    )


def _parts_to_text(parts: Any, settings: Settings) -> str:
    """Flatten `content.parts` into text.

    Parts are usually strings, but `multimodal_text` mixes in dicts describing
    images and other assets. Those carry no statement by the user, so they are
    dropped rather than stringified -- `str(dict)` would archive an asset
    pointer as if it were something the user said.
    """
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""

    pieces: List[str] = []
    for part in parts[: settings.IMPORT_MAX_PARTS_PER_MESSAGE]:
        if isinstance(part, str):
            pieces.append(part)
    return "\n".join(pieces)


def _text(value: Any, limit: int) -> str:
    """A bounded string, or empty. Never coerces a non-string."""
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _timestamp(value: Any) -> Optional[datetime]:
    """A UTC datetime from the export's epoch seconds, or None.

    None rather than a guess: these timestamps decide recency in conflict
    resolution, and inventing one would make the rules act on a fiction.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


__all__ = [
    "CONVERSATIONS_MEMBER",
    "ImportParseError",
    "ParseOutcome",
    "ParsedConversation",
    "ParsedMessage",
    "detect_format",
    "parse_conversations",
    "read_document",
]
