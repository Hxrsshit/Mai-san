"""Stage 5C: parsing, bounding and archiving an imported export.

The behaviour suite. Security properties live in
`tests/security/test_history_import_security.py`; this file is about whether
the importer reads a real export correctly and refuses a broken one.
"""

import json
import zipfile
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.history.models import ImportedRole, ImportFormat, ImportStatus
from app.history.parser import (
    ImportParseError,
    detect_format,
    parse_conversations,
    read_document,
)
from app.history.sanitise import contains_secret, scrub
from app.history.sources import ImportSourceError, list_sources, resolve, sha256_of
from tests.conftest import chatgpt_export

pytestmark = pytest.mark.anyio


def load(path, settings: Settings):
    fmt = detect_format(path, settings)
    return fmt, parse_conversations(read_document(path, fmt, settings), settings)


# --- Format detection --------------------------------------------------------


def test_a_zip_export_is_recognised(import_dir, import_settings, write_export) -> None:
    name = write_export([{"id": "c1", "turns": [("user", "hello there")]}])
    assert detect_format(import_dir / name, import_settings) is ImportFormat.CHATGPT_ZIP


def test_a_bare_conversations_json_is_recognised(
    import_dir, import_settings, write_export
) -> None:
    name = write_export(
        [{"id": "c1", "turns": [("user", "hello")]}],
        name="conversations.json",
        as_zip=False,
    )
    assert (
        detect_format(import_dir / name, import_settings) is ImportFormat.CHATGPT_JSON
    )


def test_conversations_json_one_directory_down_is_found(
    import_dir, import_settings
) -> None:
    """Exports have shipped both flat and inside a folder."""
    path = import_dir / "nested.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("chatgpt-export/conversations.json", "[]")
    assert detect_format(path, import_settings) is ImportFormat.CHATGPT_ZIP


@pytest.mark.parametrize(
    "name, payload, code",
    [
        ("notes.txt", b"hello", "unsupported_format"),
        ("empty.json", b"", "source_empty"),
    ],
)
def test_an_unusable_file_is_refused_before_parsing(
    import_dir, import_settings, name, payload, code
) -> None:
    path = import_dir / name
    path.write_bytes(payload)
    with pytest.raises(ImportParseError) as raised:
        detect_format(path, import_settings)
    assert raised.value.code == code


def test_a_zip_without_conversations_is_refused(import_dir, import_settings) -> None:
    path = import_dir / "other.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("user.json", "{}")
    with pytest.raises(ImportParseError) as raised:
        detect_format(path, import_settings)
    assert raised.value.code == "zip_missing_conversations"


def test_malformed_json_is_a_refusal_not_a_crash(
    import_dir, import_settings, write_export
) -> None:
    name = write_export("{not json at all", name="broken.zip")
    with pytest.raises(ImportParseError) as raised:
        read_document(import_dir / name, ImportFormat.CHATGPT_ZIP, import_settings)
    assert raised.value.code == "malformed_json"


def test_a_document_that_is_not_a_list_is_refused(
    import_dir, import_settings, write_export
) -> None:
    name = write_export('{"conversations": []}', name="shape.zip")
    _, document = ImportFormat.CHATGPT_ZIP, read_document(
        import_dir / name, ImportFormat.CHATGPT_ZIP, import_settings
    )
    with pytest.raises(ImportParseError) as raised:
        parse_conversations(document, import_settings)
    assert raised.value.code == "unexpected_document_shape"


# --- Parsing -----------------------------------------------------------------


def test_a_conversation_parses_into_ordered_messages(
    import_dir, import_settings, write_export
) -> None:
    name = write_export(
        [
            {
                "id": "c1",
                "title": "Databases",
                "created": 1690000000.0,
                "turns": [
                    ("user", "I prefer PostgreSQL."),
                    ("assistant", "Good choice."),
                    ("user", "Mostly for the JSON support."),
                ],
            }
        ]
    )
    _, outcome = load(import_dir / name, import_settings)

    assert len(outcome.conversations) == 1
    conversation = outcome.conversations[0]
    assert conversation.external_id == "c1"
    assert conversation.title == "Databases"
    assert [m.role for m in conversation.messages] == [
        ImportedRole.USER,
        ImportedRole.ASSISTANT,
        ImportedRole.USER,
    ]
    assert [m.sequence for m in conversation.messages] == [0, 1, 2]
    assert conversation.created_at == datetime.fromtimestamp(
        1690000000.0, tz=timezone.utc
    )


