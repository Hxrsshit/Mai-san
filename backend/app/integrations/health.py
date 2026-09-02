"""Health checks that cannot do anything.

A health check must never be a way to cause a side effect. "Verify the email
integration works" must not send an email, "verify calendar access" must not
create an event, and neither may be a channel through which someone triggers
an action nobody approved.

So what is checked here is **configuration**, not connectivity: does an
adapter exist, is it enabled, is a credential present and structurally usable.
No network call is made. A future live check would need its own design, its
own approval story and its own rate limit -- it is not smuggled in here.
"""

from typing import List

from pydantic import BaseModel, ConfigDict

from app.integrations.base import IntegrationState
from app.integrations.credentials import CredentialStatus
from app.integrations.registry import (
    IntegrationRegistry,
    get_integration_registry,
)


class IntegrationHealth(BaseModel):
    """One integration's reported condition. Carries no secret.

    Names the credential and its setting, never its value -- so this is safe
    to log, to return from an endpoint, and to show a person debugging a
    misconfiguration, which is the whole reason it exists.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    provider: str
    state: IntegrationState
    available: bool
    credential_status: CredentialStatus
    #: The configuration key a missing credential would come from. A name.
    credential_setting: str = ""
    operations: tuple = ()


def check(registry: IntegrationRegistry = None) -> List[IntegrationHealth]:
    """Describe every registered integration. Makes no external call."""
    registry = registry if registry is not None else get_integration_registry()

    report: List[IntegrationHealth] = []
    for name in registry.names():
        integration = registry.get(name)
        requirement = integration.credential_requirement
        report.append(
            IntegrationHealth(
                name=integration.name,
                provider=integration.provider or integration.name,
                state=integration.state(),
                available=integration.available,
                credential_status=integration.credential_state().status,
                credential_setting=requirement.setting_name or "",
                operations=integration.operation_names(),
            )
        )
    return report


__all__ = ["IntegrationHealth", "check"]
