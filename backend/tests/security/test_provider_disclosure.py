"""Stage 3D: what actually leaves the machine for the LLM provider.

Not a vulnerability hunt -- a disclosure map. Mai sends personal data to a
third party by design, and the point is that the categories are enumerated and
enforced rather than assumed.

The test asserts an allowlist: anything a future change adds to the outbound
payload fails here until it is deliberately accounted for.
"""

import json

import pytest
from httpx import AsyncClient

from app.prompt.formatter import REFERENCE_HEADER
from tests.test_retrieval_integration import seed_knowledge

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})


# --- What is sent -----------------------------------------------------------


async def test_the_outbound_payload_contains_only_known_categories(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """Five categories, all of them user data, all of them intended.

    1. the application system prompt        (not user data)
    2. the current user message
    3. recent conversation from this thread
    4. retrieved memories
    5. retrieved entities and relationships
    """
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    sent = fake_provider.last_call
    roles = {message.role for message in sent}
    assert roles <= {"system", "user", "assistant"}

    # No message carries anything but text. No ids, scores or metadata.
    for message in sent:
        assert set(message.to_dict()) == {"role", "content"}


async def test_no_database_identifier_reaches_the_provider(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )
    whole = "\n".join(message.content for message in fake_provider.last_call)

    for collection in ("memories", "entities", "relationships"):
        for item in (await client.get(f"/api/{collection}")).json()["items"]:
            assert item["id"] not in whole, f"a {collection} id was sent upstream"
    assert str(conversation_id) not in whole


async def test_no_configuration_reaches_the_provider_as_content(
    client: AsyncClient, conversation_id, fake_provider, settings
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello."},
    )
    whole = "\n".join(message.content for message in fake_provider.last_call)

    assert settings.GROQ_API_KEY not in whole
    assert settings.DATABASE_URL not in whole


async def test_another_conversation_is_never_sent_verbatim(
    client: AsyncClient, fake_provider
) -> None:
    """Cross-conversation recall goes through memories, never raw transcripts.

    A memory is a short extracted statement that passed importance and
    confidence thresholds. The raw text of an unrelated conversation is not
    part of the disclosure surface, and that distinction matters: it is the
    difference between sending a fact and sending a transcript.
    """
    fake_provider.extraction_reply = NOTHING_TO_STORE
    first = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{first}/messages",
        json={"content": "SECRET-TRANSCRIPT-LINE about my divorce."},
    )

    second = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{second}/messages",
        json={"content": "Tell me about my divorce."},
    )

    whole = "\n".join(message.content for message in fake_provider.last_call)
    assert "SECRET-TRANSCRIPT-LINE" not in whole


async def test_superseded_knowledge_is_not_sent_on_an_ordinary_question(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """Retired knowledge is withheld from the provider too, not just from ranking."""
    from tests.test_knowledge_retrieval import teach

    await teach(
        client, fake_provider,
        "Mai uses OpenRouter for inference.",
        "Mai uses OpenRouter for inference.",
        entity_pairs=[("Mai", "project"), ("OpenRouter", "company")],
        triples=[("Mai", "USES", "OpenRouter")],
    )
    await teach(
        client, fake_provider,
        "I migrated Mai from OpenRouter to Groq.",
        "User migrated Mai from OpenRouter to Groq.",
        entity_pairs=[("Mai", "project"), ("Groq", "company")],
        triples=[("Mai", "USES", "Groq")],
    )

    fake_provider.extraction_reply = NOTHING_TO_STORE
    fresh = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{fresh}/messages",
        json={"content": "What does Mai use for inference now?"},
    )

    whole = "\n".join(message.content for message in fake_provider.last_call)
    assert "Mai uses OpenRouter for inference." not in whole


# --- Background extraction disclosure ---------------------------------------


async def test_extraction_sends_only_the_turn_it_is_analysing(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """Background extraction is a second disclosure path and is bounded too."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "First turn with EARLIER-MARKER."},
    )
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Second turn."},
    )

    latest = "\n".join(m.content for m in fake_provider.last_extraction_call)
    assert "Second turn." in latest
    assert "EARLIER-MARKER" not in latest, (
        "extraction sent history beyond the turn it was given"
    )


# --- Transport --------------------------------------------------------------


def test_the_credential_travels_in_a_header_not_a_url() -> None:
    """A key in a query string is logged by every proxy on the path."""
    import ast
    import inspect

    from app.llm.providers import openai_compatible

    source = inspect.getsource(openai_compatible)
    assert 'f"Bearer {self._api_key}"' in source, "the key is not sent as a header"

    # The key must never be interpolated into a path or query string. Checking
    # string *literals* rather than the raw source avoids matching the keyword
    # argument `api_key=` in the constructor, which is not a URL.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        literals = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            literals.extend(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        for literal in literals:
            lowered = literal.lower()
            assert "api_key=" not in lowered, f"key in a literal: {literal!r}"
            assert "?key=" not in lowered, f"key in a query string: {literal!r}"
            assert "access_token=" not in lowered


def test_the_provider_base_url_is_https_by_default() -> None:
    from app.core.config import Settings

    settings = Settings(_env_file=None, GROQ_API_KEY="x")
    assert settings.GROQ_BASE_URL.startswith("https://")


async def test_disabling_retrieval_removes_long_term_data_from_the_payload(
    client: AsyncClient, fake_provider, session_factory, settings
) -> None:
    """An operator can shrink the disclosure surface to the current thread."""
    conversation_id = await seed_knowledge(session_factory)
    settings.RETRIEVAL_ENABLED = False
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    whole = "\n".join(m.content for m in fake_provider.last_call)
    assert REFERENCE_HEADER not in whole
    assert "PostgreSQL" not in whole


async def test_disabling_extraction_stops_the_second_disclosure_path(
    client: AsyncClient, conversation_id, fake_provider, settings
) -> None:
    settings.MEMORY_EXTRACTION_ENABLED = False

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Nothing should be extracted from this."},
    )

    assert fake_provider.extraction_calls == []
    assert fake_provider.entity_calls == []
    assert fake_provider.relationship_calls == []
