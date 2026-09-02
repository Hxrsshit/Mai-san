"""The credential boundary.

One rule, and everything here exists to make it structural rather than
remembered:

    **A credential is never a tool argument, and never reaches the model.**

A tool argument is user input. It arrives through an HTTP request, it is
echoed into an approval prompt, it is fingerprinted, and it is stored in the
`executions` table. A secret in that path would be persisted in four places
and shown to a human in one. So credentials travel a different road entirely:
resolved from configuration at dispatch time, handed to the integration, and
never returned to anything that renders.

What this stage does **not** build: a secrets vault, an encryption scheme, or
OAuth. `EnvironmentCredentialResolver` reads configuration, which is
appropriate for a single-user local deployment and is **not** appropriate for
production multi-user use -- see the module note at the bottom.
"""

import enum
import os
from datetime import datetime, timezone
from typing import FrozenSet, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.integrations.errors import (
    CredentialScopeInsufficient,
    CredentialsExpired,
    CredentialsMissing,
)

logger = get_logger(__name__)


class CredentialType(str, enum.Enum):
    """How a provider expects to be authenticated.

    Enumerated so an integration declares its shape rather than the resolver
    guessing from what it finds. `OAUTH2` is representable now and
    unimplemented: Stage 4F-A must be able to grow into it without the
    abstraction changing shape.
    """

    NONE = "none"
    API_KEY = "api_key"
    OAUTH2 = "oauth2"


class CredentialStatus(str, enum.Enum):
    """Whether usable access exists right now."""

    MISSING = "missing"
    AVAILABLE = "available"
    EXPIRED = "expired"
    #: Present, but the granted scopes do not cover what was asked for.
    INSUFFICIENT_SCOPE = "insufficient_scope"


class CredentialRequirement(BaseModel):
    """What an integration needs in order to work.

    Declared by the integration in code. Not configurable by a request, and
    not inferred: an integration states its own requirement, and the resolver
    answers it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: A stable identifier for this credential, e.g. `example.api_key`.
    #: Names the credential, never contains one.
    identifier: str = Field(..., min_length=1, max_length=64)
    provider: str = Field(..., min_length=1, max_length=64)
    credential_type: CredentialType = CredentialType.API_KEY

    #: The configuration key the value would be read from. A *name*, so it is
    #: safe to log and safe to show in a health check.
    setting_name: Optional[str] = Field(default=None, max_length=64)

    #: Scopes this integration needs. Empty means none are modelled yet.
    #:
    #: Scoped from the start, deliberately. "Google access" as a single
    #: all-or-nothing permission is how a read-only calendar integration ends
    #: up able to send mail; `gmail.readonly` and `gmail.send` must be
    #: different grants before the first OAuth integration exists, because
    #: retrofitting scope boundaries onto issued tokens is not possible.
    required_scopes: FrozenSet[str] = frozenset()

    #: Which account this belongs to. Single-user today, so `"default"` --
    #: but the field exists now so that adding users later is a change of
    #: value rather than a change of shape. See the multi-user note below.
    account: str = Field(default="default", max_length=64)


class CredentialState(BaseModel):
    """The answer to "is there usable access?" -- and nothing more.

    **Carries no secret.** There is no `value`, `token`, `key` or `secret`
    field, so this object is safe to log, to return from a health check, and
    to include in a capability report. A test pins the field set.

    The secret itself is returned only by `resolve_secret`, which hands it
    straight to an integration and is never called by anything that renders.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: str
    provider: str
    account: str = "default"
    credential_type: CredentialType = CredentialType.API_KEY
    status: CredentialStatus = CredentialStatus.MISSING

    #: For OAuth later. Absent for an API key, which does not expire on a
    #: schedule the application can see.
    expires_at: Optional[datetime] = None
    granted_scopes: Tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.status is CredentialStatus.AVAILABLE


class CredentialResolver:
    """Where credentials come from. One method returns a secret; one does not.

    The split is the design. `describe` answers "is there access?" and returns
    a `CredentialState` with no secret in it -- that is what health checks,
    capability reporting and logs use. `resolve_secret` returns the value
    itself and is called only by an integration that is about to authenticate.
    """

    def describe(self, requirement: CredentialRequirement) -> CredentialState:
        raise NotImplementedError

    def resolve_secret(self, requirement: CredentialRequirement) -> str:
        """The secret. Raises rather than returning a sentinel.

        Never returns `""` or `None` for a missing credential: an empty string
        would be sent to a provider as if it were a key, producing a confusing
        401 instead of a clear local refusal.
        """
        raise NotImplementedError


