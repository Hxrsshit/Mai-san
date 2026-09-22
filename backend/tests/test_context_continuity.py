"""Stage 5D.2: conversational continuity through the real pipeline.

From an HTTP turn to a research proposal carrying an inherited subject, with
every existing gate intact.
"""

import json

import pytest

pytestmark = pytest.mark.anyio

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})


async def new_conversation(client):
    return (await client.post("/api/conversations", json={})).json()["id"]


async def send(client, conversation_id, content):
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": content}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- The canonical case, end to end ------------------------------------------------


async def test_the_fable_follow_up_proposes_a_search_for_the_real_subject(
    research_client, fake_provider
) -> None:
    """The Stage 5D.0 failure, through the real stack.

    Before Stage 5D.2 this proposed a search for **"let me know"**.
    """
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)

    await send(research_client, conversation_id, "Is Fable better or Asta?")
    body = await send(
        research_client, conversation_id, "search up the net and let me know"
    )

    research = body["research"]
    assert research["outcome"] == "awaiting_confirmation"
    assert research["query"] == "Fable vs Asta"
    assert "let me know" not in research["query"]
    # Nothing has been sent anywhere yet.
    assert research["searched"] is False


async def test_the_proposal_shows_the_resolved_query_to_the_user(
    research_client, fake_provider
) -> None:
    """A wrong resolution must be refusable, so it has to be visible."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Is Fable better or Asta?")
    body = await send(research_client, conversation_id, "search it")

    assert "Fable vs Asta" in body["assistant_message"]["content"]


async def test_consent_is_still_required_before_anything_is_searched(
    research_client, fake_provider
) -> None:
    """§14: resolved context is not authorization."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Is Fable better or Asta?")
    proposed = await send(research_client, conversation_id, "search it")

    assert proposed["research"]["searched"] is False
    assert proposed["research"]["outcome"] == "awaiting_confirmation"

    confirmed = await send(research_client, conversation_id, "yes")
    assert confirmed["research"]["outcome"] == "completed"
    assert confirmed["research"]["searched"] is True


async def test_declining_a_resolved_proposal_searches_nothing(
    research_client, fake_provider
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Is Fable better or Asta?")
    await send(research_client, conversation_id, "search it")
    declined = await send(research_client, conversation_id, "no thanks")

    assert declined["research"]["searched"] is not True


# --- Refusing to guess, through the pipeline -------------------------------------------


async def test_search_it_with_no_context_asks_rather_than_searching(
    research_client, fake_provider
) -> None:
    """§M / §34: never manufacture a subject."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    body = await send(research_client, conversation_id, "search it")

    assert body["research"]["outcome"] == "needs_clarification"
    assert body["research"]["searched"] is not True
    assert "what should I search for" in body["assistant_message"]["content"]


async def test_an_out_of_range_ordinal_asks_rather_than_guessing(
    research_client, fake_provider
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Compare Fable, Asta and Claude.")
    body = await send(
        research_client, conversation_id, "search the web for the fourth one"
    )

    assert body["research"]["outcome"] != "awaiting_confirmation"
    assert body["research"]["searched"] is not True


async def test_which_one_is_better_recovers_the_comparison(
    research_client, fake_provider
) -> None:
    """Found in live verification: "better" was read as a literal subject."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Compare Fable and Asta.")
    body = await send(
        research_client,
        conversation_id,
        "search the web and tell me which one is better",
    )

    query = body["research"]["query"]
    assert "Fable" in query and "Asta" in query
    assert query != "which one is better"


# --- Topic switching, through the pipeline ------------------------------------------------


async def test_a_topic_switch_does_not_leak_the_old_subject(
    research_client, fake_provider
) -> None:
    """§J."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Tell me about Fable.")
    await send(research_client, conversation_id, "Tell me about Bangalore.")
    body = await send(research_client, conversation_id, "search it")

    query = body["research"]["query"]
    assert "Fable" not in query
    assert "Bangalore" in query


async def test_an_explicit_return_reaches_the_older_topic(
    research_client, fake_provider
) -> None:
    """§K."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Tell me about Fable.")
    await send(research_client, conversation_id, "Tell me about Bangalore.")
    body = await send(
        research_client, conversation_id, "going back to Fable, search it"
    )

    assert "Fable" in body["research"]["query"]


# --- Long conversations ----------------------------------------------------------------


async def test_continuity_survives_a_long_conversation(
    research_client, fake_provider
) -> None:
    """§P: ten turns, then a contextual request."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    for index in range(10):
        await send(research_client, conversation_id, f"Tell me about Topic{index}.")

    body = await send(research_client, conversation_id, "search it")
    assert body["research"]["query"] == "Topic9"


# --- Existing behaviour is unchanged --------------------------------------------------------


async def test_a_message_with_its_own_subject_is_unaffected(
    research_client, fake_provider
) -> None:
    """§16: the existing research grammar still owns the ordinary case."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Tell me about Fable.")
    body = await send(
        research_client, conversation_id, "search the web for quantum computing"
    )

    assert body["research"]["query"] == "quantum computing"


async def test_an_ordinary_turn_is_not_turned_into_research(
    research_client, fake_provider
) -> None:
    """Resolution must not make Mai offer to search when nobody asked."""
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Tell me about Fable.")
    body = await send(research_client, conversation_id, "tell me more")

    assert body["research"] is None or body["research"]["outcome"] == "not_research"


async def test_a_misspelled_verb_still_does_not_become_a_request(
    research_client, fake_provider
) -> None:
    """§AB, and a Stage 5A property this stage must not weaken.

    Stage 5A deliberately does not spell-correct *verbs*: "a layer that can
    repair a broken verb into a working one is a layer that can manufacture an
    instruction out of noise." So "serch it" is not a research request, and
    context resolution does not change that -- resolution answers *what about*,
    never *whether to act*.
    """
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation_id = await new_conversation(research_client)
    await send(research_client, conversation_id, "Tell me about Fable.")
    body = await send(research_client, conversation_id, "serch it")

    assert body["research"] is None or body["research"]["outcome"] == "not_research"
