"""The search result contract.

Everything here exists because **a provider's response is not trustworthy**.
It arrives over the network, it contains text written by strangers, and the
provider itself could be compromised or simply buggy. So nothing is passed
through: each field is extracted by name, bounded, and validated, and anything
that fails validation is dropped rather than repaired.

Dropped rather than repaired, specifically. A result whose URL is
`javascript:alert(1)` is not rewritten into something harmless -- it is
discarded, because a repaired result is a result Mai would then attribute a
claim to, and the attribution would be to something that was never there.
"""

from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.integrations.result import DataClassification, ExternalData

logger = get_logger(__name__)

#: Bounds on what a provider may return. Every one of these is a ceiling on
#: prompt budget as much as on memory: search results go into a context
#: window, and an unbounded snippet is an unbounded prompt.
MAX_RESULTS = 10
MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 500
MAX_URL_CHARS = 500
MAX_QUERY_CHARS = 300

#: Schemes a result URL may use. A result is something a person might click,
#: so `javascript:`, `data:` and `file:` are refused outright.
_RESULT_SCHEMES = frozenset({"https", "http"})


class SearchResult(BaseModel):
    """One result, with only the fields a reader or the model needs.

    No provider identifiers, no ranking scores, no tracking parameters, no
    raw HTML. Those are debugging material at best and fingerprinting at
    worst, and every one of them would cost prompt budget for nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(default="", max_length=MAX_TITLE_CHARS)
    url: str = Field(default="", max_length=MAX_URL_CHARS)
    #: The host, kept separately so attribution can be shown compactly and so
    #: a reader can see the source without parsing a URL.
    domain: str = Field(default="", max_length=100)
    snippet: str = Field(default="", max_length=MAX_SNIPPET_CHARS)
    published_at: Optional[datetime] = None


class SearchResults(BaseModel):
    """A bounded set of results for one query.

    Carries the query back deliberately: a caller comparing what was asked
    against what returned is how "Mai searched for something else" becomes
    visible rather than invisible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(default="", max_length=MAX_QUERY_CHARS)
    results: Tuple[SearchResult, ...] = ()
    #: How many the provider offered before bounding, so a truncated set is
    #: recognisable as truncated.
    total_available: int = 0
    provider: str = Field(default="", max_length=64)

    def as_external_data(self) -> ExternalData:
        """Render the results as labelled untrusted content.

        The rendering is deliberately plain and deliberately *fenced*. Each
        result is introduced by its own source line, so a snippet that says
        "IMPORTANT SYSTEM MESSAGE" is visibly a snippet belonging to a named
        domain rather than a floating instruction.

        `ExternalData` cannot be constructed as anything but untrusted, so
        the label travels with this text wherever it goes next.
        """
        lines: List[str] = []
        for index, result in enumerate(self.results, start=1):
            lines.append(
                f"[{index}] {_flatten(result.title)} — {_flatten(result.domain)}"
            )
            lines.append(f"    URL: {_flatten(result.url)}")
            if result.published_at is not None:
                # Stage 5E.2. Date-stamped so the model can say *when* a
                # source said something. Rendered only when the provider
                # supplied one: an absent date stays absent rather than
                # becoming "unknown", which would read as a fact about the
                # source rather than about the search.
                #
                # This is evidence metadata, not a ranking. Nothing here
                # decides that newer is better; that decision does not exist
                # in this stage.
                lines.append(
                    f"    Published: {result.published_at.date().isoformat()}"
                )
            if result.snippet:
                lines.append(f"    Snippet: {_flatten(result.snippet)}")

        return ExternalData(
            source="web_search",
            content="\n".join(lines),
            # A search query and its results reflect what the user wanted to
            # know. Public pages, private interest -- so PRIVATE, not PUBLIC.
            classification=DataClassification.PRIVATE,
        )


def normalise_query(raw: str) -> str:
    """Validate a search query. Deterministic, and deliberately light.

    Whitespace is collapsed and control characters are dropped, because
    neither carries meaning in a query and both can forge structure in a log
    line or a rendered block. Nothing else is touched: quotes, operators,
    minus signs, non-Latin scripts and punctuation are all ordinary parts of
    real searches, and stripping them would silently search for something the
    user did not ask for.

    No model is involved. Asking an LLM to sanitise a query would make the
    query's meaning depend on a model's judgement, which is exactly the
    authority separation the earlier stages exist to maintain.
    """
    if not isinstance(raw, str):
        raise ValueError("query must be a string")

    # Control characters become spaces rather than being deleted: deleting
    # them would silently join two words that were separated, changing what
    # was searched for. The whitespace collapse below then tidies up.
    cleaned = "".join(
        character if character.isprintable() else " " for character in raw
    )
    cleaned = " ".join(cleaned.split())

    if not cleaned:
        raise ValueError("query is empty")
    return cleaned[:MAX_QUERY_CHARS]


def safe_result_url(raw: Any) -> str:
    """A result URL, or `""` if it is not one Mai will show.

    Returns rather than raises: one bad URL among ten results should drop
    that result, not the search.
    """
    if not isinstance(raw, str):
        return ""

    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_URL_CHARS:
        return ""
    # A control character in a URL is either an encoding bug or an attempt to
    # break the line it will be rendered on.
    if any(not character.isprintable() for character in candidate):
        return ""

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""

    if parsed.scheme.lower() not in _RESULT_SCHEMES:
        return ""
    if not parsed.hostname:
        return ""
    return candidate