class EnvironmentCredentialResolver(CredentialResolver):
    """Reads credentials from process configuration.

    Appropriate for a single-user local deployment, where the operator running
    Mai is the person whose credentials these are. **Not** appropriate for a
    multi-user or hosted deployment -- see the note at the bottom of this
    module.
    """

    def __init__(self, settings=None, environ=None) -> None:
        self._settings = settings
        self._environ = environ if environ is not None else os.environ

    def describe(self, requirement: CredentialRequirement) -> CredentialState:
        """Report availability without reading the value into anything.

        The secret is read here to test for presence and then dropped. It is
        not stored on the returned object, not logged, and not returned.
        """
        if requirement.credential_type is CredentialType.NONE:
            return CredentialState(
                identifier=requirement.identifier,
                provider=requirement.provider,
                account=requirement.account,
                credential_type=CredentialType.NONE,
                status=CredentialStatus.AVAILABLE,
            )

        raw = self._read(requirement)
        status = (
            CredentialStatus.AVAILABLE if raw else CredentialStatus.MISSING
        )

        logger.debug(
            "Credential availability checked",
            # The identifier and the setting *name*. Never the value, and
            # never its length -- a length is a small leak but a real one.
            extra={
                "credential": requirement.identifier,
                "setting": requirement.setting_name,
                "status": status.value,
            },
        )

        return CredentialState(
            identifier=requirement.identifier,
            provider=requirement.provider,
            account=requirement.account,
            credential_type=requirement.credential_type,
            status=status,
        )

    def resolve_secret(self, requirement: CredentialRequirement) -> str:
        state = self.describe(requirement)

        if state.status is CredentialStatus.MISSING:
            raise CredentialsMissing(detail=requirement.identifier)
        if state.status is CredentialStatus.EXPIRED:
            raise CredentialsExpired(detail=requirement.identifier)
        if state.status is CredentialStatus.INSUFFICIENT_SCOPE:
            raise CredentialScopeInsufficient(detail=requirement.identifier)

        return self._read(requirement) or ""

    def _read(self, requirement: CredentialRequirement) -> Optional[str]:
        name = requirement.setting_name
        if not name:
            return None

        value = getattr(self._settings, name, None) if self._settings else None
        if value is None:
            value = self._environ.get(name)

        # Whitespace is not a credential. Treating "   " as present would
        # send it to a provider and turn a configuration mistake into an
        # authentication failure that looks like a revoked key.
        return value.strip() if isinstance(value, str) and value.strip() else None


#: The default resolver for this process.
_resolver = EnvironmentCredentialResolver()


def get_credential_resolver() -> CredentialResolver:
    return _resolver


# --- Production and multi-user notes ----------------------------------------
#
# **Secret storage.** Configuration is not a secret store. It has no rotation,
# no access audit, no per-secret permissions and no encryption at rest beyond
# whatever the filesystem provides. Before any integration holds a credential
# that matters, this resolver should be joined by one backed by a dedicated
# secret manager. `CredentialResolver` exists as an interface so that is a new
# subclass rather than a rewrite. Nothing here writes a secret to PostgreSQL,
# and no custom encryption is invented -- both deliberate.
#
# **Multi-user ownership.** `account` defaults to `"default"` because Mai is
# single-user today. In a multi-user deployment no credential may be shared
# between users: a credential belongs to the person who granted it, and a
# resolver must refuse to hand one user's token to another user's request.
# Stage 3D already flagged ownership as the multi-user blocker; the field is
# present now so that adding users is a change of value rather than a change
# of shape.
#
# **OAuth.** Not implemented. `CredentialType.OAUTH2`, `expires_at` and
# `granted_scopes` are the shape it will need. Scopes are modelled from the
# start so no integration is ever granted broad provider access on the theory
# that it will be narrowed later.

__all__ = [
    "CredentialRequirement",
    "CredentialResolver",
    "CredentialState",
    "CredentialStatus",
    "CredentialType",
    "EnvironmentCredentialResolver",
    "get_credential_resolver",
]
