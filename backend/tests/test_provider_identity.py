"""Stage 5E.2.1: the research provider identifies itself correctly.

`WebSearchIntegration.provider` was a class attribute reading `"brave"` --
correct when Brave was the only provider, and silently wrong from the moment a
second one was added and selected. The value is *provenance*: it names the
external party that received the user's query.

The defect reached two surfaces while requests went to Tavily:

- the search-completion log line (`provider=brave`), and
- the integration health report, which is user-facing.

Expectations here are literals. The point of the test is to disagree with the
implementation when the implementation is wrong, and a test that derives its
expectation from the value under test cannot do that.
"""

import pytest

from app.integrations.web_search import PROVIDERS, WebSearchIntegration


def _integration(provider):
    from app.integrations.credentials import EnvironmentCredentialResolver

    return WebSearchIntegration(
        credentials=EnvironmentCredentialResolver(environ={"SEARCH_API_KEY": "k"}),
        provider=provider,
    )


# --- The identity itself --------------------------------------------------------


@pytest.mark.parametrize("configured, expected", [("tavily", "tavily"), ("brave", "brave")])
def test_the_reported_provider_is_the_one_actually_used(configured, expected) -> None:
    """Literal expectations, one per configurable provider."""
    integration = _integration(configured)
    assert integration.provider == expected


def test_the_stale_default_is_gone() -> None:
    """The specific regression: Tavily configured, `brave` reported."""
    assert _integration("tavily").provider != "brave"
    assert _integration("tavily").provider == "tavily"


def test_identity_matches_the_resolved_descriptor() -> None:
    """One authoritative source, not two that can drift."""
    for name in PROVIDERS:
        integration = _integration(name)
        assert integration.provider == integration._provider.name


def test_identity_matches_the_host_the_request_can_reach() -> None:
    """Provenance and destination must agree.

    A provider identity that disagreed with the network policy would name one
    third party while the bytes went to another -- which is the whole defect,
    stated as a property.
    """
    expected_hosts = {"tavily": "api.tavily.com", "brave": "api.search.brave.com"}
    for name, host in expected_hosts.items():
        integration = _integration(name)
        assert integration.provider == name
        assert integration._client._policy.allowed_hosts == frozenset({host})


# --- The surfaces the defect reached ----------------------------------------------


def test_the_credential_requirement_names_the_actual_provider() -> None:
    assert _integration("tavily").credential_requirement.provider == "tavily"
    assert _integration("brave").credential_requirement.provider == "brave"


def test_the_health_report_names_the_actual_provider() -> None:
    """`app.integrations.health` reads `integration.provider or integration.name`."""
    integration = _integration("tavily")
    assert (integration.provider or integration.name) == "tavily"


@pytest.mark.anyio
async def test_the_completion_log_names_the_actual_provider(caplog) -> None:
    """The line that carried the wrong name. Asserted on the emitted record."""
    import logging

    from tests.support.stub_transport import StubTransport, tavily_payload
    from tests.test_tavily_provider import _integration as built

    transport = StubTransport(payload=tavily_payload())
    integration = built(transport, provider="tavily")

    with caplog.at_level(logging.INFO):
        await integration.ainvoke("search", {"query": "q"})

    logged = [
        record for record in caplog.records
        if "Web search completed" in record.getMessage()
    ]
    assert logged, "the completion line was not emitted"
    assert getattr(logged[0], "provider", None) == "tavily"


# --- A future provider cannot inherit the wrong identity ---------------------------


def test_identity_is_derived_not_stored() -> None:
    """A property, so there is no field a new provider could inherit.

    The defect existed because the identity was a *stored* default. Anything
    that reintroduces one -- a class attribute, an `__init__` assignment, or a
    cache populated on first read -- brings back the same failure for the next
    provider added.

    Checked after *reading* the value, and against every instance attribute
    rather than one named `provider`: mutation testing showed a cache stored
    under a different key passed the narrower check.
    """
    assert isinstance(
        WebSearchIntegration.provider, property
    ), "provider must be derived from the descriptor, not stored"

    integration = _integration("tavily")
    assert integration.provider == "tavily"

    stored = {
        key: value
        for key, value in vars(integration).items()
        if isinstance(value, str) and value == "tavily"
    }
    assert stored == {}, f"the identity was cached in {sorted(stored)}"


def test_a_new_provider_reports_its_own_name() -> None:
    """A descriptor added later identifies itself without further code.

    The identity is read **before** the descriptor changes, which is the
    realistic drift: something logs the provider, and only afterwards does the
    resolved descriptor differ. A cache populated on first read survives a
    test that reads only after the swap -- mutation testing found exactly that
    hole in the first version of this test.
    """
    integration = _integration("tavily")
    assert integration.provider == "tavily"  # first read, before any change

    integration._provider = PROVIDERS["tavily"]._replace(name="newprovider")
    assert integration.provider == "newprovider", "identity did not follow the descriptor"


def test_every_declared_provider_reports_its_declared_name() -> None:
    """No provider in the table can report a different name than it declares."""
    for name, descriptor in PROVIDERS.items():
        assert descriptor.name == name, f"{name} declares {descriptor.name}"
        assert _integration(name).provider == descriptor.name
