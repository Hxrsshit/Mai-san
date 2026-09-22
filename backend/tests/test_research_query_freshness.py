"""Stage 5E.1/5E.2: query construction and freshness propagation.

The Stage 5E audit reproduced a measured failure: the question

    "what is the latest model by Claude, what is the latest model of opus ?"

became the search query

    "latest model by Claude, what is the latest model of opus"

which the provider answered with five slots filled by **one** eleven-month-old
article, carrying no publication dates at all.

Expected values here are written as literals, never derived from the
implementation's own constants. Stage 5D.2's mutation run found two tests that
computed their fixtures from the very constant they were guarding, so raising
the constant raised the fixture and the test could never fail.
"""

from datetime import datetime, timezone

import pytest

pytestmark_anyio = pytest.mark.anyio

from app.integrations.search import (
    canonical_url,
    parse_published_at,
    parse_results,
)
from app.orchestration.freshness import assess

# --- 5E.1: compound query construction ---------------------------------------


def test_the_measured_compound_question_is_reduced() -> None:
    """The exact incident string, with a literal expectation."""
    result = assess(
        "what is the latest model by Claude, what is the latest model of opus ?"
    )
    assert result.subject == "latest model by Claude opus"
    # The second interrogative frame is gone, which is the whole defect.
    assert "what is" not in result.subject
    assert "," not in result.subject


@pytest.mark.parametrize(
    "message, expected",
    [
        # Simple questions must take exactly the path they took before.
        ("what is the latest Claude model?", "latest Claude model"),
        ("what is the latest Opus model?", "latest Opus model"),
        ("what is the latest model from Anthropic?", "latest model from Anthropic"),
        ("what is the current OpenAI model?", "current OpenAI model"),
        ("what is the latest news about OpenAI", "latest news about OpenAI"),
        # Compound questions merge.
        ("what is the latest iPhone and what is the latest MacBook?",
         "latest iPhone MacBook"),
        ("what is the current repo rate, what is the current inflation rate?",
         "current repo rate inflation"),
    ],
)
def test_subjects_are_literal_pinned(message, expected) -> None:
    assert assess(message).subject == expected


def test_a_simple_question_is_untouched_by_the_compound_path() -> None:
    """No behaviour change where there is no second question."""
    single = "what is the latest Claude model?"
    assert assess(single).subject == "latest Claude model"


def test_nothing_is_invented() -> None:
    """Every word of the query is a word the user typed.

    The audit's own "clean query" variant added the word "Anthropic", which
    the user never wrote. That is a rewrite, not a reduction, and this stage
    deliberately does not do it.
    """
    message = "what is the latest model by Claude, what is the latest model of opus ?"
    typed = set(message.lower().replace(",", " ").replace("?", " ").split())
    for word in assess(message).subject.lower().split():
        assert word in typed, word


def test_the_user_entities_survive_the_merge() -> None:
    """Both subjects of a compound question reach the query."""
    subject = assess(
        "what is the latest model by Claude, what is the latest model of opus ?"
    ).subject.lower()
    assert "claude" in subject
    assert "opus" in subject


def test_a_repeated_clause_adds_nothing() -> None:
    result = assess("what is the latest Claude model, what is the latest Claude model?")
    assert result.subject == "latest Claude model"


# --- 5E.2: freshness reaches the approved payload -----------------------------


def test_the_freshness_path_marks_the_payload_as_recent() -> None:
    """`propose_current_information` is the only caller that sets it."""
    import inspect

    from app.research.service import ResearchService

    source = inspect.getsource(ResearchService._propose_query)
    assert '"prefer_recent": from_freshness' in source


def test_the_tool_argument_is_a_bool_with_a_safe_default() -> None:
    from app.tools.catalog import WebSearchArguments

    assert WebSearchArguments(query="x").prefer_recent is False
    assert WebSearchArguments(query="x", prefer_recent=True).prefer_recent is True


def test_the_argument_crosses_the_tool_boundary() -> None:
    from app.execution.web_search_tool import WebSearchTool

    tool = WebSearchTool()
    arguments = tool.validate_arguments({"query": "x", "prefer_recent": True})
    assert tool.build_operation_arguments(arguments)["prefer_recent"] is True


def test_only_a_provider_that_supports_recency_declares_it() -> None:
    from app.integrations.web_search import PROVIDERS

    assert PROVIDERS["tavily"].supports_recency is True
    assert PROVIDERS["brave"].supports_recency is False


def test_the_recency_window_is_a_literal_constant() -> None:
    """Pinned so widening the window is a decision, not a drift."""
    from app.integrations.web_search import RECENT_WINDOW_DAYS

    assert RECENT_WINDOW_DAYS == 30


