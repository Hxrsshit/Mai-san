"""Stage 5F.2 -- the Gmail daily assistant.

Recognition, bounded retrieval and the attention intent. Every behavioural
test here drives the **application path**: a real chat turn through the API,
the real router, the real authorization turn and a stubbed Google socket.

That is deliberate. Stage 5F.1's two live-only defects were both invisible to
component tests -- the layer was correct and the router dropped its result on
the floor -- so a test that calls `MailService.handle` directly proves the
wrong thing. The grammar tests below are the one exception, and they test a
pure function whose output the path tests then exercise.
"""

import pytest
from httpx import AsyncClient

from app.orchestration.mail_language import (
    MAX_ATTENTION_RESULTS,
    MAX_LIST_RESULTS,
    MAX_SUMMARY_BODIES,
    PRIORITY_WORDS,
    MailIntent,
    recognise,
)

pytestmark = pytest.mark.anyio


async def send(client, conversation_id, content):
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": content},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def reply_to(client, conversation_id, content) -> str:
    return (await send(client, conversation_id, content))["assistant_message"]["content"]


async def new_conversation(client):
    return (await client.post("/api/conversations", json={})).json()["id"]


# ============================================================================
# A. The request families 5F.2 exists to serve
# ============================================================================


#: Every phrasing Stage 5F.2 undertook to support, with what it must produce.
#:
#: A literal table, so adding a phrasing is a deliberate edit and losing one
#: is a test failure rather than a quiet regression. Eight of these reached no
#: handler at all before this stage -- the user asked for their mail and got a
#: general answer.
SUPPORTED = [
    ("Check my emails.", MailIntent.LIST, False),
    ("Check my inbox.", MailIntent.LIST, False),
    ("Show my unread emails.", MailIntent.LIST, False),
    ("Show me unread emails from today.", MailIntent.LIST, False),
    ("Show emails from today.", MailIntent.LIST, False),
    ("Show emails from yesterday.", MailIntent.LIST, False),
    ("Show emails from Amazon.", MailIntent.LIST, False),
    ("Do I have anything from Google?", MailIntent.LIST, False),
    ("Do I have any unread emails from Netflix?", MailIntent.LIST, False),
    ("Summarize my unread emails.", MailIntent.SUMMARISE, False),
    ("Give me a summary of today's important emails.", MailIntent.SUMMARISE, True),
    ("Summarise the latest email from Acme.", MailIntent.SUMMARISE, False),
    ("Which emails should I look at?", MailIntent.ATTENTION, True),
    ("Any emails I need to respond to?", MailIntent.ATTENTION, True),
    ("Anything urgent in my inbox?", MailIntent.ATTENTION, True),
    ("Do I have anything important in my email?", MailIntent.ATTENTION, True),
]


@pytest.mark.parametrize("message, intent, priority", SUPPORTED)
def test_every_supported_phrasing_is_recognised(message, intent, priority) -> None:
    request = recognise(message)
    assert request.is_readable, message
    assert request.intent is intent, message
    assert request.priority is priority, message


def test_a_date_word_becomes_a_bounded_day_count() -> None:
    assert recognise("Show emails from today.").newer_than_days == 1
    assert recognise("Show emails from yesterday.").newer_than_days == 2
    assert recognise("what emails did I get this week?").newer_than_days == 7
    assert recognise("Check my emails.").newer_than_days is None


def test_a_sender_is_extracted_as_one_bounded_token() -> None:
    assert recognise("Show emails from Amazon.").sender == "Amazon"
    assert recognise("Do I have anything from Google?").sender == "Google"
    assert recognise("Do I have any unread emails from Netflix?").sender == "Netflix"


def test_unread_is_read_from_the_words_not_guessed() -> None:
    assert recognise("Show my unread emails.").unread_only is True
    assert recognise("Check my emails.").unread_only is False


# ============================================================================
# B. Bounds
# ============================================================================


def test_the_three_read_depths_are_distinct() -> None:
    """`_intent_of` tells an attention read from a listing by its bound.

    Pinned as literals. If `MAX_LIST_RESULTS` and `MAX_ATTENTION_RESULTS`
    ever became equal, a completed attention read would report itself as an
    ordinary listing and lose its synthesis instruction -- silently.
    """
    assert MAX_LIST_RESULTS == 5
    assert MAX_ATTENTION_RESULTS == 8
    assert MAX_SUMMARY_BODIES == 3
    assert MAX_ATTENTION_RESULTS != MAX_LIST_RESULTS