def test_only_the_displayed_branch_is_imported(import_dir, import_settings) -> None:
    """A regenerated answer leaves an abandoned branch in `mapping`.

    Iterating `mapping.values()` would interleave it with the real thread and
    archive a conversation that never happened.
    """
    document = chatgpt_export([{"id": "c1", "turns": [("user", "kept question")]}])
    mapping = document[0]["mapping"]
    mapping["abandoned"] = {
        "id": "abandoned",
        "parent": "root",
        "children": [],
        "message": {
            "id": "abandoned-m",
            "author": {"role": "user"},
            "create_time": 1690000000.0,
            "content": {"content_type": "text", "parts": ["ABANDONED BRANCH"]},
        },
    }
    path = import_dir / "branch.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", json.dumps(document))

    _, outcome = load(path, import_settings)
    texts = [m.content for m in outcome.conversations[0].messages]
    assert texts == ["kept question"]


def test_a_cyclic_parent_chain_terminates(import_dir, import_settings) -> None:
    """`parent` is untrusted data, so the walk cannot assume a tree."""
    document = chatgpt_export([{"id": "c1", "turns": [("user", "a"), ("user", "b")]}])
    mapping = document[0]["mapping"]
    # Point the first node's parent at the last, closing a loop.
    mapping["c1-n0"]["parent"] = "c1-n1"
    path = import_dir / "cycle.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", json.dumps(document))

    _, outcome = load(path, import_settings)
    assert len(outcome.conversations[0].messages) <= 2


def test_an_empty_message_is_not_archived(import_dir, import_settings) -> None:
    """Root nodes and placeholders are structure, not history."""
    document = chatgpt_export(
        [{"id": "c1", "turns": [("user", "   "), ("user", "real content")]}]
    )
    path = import_dir / "empty.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", json.dumps(document))

    _, outcome = load(path, import_settings)
    assert [m.content for m in outcome.conversations[0].messages] == ["real content"]


def test_a_multimodal_part_is_dropped_rather_than_stringified(
    import_dir, import_settings
) -> None:
    """`str(dict)` would archive an asset pointer as if the user had said it."""
    document = chatgpt_export([{"id": "c1", "turns": [("user", "look at this")]}])
    document[0]["mapping"]["c1-n0"]["message"]["content"] = {
        "content_type": "multimodal_text",
        "parts": [{"asset_pointer": "file-service://abc"}, "and the caption"],
    }
    path = import_dir / "mm.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", json.dumps(document))

    _, outcome = load(path, import_settings)
    content = outcome.conversations[0].messages[0].content
    assert content == "and the caption"
    assert "asset_pointer" not in content


def test_an_unknown_role_becomes_unknown_not_user(
    import_dir, import_settings
) -> None:
    """An unrecognised author must never be mistaken for the user."""
    document = chatgpt_export([{"id": "c1", "turns": [("user", "hi")]}])
    document[0]["mapping"]["c1-n0"]["message"]["author"]["role"] = "plugin"
    path = import_dir / "role.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", json.dumps(document))

    _, outcome = load(path, import_settings)
    assert outcome.conversations[0].messages[0].role is ImportedRole.UNKNOWN


def test_a_missing_timestamp_stays_missing(import_dir, import_settings) -> None:
    """Inventing one would make the conflict rules act on a fiction."""
    document = chatgpt_export([{"id": "c1", "turns": [("user", "hi")]}])
    del document[0]["mapping"]["c1-n0"]["message"]["create_time"]
    document[0]["create_time"] = "not a number"
    path = import_dir / "nots.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", json.dumps(document))

    _, outcome = load(path, import_settings)
    assert outcome.conversations[0].messages[0].created_at is None
    assert outcome.conversations[0].created_at is None


# --- Bounds -------------------------------------------------------------------


def test_the_conversation_cap_stops_the_import_and_says_so(
    import_dir, import_settings, write_export
) -> None:
    bounded = import_settings.model_copy(update={"IMPORT_MAX_CONVERSATIONS": 2})
    name = write_export(
        [{"id": f"c{i}", "turns": [("user", f"message {i}")]} for i in range(5)]
    )
    _, outcome = load(import_dir / name, bounded)

    assert len(outcome.conversations) == 2
    assert outcome.conversations_skipped == 3
    assert outcome.truncated_by_limit is True