@pytest.mark.anyio
async def test_a_provider_without_recency_support_gets_no_recency_parameters() -> None:
    """Capability is asked of the descriptor, not of the provider's name.

    Brave takes a different freshness parameter with a different vocabulary,
    which Stage 5E.2 does not add. Sending Tavily's `topic`/`days` to it would
    be sending a parameter it never declared -- so the descriptor gates it,
    and this asserts on the real outbound request rather than on source text.
    """
    from tests.support.stub_transport import StubTransport, brave_payload
    from tests.test_tavily_provider import _integration

    transport = StubTransport(payload=brave_payload())
    integration = _integration(transport, provider="brave")

    await integration.ainvoke(
        "search", {"query": "q", "max_results": 3, "prefer_recent": True}
    )

    # Brave is a GET: the recency vocabulary must appear nowhere in the URL.
    url = transport.connections[0]
    assert "topic=" not in url
    assert "days=" not in url
    assert "news" not in url


@pytest.mark.anyio
async def test_a_post_provider_without_recency_support_still_gets_nothing() -> None:
    """The capability guard, exercised where it can actually bite.

    Mutation testing showed that deleting `and chosen.supports_recency` is
    *currently* equivalent: the recency block lives inside the POST branch,
    and today the only POST provider is also the only one declaring recency
    support, so the two conditions coincide. An equivalent mutation is not a
    reason to delete a guard -- it is a reason to test the case the guard
    exists for.

    So this builds the case that does not exist yet: a POST provider that has
    not declared recency support. Without the guard it would receive Tavily's
    `topic`/`days`, which it never agreed to accept.
    """
    import json as _json

    from app.integrations import web_search as ws
    from tests.support.stub_transport import StubTransport, tavily_payload
    from tests.test_tavily_provider import _integration

    hypothetical = ws.PROVIDERS["tavily"]._replace(
        name="postprovider", supports_recency=False
    )
    transport = StubTransport(payload=tavily_payload())
    integration = _integration(transport)
    # Swap the descriptor only; the policy and client are already built, and
    # the host is unchanged, so nothing about the network boundary moves.
    integration._provider = hypothetical

    await integration.ainvoke(
        "search", {"query": "q", "max_results": 3, "prefer_recent": True}
    )

    body = _json.loads(transport.bodies[0].decode())
    assert "topic" not in body
    assert "days" not in body


def test_the_clause_bound_is_a_literal() -> None:
    """Widening it must be a decision, not a drift.

    Stage 5D.2's mutation run found two bound tests that computed their
    fixtures from the constant they were guarding, so raising the constant
    raised the fixture and the test could never fail. This one is a literal,
    and the behavioural test below uses a literal number of clauses.
    """
    from app.orchestration.freshness import MAX_CLAUSES

    assert MAX_CLAUSES == 4


def test_more_clauses_than_the_bound_are_not_all_merged() -> None:
    """Six questions in one message; the query stays bounded."""
    message = (
        "what is the latest alpha, what is the latest bravo, "
        "what is the latest charlie, what is the latest delta, "
        "what is the latest echo, what is the latest foxtrot?"
    )
    subject = assess(message).subject.lower()
    # The first four clauses' subjects are present; the fifth and sixth are not.
    assert "alpha" in subject
    assert "delta" in subject
    assert "echo" not in subject
    assert "foxtrot" not in subject


# --- 5E.2: publication dates --------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected_year, expected_month",
    [
        ("Mon, 31 Aug 2026 10:23:13 GMT", 2026, 8),
        ("Tue, 22 Sep 2026 16:00:00 GMT", 2026, 9),
        ("2026-09-22T16:00:00Z", 2026, 9),
        ("2026-09-22T16:00:00+00:00", 2026, 9),
    ],
)
def test_a_provider_date_is_parsed(raw, expected_year, expected_month) -> None:
    parsed = parse_published_at(raw)
    assert parsed is not None
    assert parsed.year == expected_year
    assert parsed.month == expected_month


@pytest.mark.parametrize(
    "raw", ["", "   ", "not a date", "yesterday", None, 12345, {"a": 1}]
)
def test_an_unreadable_date_is_none_never_a_guess(raw) -> None:
    """An invented date would be evidence metadata Mai made up."""
    assert parse_published_at(raw) is None


def test_a_date_is_preserved_through_parsing() -> None:
    payload = {"results": [{
        "url": "https://example.com/a", "title": "A", "content": "c",
        "published_date": "Tue, 22 Sep 2026 16:00:00 GMT",
    }]}
    results = parse_results(payload, query="q", provider="tavily")
    assert results.results[0].published_at is not None
    assert results.results[0].published_at.year == 2026