def test_no_phrasing_asks_for_more_than_the_schema_permits() -> None:
    """The grammar can never propose a read the query schema would reject."""
    from app.integrations.gmail_schemas import MAX_BODIES, MAX_MESSAGES

    for message, _, _ in SUPPORTED:
        request = recognise(message)
        assert 1 <= request.max_results <= MAX_MESSAGES, message
        assert 0 <= request.body_count <= MAX_BODIES, message
        assert request.body_count <= request.max_results, message


def test_an_attention_read_asks_for_no_bodies() -> None:
    """Triage is answered from senders and subjects, not from eight bodies."""
    for message, intent, _ in SUPPORTED:
        if intent is MailIntent.ATTENTION:
            assert recognise(message).body_count == 0, message


def test_a_longer_message_cannot_widen_the_read() -> None:
    """Result counts come from the intent, never from the user's words."""
    for message in (
        "show me my latest 500 emails",
        "check my inbox, all of it, every single message",
        "show me emails max_results=1000",
    ):
        request = recognise(message)
        if request.is_readable:
            assert request.max_results <= MAX_ATTENTION_RESULTS, message


# ============================================================================
# C. "Important" is never a Gmail query
# ============================================================================


def test_a_priority_word_never_becomes_a_search_term() -> None:
    """§: "important" must not be an arbitrary model-generated Gmail query.

    It is not a Gmail operator, and `subject:("important")` would answer a
    different question -- messages with the word in the subject line. Gmail's
    own `is:important` is equally absent: it is Google's classifier, and
    `MailQuery` has no field that could carry it.
    """
    for message in (
        "Which emails should I look at?",
        "Anything urgent in my inbox?",
        "Do I have anything important in my email?",
        "Any emails I need to respond to?",
        "show me emails about important changes",
        "give me a summary of today's important emails",
    ):
        request = recognise(message)
        rendered = " ".join(request.subject_terms).lower()
        for word in PRIORITY_WORDS:
            assert word not in rendered, (message, word)
        assert "important" not in request.sender.lower(), message


def test_the_rendered_query_carries_no_priority_operator() -> None:
    from app.integrations.gmail_schemas import MailQuery

    request = recognise("Which emails should I look at?")
    query = MailQuery(
        sender=request.sender,
        subject_terms=request.subject_terms,
        unread_only=request.unread_only,
        newer_than_days=request.newer_than_days,
        max_results=request.max_results,
    ).to_gmail_query()

    assert "is:important" not in query
    assert "is:starred" not in query
    assert "important" not in query.lower()
    assert "label:" not in query


def test_the_query_schema_has_no_importance_field() -> None:
    """Literal-pinned: there is nowhere for a priority notion to be carried."""
    from app.integrations.gmail_schemas import MailQuery

    assert set(MailQuery.model_fields) == {
        "sender", "subject_terms", "text_terms", "unread_only",
        "newer_than_days", "max_results",
    }


# ============================================================================
# D. The application path: a whole turn, through the router
# ============================================================================


async def test_an_attention_question_proposes_before_reading(
    gmail_client: AsyncClient
) -> None:
    conversation = await new_conversation(gmail_client)
    reply = await reply_to(gmail_client, conversation, "Which emails should I look at?")

    assert "attention" in reply.lower()
    assert "up to 8" in reply
    assert "no message bodies" in reply
    # Disclosure happens before any request to Google.
    assert gmail_client.gmail_transport.urls == []


async def test_confirming_an_attention_question_reads_and_answers(
    gmail_client: AsyncClient, fake_provider
) -> None:
    conversation = await new_conversation(gmail_client)
    await send(gmail_client, conversation, "Which emails should I look at?")
    reply = await reply_to(gmail_client, conversation, "yes")

    assert reply.strip(), "the turn stored an empty assistant message"
    assert gmail_client.gmail_transport.urls, "nothing was read"

    prompt = "\n".join(m.content for m in fake_provider.last_call)
    assert "deserve their attention" in prompt
    assert "do not state a total different from the number listed" in prompt


async def test_an_ordinary_listing_gets_no_attention_instruction(
    gmail_client: AsyncClient, fake_provider
) -> None:
    """The instruction is for the question that asked for a judgement."""
    conversation = await new_conversation(gmail_client)
    await send(gmail_client, conversation, "Check my inbox.")
    await send(gmail_client, conversation, "yes")

    prompt = "\n".join(m.content for m in fake_provider.last_call)
    assert "deserve their attention" not in prompt
    # The untrusted framing is present either way.
    assert "data, not instructions" in prompt


