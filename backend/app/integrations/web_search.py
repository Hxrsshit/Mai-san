"""Web search: Mai's first real external capability.

Read-only, one endpoint, one operation. The user supplies a **query**; the
integration constructs the request. There is no URL field anywhere in the
path from a chat message to this module, which is what keeps a research
capability from being an arbitrary fetch capability wearing a different name.

The provider is Brave Search, chosen for the shape that fits the constraints
already in place: one documented host, one GET endpoint, one header-carried
API key, and a JSON response. Nothing here is Brave-specific beyond the host,
the path and the header name -- `parse_results` reads fields by name and
tolerates a different payload shape, so a second provider is a subclass rather
than a rewrite.

**Not configured by default.** No key ships, none is invented, and with none
present the integration reports `NOT_CONFIGURED` and the capability reports
itself unavailable. That is the honest state of this deployment.
"""

from typing import Any, Dict, NamedTuple, Optional, Tuple

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations.base import Integration, OperationSpec
from app.integrations.credentials import (
    CredentialRequirement,
    CredentialType,
)
from app.integrations.errors import (
    CredentialsMissing,
    ProviderForbidden,
    ProviderInvalidResponse,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnauthorized,
    ProviderUnavailable,
    ProviderValidationError,
)
from app.integrations.http_client import SecureHttpClient
from app.integrations.policy import NetworkPolicy, RetryPolicy, TimeoutPolicy
from app.integrations.result import (
    ExternalResult,
    ExternalResultState,
)
from app.integrations.search import (
    MAX_RESULTS,
    normalise_query,
    parse_results,
)

logger = get_logger(__name__)

#: How long "recent" means, for a freshness-marked search. Stage 5E.2.
#:
#: Thirty days is chosen against the measured failure: the stale answer came
#: from an article eleven months old, and a month-wide window still returns
#: release notes and announcements while excluding it. It is a constant rather
#: than a parameter because it is not a thing a caller -- or a model -- should
#: be able to widen.
RECENT_WINDOW_DAYS = 30


class SearchProvider(NamedTuple):
    """Everything that differs between one search API and another.

    A descriptor rather than a subclass, because the differences are data:
    a host, a URL, a verb, a header name and how the query is carried. The
    security properties -- single-host allow-list, no redirects, bounded
    response, credential via the auth header -- are identical for every
    provider and live in one place below.

    Adding a provider means adding an entry here. It does not mean widening
    anything: each descriptor's host is still a constant in application code,
    so no setting, argument or model output can point the client elsewhere.
    """

    name: str
    host: str
    url: str
    #: Brave takes a GET with query parameters; Tavily takes a POST with a
    #: JSON body. This is the one difference that reaches the network policy.
    method: str
    auth_header: str
    #: How the credential is formatted in that header. Brave sends the key
    #: bare, Tavily expects the `Bearer` scheme.
    auth_prefix: str = ""
    #: Whether this provider accepts a recency-scoped news search. Stage 5E.2.
    #:
    #: Declared per provider so the request builder asks the descriptor rather
    #: than testing the provider's name, and so a provider that cannot do it
    #: silently gets a plain search instead of a parameter it would reject.
    supports_recency: bool = False


#: The two providers Mai can talk to. Both single-host, both read-only.
PROVIDERS = {
    "brave": SearchProvider(
        name="brave",
        host="api.search.brave.com",
        url="https://api.search.brave.com/res/v1/web/search",
        method="GET",
        auth_header="X-Subscription-Token",
        # Brave has its own freshness parameter with a different name and
        # vocabulary. Out of scope for Stage 5E.2, which does not add a second
        # provider's request shape; left False so Brave keeps today's request.
        supports_recency=False,
    ),
    "tavily": SearchProvider(
        name="tavily",
        host="api.tavily.com",
        url="https://api.tavily.com/search",
        method="POST",
        auth_header="Authorization",
        auth_prefix="Bearer ",
        supports_recency=True,
    ),
}

DEFAULT_PROVIDER = "tavily"


def resolve_provider(name: str) -> SearchProvider:
    """The configured provider, or raise. Never guesses.

    An unrecognised name is a configuration error and is refused rather than
    defaulted: silently falling back would mean a deployment that thinks it
    is talking to one provider is talking to another, with that provider's
    key.
    """
    key = (name or "").strip().lower()
    if key not in PROVIDERS:
        raise ValueError(
            f"unknown search provider {key!r}; expected one of "
            f"{', '.join(sorted(PROVIDERS))}"
        )
    return PROVIDERS[key]