def test_a_missing_date_stays_missing() -> None:
    payload = {"results": [
        {"url": "https://example.com/a", "title": "A", "content": "c"}
    ]}
    results = parse_results(payload, query="q", provider="tavily")
    assert results.results[0].published_at is None


def test_a_date_is_rendered_for_synthesis() -> None:
    payload = {"results": [{
        "url": "https://example.com/a", "title": "A", "content": "c",
        "published_date": "Tue, 22 Sep 2026 16:00:00 GMT",
    }]}
    block = parse_results(payload, query="q", provider="tavily").as_external_data()
    assert "Published: 2026-09-22" in block.content


def test_no_date_line_is_rendered_when_there_is_no_date() -> None:
    payload = {"results": [
        {"url": "https://example.com/a", "title": "A", "content": "c"}
    ]}
    block = parse_results(payload, query="q", provider="tavily").as_external_data()
    assert "Published:" not in block.content


def test_a_date_is_never_inferred_from_a_url() -> None:
    """The URL carries 2025/11/24; the result must still have no date."""
    payload = {"results": [{
        "url": "https://www.cnbc.com/2025/11/24/article.html",
        "title": "A", "content": "c",
    }]}
    results = parse_results(payload, query="q", provider="tavily")
    assert results.results[0].published_at is None


# --- Deduplication (the measured duplicate) -----------------------------------


#: The exact shape the provider returned for the failing query: one article,
#: five slots, differing only by tracking parameters and fragments.
MEASURED_DUPLICATE_PAYLOAD = {"results": [
    {"url": "https://www.cnbc.com/2025/11/24/anthropic-unveils-claude-opus-4point5.html?msockid=3cd6de8d",
     "title": "Anthropic unveils Claude Opus 4.5", "content": "x"},
    {"url": "https://www.cnbc.com/2025/11/24/anthropic-unveils-claude-opus-4point5.html?msockid=aaaaaaaa",
     "title": "Anthropic unveils Claude Opus 4.5", "content": "x"},
    {"url": "https://www.cnbc.com/2025/11/24/anthropic-unveils-claude-opus-4point5.html#MainContent",
     "title": "Anthropic unveils Claude Opus 4.5", "content": "x"},
    {"url": "https://www.cnbc.com/2025/11/24/anthropic-unveils-claude-opus-4point5.html",
     "title": "Anthropic unveils Claude Opus 4.5 - CNBC", "content": "x"},
    {"url": "https://www.anthropic.com/news/claude-opus",
     "title": "Anthropic", "content": "y"},
]}


def test_the_measured_duplicate_payload_collapses_to_two_documents() -> None:
    """Five slots, two documents. Literal expectation."""
    results = parse_results(
        MEASURED_DUPLICATE_PAYLOAD, query="q", provider="tavily"
    )
    assert len(results.results) == 2
    assert {r.domain for r in results.results} == {"www.cnbc.com", "www.anthropic.com"}


def test_genuinely_different_pages_are_not_merged() -> None:
    """The failure direction is to keep a duplicate, never to lose an article."""
    payload = {"results": [
        {"url": "https://example.com/articles?page=2", "title": "A", "content": "x"},
        {"url": "https://example.com/articles?page=3", "title": "B", "content": "y"},
        {"url": "https://example.com/other", "title": "C", "content": "z"},
    ]}
    assert len(parse_results(payload, query="q", provider="t").results) == 3


@pytest.mark.parametrize(
    "left, right, same",
    [
        ("https://a.com/x?msockid=1", "https://a.com/x", True),
        ("https://a.com/x#frag", "https://a.com/x", True),
        ("https://a.com/x/", "https://a.com/x", True),
        ("https://A.COM/x", "https://a.com/x", True),
        ("https://a.com/x?utm_source=t", "https://a.com/x", True),
        ("https://a.com/x?page=2", "https://a.com/x?page=3", False),
        ("https://a.com/x", "https://a.com/y", False),
        ("https://a.com/x", "https://b.com/x", False),
    ],
)
def test_url_identity(left, right, same) -> None:
    assert (canonical_url(left) == canonical_url(right)) is same


def test_the_shown_url_is_never_the_canonical_one() -> None:
    """Attribution uses the link the provider returned, unrewritten."""
    original = "https://www.cnbc.com/2025/11/24/a.html?msockid=3cd6de8d"
    payload = {"results": [{"url": original, "title": "A", "content": "x"}]}
    results = parse_results(payload, query="q", provider="tavily")
    assert results.results[0].url == original
    assert original in results.as_external_data().content