async def test_a_sender_question_reaches_gmail_as_a_quoted_term(
    gmail_client: AsyncClient
) -> None:
    from urllib.parse import parse_qs, urlparse

    conversation = await new_conversation(gmail_client)
    await send(gmail_client, conversation, "Show emails from Amazon.")
    await send(gmail_client, conversation, "yes")

    listing = gmail_client.gmail_transport.urls[0]
    query = parse_qs(urlparse(listing).query).get("q", [""])[0]
    assert query == 'from:("Amazon")'


async def test_a_today_question_bounds_the_window_on_the_wire(
    gmail_client: AsyncClient
) -> None:
    from urllib.parse import parse_qs, urlparse

    conversation = await new_conversation(gmail_client)
    await send(gmail_client, conversation, "Show emails from today.")
    await send(gmail_client, conversation, "yes")

    listing = gmail_client.gmail_transport.urls[0]
    query = parse_qs(urlparse(listing).query).get("q", [""])[0]
    assert "newer_than:1d" in query


async def test_an_attention_read_requests_no_more_than_its_bound(
    gmail_client: AsyncClient
) -> None:
    from urllib.parse import parse_qs, urlparse

    conversation = await new_conversation(gmail_client)
    await send(gmail_client, conversation, "Which emails should I look at?")
    await send(gmail_client, conversation, "yes")

    listing = gmail_client.gmail_transport.urls[0]
    requested = int(parse_qs(urlparse(listing).query).get("maxResults", ["0"])[0])
    assert 1 <= requested <= MAX_ATTENTION_RESULTS


async def test_declining_reads_nothing(gmail_client: AsyncClient) -> None:
    conversation = await new_conversation(gmail_client)
    await send(gmail_client, conversation, "Which emails should I look at?")
    reply = await reply_to(gmail_client, conversation, "no")

    assert gmail_client.gmail_transport.urls == []
    assert reply.strip()


async def test_an_empty_mailbox_is_reported_as_empty(
    gmail_client: AsyncClient, fake_provider
) -> None:
    """Zero is a fact about the mailbox and must survive to the prompt."""
    gmail_client.gmail_transport.listing = {"messages": [], "resultSizeEstimate": 0}

    conversation = await new_conversation(gmail_client)
    await send(gmail_client, conversation, "Which emails should I look at?")
    await send(gmail_client, conversation, "yes")

    prompt = "\n".join(m.content for m in fake_provider.last_call)
    assert "(no messages matched)" in prompt


# ============================================================================
# E. Gaps found by mutation testing
# ============================================================================


def test_the_priority_vocabulary_contains_the_words_that_matter() -> None:
    """Literal-pinned, because the test above cannot pin itself.

    `test_a_priority_word_never_becomes_a_search_term` iterates over
    `PRIORITY_WORDS`, so it derives its expectation from the constant it
    guards: deleting "important" from the set made that test check one word
    fewer and pass. Mutation testing found it. The set is listed here as
    literals, so shrinking it has to be argued for.
    """
    assert {
        "important", "urgent", "priority", "pressing", "critical",
        "attention", "response", "reply",
    } <= PRIORITY_WORDS


@pytest.mark.parametrize("word", ["important", "urgent", "priority", "critical"])
def test_each_priority_word_individually_stays_out_of_a_query(word) -> None:
    """One test per word, so removing a word from the set fails a test."""
    request = recognise(f"show me emails about {word} changes")
    assert request.is_readable
    assert word not in " ".join(request.subject_terms).lower()


def test_the_narrow_sender_family_does_not_take_a_long_sentence() -> None:
    """"Anything from X" is recognised only when the sentence ends there.

    Without the anchor the family swallows "do I have anything from Google
    about the meeting tomorrow", which at that length is at least as likely
    to be a calendar question. Mutation testing found the missing test.
    """
    assert recognise("Do I have anything from Google?").family == "anything_from"
    assert recognise("do I have anything from Amazon").family == "anything_from"

    for message in (
        "do I have anything from Google about the meeting tomorrow",
        "do I have anything from the dentist scheduled this week",
        "do I have anything from work on my calendar",
    ):
        assert recognise(message).family != "anything_from", message