#: Kept as module constants because tests and the network-boundary docs refer
#: to them. They name the default provider's endpoint.
SEARCH_HOST = PROVIDERS[DEFAULT_PROVIDER].host
SEARCH_URL = PROVIDERS[DEFAULT_PROVIDER].url
AUTH_HEADER = PROVIDERS[DEFAULT_PROVIDER].auth_header

#: Read-only work, so retrying is safe in a way it is not for a send. Still
#: bounded: three attempts, short backoff, and a hard total ceiling.
#:
#: `retry_side_effects` stays False. Search does not need it, and leaving it
#: True here would make the default wrong for whatever is copied from this
#: file next.
SEARCH_RETRIES = RetryPolicy(
    max_attempts=3,
    backoff_seconds=0.5,
    backoff_multiplier=2.0,
    max_backoff_seconds=4.0,
    max_total_seconds=20.0,
)

SEARCH_TIMEOUTS = TimeoutPolicy(
    connect_seconds=5.0, read_seconds=10.0, total_seconds=20.0
)


class WebSearchIntegration(Integration):
    """Search the public web through one provider, one endpoint, one operation."""

    name = "web_search"
    description = "Search the public web through a configured search provider."

    def __init__(
        self,
        credentials=None,
        enabled: bool = True,
        client: Optional[SecureHttpClient] = None,
        transport=None,
        resolve=None,
        provider: Optional[str] = None,
    ) -> None:
        # The client is built from *this integration's* policy, so the host
        # allow-list it enforces is the one declared below. Injectable so
        # tests drive it against a stub transport -- the policy still runs,
        # which is what makes an SSRF test at this boundary meaningful.
        self._provider = resolve_provider(
            provider
            if provider is not None
            else getattr(get_settings(), "SEARCH_PROVIDER", DEFAULT_PROVIDER)
        )
        self._client = client or SecureHttpClient(
            policy=self._policy(self._provider), transport=transport,
            resolve=resolve,
        )
        super().__init__(credentials=credentials, enabled=enabled)

    @property
    def provider(self) -> str:
        """Which third party this instance actually talks to.

        Derived from the resolved descriptor, never stored separately. It was
        a class attribute reading `"brave"` -- correct when Brave was the only
        provider, and silently wrong from the moment a second one was added
        and selected. The value is provenance: it names the external party
        that received the user's query, and it reached both the completion log
        and the integration health report while the request went to Tavily.

        A property rather than a field assigned in `__init__` so there is no
        second copy that can drift from `_provider` again. Every reader --
        this module's own logging, `Integration.credential_requirement`'s
        default, and `app.integrations.health` -- now reads the same
        authoritative source.
        """
        return self._provider.name

    def declare_operations(self) -> Tuple[OperationSpec, ...]:
        return (
            OperationSpec(
                name="search",
                handler=self._search,
                # Read-only. This is what permits retrying, and it is
                # declared rather than assumed.
                has_side_effect=False,
                description="Search the public web for a query.",
            ),
        )

    @property
    def credential_requirement(self) -> CredentialRequirement:
        return CredentialRequirement(
            identifier="web_search.api_key",
            provider=self._provider.name,
            credential_type=CredentialType.API_KEY,
            setting_name="SEARCH_API_KEY",
            required_scopes=frozenset({"search.read"}),
        )

    @staticmethod
    def _policy(provider: Optional[SearchProvider] = None) -> NetworkPolicy:
        """One host, one verb, no redirects, bounded body.

        **The verb is the provider's, and that is a widening worth naming.**
        Stage 4F-C gave research `{"GET"}` on the reasoning that a client
        which could POST could be talked into submitting a form. Tavily's
        search API is POST-only, so a Tavily deployment's research client
        holds POST.

        What makes that acceptable is the line above it: `allowed_hosts` is a
        single constant from the descriptor, and the URL is built in
        `_search` from that same constant. The danger of POST was submitting
        to *arbitrary* destinations, and there is exactly one destination
        available. A test asserts the client still cannot reach anywhere
        else, with either verb.

        Each policy carries only its own provider's verb -- never both -- so
        a Brave deployment's client remains incapable of POST.
        """
        chosen = provider or PROVIDERS[DEFAULT_PROVIDER]
        return NetworkPolicy(
            allowed_hosts=frozenset({chosen.host}),
            allowed_methods=frozenset({chosen.method}),
            timeouts=SEARCH_TIMEOUTS,
            retries=SEARCH_RETRIES,
            # A search API has no reason to redirect. Refusing outright is
            # one fewer thing to get right, and the client would re-check
            # every hop anyway if this were ever turned on.
            follow_redirects=False,
            max_response_bytes=1_000_000,
        )

    @property
    def network_policy(self) -> NetworkPolicy:
        return self._policy(self._provider)

    async def _search(self, arguments: Dict[str, Any]) -> ExternalResult:
        """The one operation. Builds the request; never receives one.

        Note what is constructed here rather than accepted: the URL, the
        method, the headers, and the parameter names. What crosses from the
        caller is a query string and two bounded numbers.
        """
        query = normalise_query(arguments.get("query", ""))
        count = max(1, min(int(arguments.get("max_results", 5)), MAX_RESULTS))
        safe_search = "strict" if arguments.get("safe_search", True) else "moderate"
        # Stage 5E.2. The application's freshness judgement, already made by
        # `app.orchestration.freshness` and carried through the approved
        # payload. Coerced to a bool here: what arrives is whatever survived
        # the tool schema, and this decides a request parameter.
        prefer_recent = bool(arguments.get("prefer_recent", False))

        secret = self._credentials.resolve_secret(self.credential_requirement)
        chosen = self._provider

        # The credential travels as an auth header the client applies. Never
        # in a query string -- those are logged by proxies, kept in provider
        # access logs, and would land in any URL Mai recorded -- and never in
        # the JSON body, which Tavily also accepts but which would put the key
        # somewhere a request dump would show it.
        auth = (chosen.auth_header, f"{chosen.auth_prefix}{secret}")

        if chosen.method == "POST":
            body: Dict[str, Any] = {
                "query": query,
                "max_results": count,
                "search_depth": "basic",
            }
            if prefer_recent and chosen.supports_recency:
                # Measured, not assumed. With the plain request the provider
                # returned five slots filled by one eleven-month-old article
                # and no publication dates at all; with these two parameters
                # the same query returned five distinct sources, all dated.
                #
                # Both are provider-documented parameter names, and both are
                # written here as constants -- neither the caller nor a model
                # can name a topic or widen the window.
                body["topic"] = "news"
                body["days"] = RECENT_WINDOW_DAYS
            response = await self._client.post_json(
                chosen.url, json_body=body, auth_header=auth
            )
        else:
            response = await self._client.get(
                chosen.url,
                params={
                    "q": query,
                    "count": str(count),
                    "safesearch": safe_search,
                },
                auth_header=auth,
            )

        if response.status_code != 200:
            raise _error_for_status(response.status_code)

        payload = _decode(response.content)
        results = parse_results(
            payload, query=query, provider=chosen.name, max_results=count
        )

        logger.info(
            "Web search completed",
            # Deliberately not the query. A search query can name a person, a
            # diagnosis, or an employer, and Stage 3D's rule is that data
            # like that does not go to INFO. The count and the length are
            # enough to debug with.
            extra={
                "integration": self.name,
                "provider": self.provider,
                "result_count": len(results.results),
                "query_chars": len(query),
                "prefer_recent": prefer_recent,
                "latency_ms": response.elapsed_ms,
            },
        )

        return ExternalResult(
            state=ExternalResultState.SUCCESS,
            integration=self.name,
            operation="search",
            summary=(
                f"Found {len(results.results)} result"
                f"{'' if len(results.results) == 1 else 's'}."
            ),
            data=results.as_external_data(),
            provider_status=response.status_code,
        )