#: Query parameters that identify a *visit*, not a document.
#:
#: The measured failure returned the same CNBC article four times, differing
#: only by `msockid` and a fragment. Stripping these is what makes five result
#: slots buy five documents.
#:
#: A closed list, and deliberately short: a parameter not named here is kept,
#: so two genuinely different pages are never merged. The failure direction is
#: to keep a duplicate, not to lose an article.
_TRACKING_PARAMS = frozenset({
    "msockid", "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "utm_id", "gclid", "fbclid", "mc_cid", "mc_eid",
    "ref", "ref_src", "source", "cmpid", "ncid", "spm", "igshid",
    "_hsenc", "_hsmi", "icid", "sref",
})


def canonical_url(url: str) -> str:
    """The identity of a document, for deduplication only. Stage 5E.3-lite.

    Drops the fragment and known tracking parameters, lowercases the host, and
    removes a trailing slash. **Never used as the URL Mai shows or cites** --
    attribution uses the URL the provider returned, because a rewritten link is
    a link the user did not receive.

    Deliberately conservative. Ordinary query parameters are kept, since
    `?page=2` and `?id=7` are different documents, and merging them would lose
    an article rather than a duplicate.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url

    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower()
            + (f":{parsed.port}" if parsed.port else ""),
            path,
            "",
            urlencode(kept),
            "",
        )
    )


def parse_published_at(value: Any) -> Optional[datetime]:
    """Read a provider's publication date, or None. Never raises.

    Providers send several shapes: RFC 2822 (`Mon, 31 Aug 2026 10:23:13 GMT`),
    ISO 8601, and ISO with a `Z`. Each is tried by name; anything unrecognised
    yields None.

    None is the honest answer for an unparseable date, and it is important
    that it stays None: an invented or inferred date would be evidence
    metadata Mai made up, and the next stage's ranking would act on it. Dates
    are never derived from a URL or from article text.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_results(
    payload: Dict[str, Any],
    query: str,
    provider: str,
    max_results: int = MAX_RESULTS,
) -> SearchResults:
    """Extract results from a provider payload, field by field.

    Never `SearchResult(**item)`. Each field is read by name and bounded, so a
    provider that adds a field, renames one, or returns something of the wrong
    type produces a poorer result rather than an error or an injection.

    A result with no usable URL is dropped. Mai must not attribute a claim to
    a source it cannot name.
    """
    raw_results = payload.get("web", {}).get("results")
    if not isinstance(raw_results, list):
        raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raw_results = []

    collected: List[SearchResult] = []
    seen: set = set()
    duplicates = 0
    for item in raw_results:
        if len(collected) >= min(max_results, MAX_RESULTS):
            break
        if not isinstance(item, dict):
            continue

        url = safe_result_url(item.get("url"))
        if not url:
            continue

        # Stage 5E.2/5E.3-lite. One document per slot. The measured failure
        # filled all five slots with one article, so `result_count` reported
        # five sources while the evidence set was one.
        identity = canonical_url(url)
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)

        collected.append(
            SearchResult(
                title=_bounded(item.get("title"), MAX_TITLE_CHARS),
                url=url,
                domain=_domain_of(url),
                # Providers name this field differently: Brave sends
                # `description`, Tavily sends `content`. Read by name, in
                # order, so an unfamiliar provider yields a result with no
                # snippet rather than an exception or a wrong field.
                snippet=_bounded(
                    item.get("description")
                    or item.get("snippet")
                    or item.get("content"),
                    MAX_SNIPPET_CHARS,
                ),
                # Stage 5E.2. Read by name like every other field. Providers
                # differ: Tavily sends `published_date`, and only for a news
                # search. Absent stays absent.
                published_at=parse_published_at(
                    item.get("published_date")
                    or item.get("published")
                    or item.get("page_age")
                ),
            )
        )

    dropped = len(raw_results) - len(collected)
    if dropped > 0:
        logger.info(
            "Search results were bounded or dropped",
            # Counts only. Never a title, a URL or the query itself.
            extra={
                "provider": provider,
                "dropped": dropped,
                "duplicates": duplicates,
                "dated": sum(1 for r in collected if r.published_at is not None),
            },
        )

    return SearchResults(
        query=query[:MAX_QUERY_CHARS],
        results=tuple(collected),
        total_available=len(raw_results),
        provider=provider,
    )


def _bounded(value: Any, limit: int) -> str:
    """A flattened, length-bounded string, or `""`.

    Flattened for the same reason retrieved memories are in Stage 3B: a
    newline inside a snippet could forge the structure of the block it is
    rendered into, and a search snippet is written by whoever owns the page.
    """
    if not isinstance(value, str):
        return ""
    return _flatten(value)[:limit]


def _flatten(text: str) -> str:
    return " ".join((text or "").split())


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "")[:100]
    except ValueError:
        return ""


__all__ = [
    "MAX_QUERY_CHARS",
    "MAX_RESULTS",
    "MAX_SNIPPET_CHARS",
    "MAX_TITLE_CHARS",
    "MAX_URL_CHARS",
    "SearchResult",
    "SearchResults",
    "normalise_query",
    "parse_results",
    "canonical_url",
    "parse_published_at",
    "safe_result_url",
]