def test_the_total_message_cap_stops_the_import(
    import_dir, import_settings, write_export
) -> None:
    bounded = import_settings.model_copy(update={"IMPORT_MAX_TOTAL_MESSAGES": 3})
    name = write_export(
        [{"id": f"c{i}", "turns": [("user", "a"), ("user", "b")]} for i in range(4)]
    )
    _, outcome = load(import_dir / name, bounded)

    total = sum(len(c.messages) for c in outcome.conversations)
    assert total <= 3
    assert outcome.truncated_by_limit is True


def test_an_overlong_message_is_truncated_and_flagged(
    import_dir, import_settings, write_export
) -> None:
    bounded = import_settings.model_copy(update={"IMPORT_MAX_MESSAGE_CHARS": 20})
    name = write_export([{"id": "c1", "turns": [("user", "x" * 500)]}])
    _, outcome = load(import_dir / name, bounded)

    message = outcome.conversations[0].messages[0]
    assert len(message.content) == 20
    assert message.truncated is True


def test_a_file_over_the_size_cap_is_refused(
    import_dir, import_settings, write_export
) -> None:
    tiny = import_settings.model_copy(update={"IMPORT_MAX_FILE_BYTES": 10})
    name = write_export([{"id": "c1", "turns": [("user", "hello world")]}])
    with pytest.raises(ImportParseError) as raised:
        detect_format(import_dir / name, tiny)
    assert raised.value.code == "source_too_large"


def test_a_zip_bomb_is_refused_before_extraction(
    import_dir, import_settings
) -> None:
    """The declared uncompressed size is checked first, so it is not expanded."""
    path = import_dir / "bomb.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("conversations.json", "[" + ("0," * 500_000) + "0]")

    bounded = import_settings.model_copy(
        update={"IMPORT_MAX_UNCOMPRESSED_BYTES": 1000}
    )
    with pytest.raises(ImportParseError) as raised:
        read_document(path, ImportFormat.CHATGPT_ZIP, bounded)
    assert raised.value.code == "uncompressed_too_large"


# --- Sources -------------------------------------------------------------------


def test_sources_are_listed_newest_first(import_dir, import_settings) -> None:
    import os
    import time

    for index, name in enumerate(["old.zip", "new.zip"]):
        path = import_dir / name
        path.write_bytes(b"PK\x03\x04")
        os.utime(path, (time.time() + index, time.time() + index))

    names = [source.filename for source in list_sources(import_settings)]
    assert names == ["new.zip", "old.zip"]


def test_a_missing_import_directory_lists_nothing(import_settings, tmp_path) -> None:
    """An unconfigured deployment is not an error worth raising at a panel."""
    missing = import_settings.model_copy(
        update={"MAI_IMPORT_DIR": str(tmp_path / "nope")}
    )
    assert list_sources(missing) == []


def test_the_same_bytes_hash_the_same_under_a_different_name(
    import_dir, import_settings
) -> None:
    """Idempotency is content-addressed: a renamed export is the same export."""
    (import_dir / "a.zip").write_bytes(b"identical bytes")
    (import_dir / "b.zip").write_bytes(b"identical bytes")
    assert sha256_of(import_dir / "a.zip", import_settings) == sha256_of(
        import_dir / "b.zip", import_settings
    )


# --- Scrubbing -------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "gsk_ABCDEFGH12345678",
        "sk-or-v1-ABCDEFGH1234",
        "ghp_ABCDEFGH1234",
        "github_pat_ABCDEFGH1234",
        # Assembled rather than written out. `test_secrets.py` forbids a
        # credential *shape* in any test file, and the AWS pattern has no
        # length discriminator a "TEST" marker could break -- so the literal
        # cannot appear here even as an obvious fake.
        "AKIA" + "TESTKEY" + "A" * 9,
        "postgresql://user:hunter2@host/db",
        "Bearer abcdefgh12345678",
    ],
)
def test_a_credential_shape_is_masked(secret) -> None:
    result = scrub(f"here it is: {secret} ok")
    assert result.count >= 1
    assert not contains_secret(result.text)


def test_scrubbing_is_idempotent() -> None:
    """A second pass must be a no-op, or `contains_secret` means nothing."""
    once = scrub("key gsk_ABCDEFGH12345678 and postgres://u:p@h/db")
    twice = scrub(once.text)
    assert twice.text == once.text
    assert twice.count == 0


def test_ordinary_text_is_left_exactly_alone() -> None:
    text = "I decided to use PostgreSQL for the project in March."
    result = scrub(text)
    assert result.text == text
    assert result.count == 0