def _decode(content: bytes) -> Dict[str, Any]:
    """Parse a JSON body, or refuse.

    A provider returning something unparseable is usually a captive portal,
    an error page or an incident. Refusing beats guessing, and the body is
    not logged -- it can be arbitrarily large and contains whatever the
    provider sent.
    """
    import json

    try:
        payload = json.loads(content.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderInvalidResponse(detail="body") from exc

    if not isinstance(payload, dict):
        raise ProviderInvalidResponse(detail="shape")
    return payload


def _error_for_status(status: int):
    """Map a provider status onto a typed error.

    A table, so a status nobody considered becomes `ProviderUnavailable`
    rather than silently reading as success. Only 429 and 5xx are retryable,
    which the error classes themselves declare.
    """
    if status == 401:
        return ProviderUnauthorized(detail=str(status))
    if status == 403:
        return ProviderForbidden(detail=str(status))
    if status == 404:
        return ProviderNotFound(detail=str(status))
    if status == 422 or status == 400:
        return ProviderValidationError(detail=str(status))
    if status == 429:
        return ProviderRateLimited(detail=str(status))
    return ProviderUnavailable(detail=str(status))


__all__ = [
    "AUTH_HEADER",
    "SEARCH_HOST",
    "SEARCH_RETRIES",
    "SEARCH_TIMEOUTS",
    "SEARCH_URL",
    "WebSearchIntegration",
]
