"""The provider gateway: which model backend Mai talks to, and how it authenticates.

Mai already had a provider-agnostic interface -- `LLMProvider` and
`LLMResponse` in `base.py`, unchanged since Stage 1. What this module adds is
the part that interface never covered: **which** provider is in use, **how**
it authenticates, and whether it may be used at all.

Three modes, and exactly one is active
--------------------------------------

    groq                API key      -> api.groq.com
    anthropic_api       API key      -> api.anthropic.com
    claude_subscription subscription -> UNAVAILABLE, see below

The active mode comes from `LLM_PROVIDER`, which is operator configuration.
Nothing else selects it: not a user message, not model output, not a search
result, not a memory. A test asserts each of those.

**There is no fallback, and that is a security property rather than an
omission.** A provider that quietly failed over to another would send the
user's conversation to a company they did not choose, and would bill an API
account they may not have meant to use. A configured provider that fails
produces an error.
"""

import enum
from typing import Dict, FrozenSet, NamedTuple, Optional

from app.core.errors import MaiError


class AuthMode(str, enum.Enum):
    """How a provider proves who it is. Never the credential itself."""

    API_KEY = "api_key"
    #: A Claude Pro/Max plan, through the Agent SDK. Declared so the concept
    #: exists and can be reported truthfully; no provider uses it.
    SUBSCRIPTION = "subscription"


class ProviderMode(str, enum.Enum):
    """The providers Mai knows about. A closed set."""

    GROQ = "groq"
    ANTHROPIC_API = "anthropic_api"
    CLAUDE_SUBSCRIPTION = "claude_subscription"


class ProviderSpec(NamedTuple):
    """Everything the application knows about one provider, before building it.

    `settings_prefix` is explicit rather than derived from the mode name.
    Deriving it gave `ANTHROPIC_API_API_KEY` for `anthropic_api`, which is not
    what anyone would write in a `.env` file -- and a convention that produces
    a surprising name is a convention that will be worked around.
    """

    mode: ProviderMode
    settings_prefix: str
    auth_mode: AuthMode
    #: The single host this provider's traffic may reach. Enforced by
    #: `NetworkPolicy`; recorded here so it can be reported and tested.
    host: str
    available: bool
    #: Why not, when `available` is False. Shown to an operator, never to a
    #: model, and never empty for an unavailable provider.
    unavailable_reason: str = ""


#: Why `claude_subscription` is not available, in the two independent senses
#: that each would be sufficient on its own.
#:
#: **1. Anthropic does not permit it.** The Agent SDK documentation states, in
#: both the overview and the authentication section of the quickstart: "Unless
#: previously approved, Anthropic does not allow third party developers to
#: offer claude.ai login or rate limits for their products, including agents
#: built on the Claude Agent SDK. Use the API key authentication methods
#: described in the Quickstart instead." Mai is a third-party product. This is
#: a permission boundary, not a technical one, and no amount of engineering
#: makes it appropriate to cross.
#:
#: **2. Mai could not police its network.** The Agent SDK drives a Claude Code
#: binary as a subprocess. Its `Transport` abstraction is the message channel
#: between the SDK and that subprocess -- `connect`, `write`, `read_messages`,
#: `close` -- not an HTTP transport. Mai's `SecureHttpClient` and
#: `NetworkPolicy` cannot govern where that subprocess connects, and Stage
#: 4F-F's own rule is that a provider able to make external requests outside
#: Mai's security model must not be marked available.
#:
#: Either reason alone settles it. Recorded together so that if the first ever
#: changes, the second is still waiting.
CLAUDE_SUBSCRIPTION_UNAVAILABLE = (
    "Anthropic does not permit third-party products to authenticate with a "
    "claude.ai login, and the Agent SDK's outbound traffic could not be "
    "placed behind Mai's network policy in any case."
)


#: The closed provider table. Adding a provider means adding an entry here.
PROVIDERS: Dict[ProviderMode, ProviderSpec] = {
    ProviderMode.GROQ: ProviderSpec(
        mode=ProviderMode.GROQ,
        settings_prefix="GROQ",
        auth_mode=AuthMode.API_KEY,
        host="api.groq.com",
        available=True,
    ),
    ProviderMode.ANTHROPIC_API: ProviderSpec(
        mode=ProviderMode.ANTHROPIC_API,
        settings_prefix="ANTHROPIC",
        auth_mode=AuthMode.API_KEY,
        host="api.anthropic.com",
        available=True,
    ),
    ProviderMode.CLAUDE_SUBSCRIPTION: ProviderSpec(
        mode=ProviderMode.CLAUDE_SUBSCRIPTION,
        settings_prefix="CLAUDE_SUBSCRIPTION",
        auth_mode=AuthMode.SUBSCRIPTION,
        # No host: nothing is permitted to connect anywhere on its behalf.
        host="",
        available=False,
        unavailable_reason=CLAUDE_SUBSCRIPTION_UNAVAILABLE,
    ),
}

#: Every host any provider may reach, for the network audit.
PERMITTED_PROVIDER_HOSTS: FrozenSet[str] = frozenset(
    spec.host for spec in PROVIDERS.values() if spec.host
)


class ProviderUnavailable(MaiError):
    """The configured provider exists but may not be used.

    Distinct from `UnknownProviderError`: "this is not a provider" and "this
    is a provider you may not use" are different operator problems, and
    reporting the second as the first sends someone looking for a typo.
    """

    status_code = 503
    code = "llm_provider_unavailable"


class UnknownProviderMode(MaiError):
    status_code = 500
    code = "unknown_llm_provider"


def resolve_mode(name: str) -> ProviderMode:
    """The configured mode, or raise. Never guesses, never defaults.

    An unrecognised value is a configuration error. Falling back to a default
    would start a provider the operator did not choose -- with that provider's
    credentials and that provider's bill.
    """
    key = (name or "").strip().lower()
    for mode in ProviderMode:
        if mode.value == key:
            return mode
    raise UnknownProviderMode(
        f"Unknown LLM_PROVIDER {name!r}. Available: "
        f"{', '.join(mode.value for mode in ProviderMode)}."
    )


def spec_for(mode: ProviderMode) -> ProviderSpec:
    return PROVIDERS[mode]


def require_available(mode: ProviderMode) -> ProviderSpec:
    """The spec, if the provider may be used. Raises if it may not."""
    spec = PROVIDERS[mode]
    if not spec.available:
        raise ProviderUnavailable(
            f"The {mode.value} provider is not available. {spec.unavailable_reason}"
        )
    return spec


__all__ = [
    "CLAUDE_SUBSCRIPTION_UNAVAILABLE",
    "PERMITTED_PROVIDER_HOSTS",
    "PROVIDERS",
    "AuthMode",
    "ProviderMode",
    "ProviderSpec",
    "ProviderUnavailable",
    "UnknownProviderMode",
    "require_available",
    "resolve_mode",
    "spec_for",
]
