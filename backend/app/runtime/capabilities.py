"""What Mai can actually do, derived from the registry rather than recalled.

Stage 4D.1 fixed Mai answering "OpenAI / GPT-4" when asked what it ran on.
This module fixes the same class of failure one level up: asked what tools it
had, Mai listed web search, a calculator, code execution, filesystem access
and email sending. None of those exist. The model was not lying -- it was
answering from pretraining, because nothing in the prompt had ever told it
what this deployment actually has.

**Knowledge of a capability is not availability of a capability.** The model
knows what SMTP is; Mai cannot send email. The model knows how a web crawler
works; Mai cannot browse. Explaining a thing and being able to do it are
different claims, and only the second one this module answers.

The source of truth is the two registries, read at build time:

    app.tools.registry        what the application *declares*
    app.execution.tools       what actually has an implementation

Nothing here maintains a list. A tool that is not registered cannot appear,
and a tool that is registered cannot be omitted -- which is what makes the
rendered capability section follow configuration without anyone editing
prompt text.

Cost: zero model calls, zero database queries. Both registries are in-memory
dictionaries populated at import time, so this is a loop over a tuple.
"""

import enum
from typing import Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)


class CapabilityState(str, enum.Enum):
    """How available one declared capability is, right now.

    Five states rather than a boolean, because "can Mai do this?" has five
    genuinely different answers and collapsing them produces exactly the
    vagueness this stage exists to remove. "The tool exists but execution is
    off" and "there is no such tool" would both become "no", and a user who
    heard "no" to the first would be misinformed about what switching
    execution on would give them.

    Ordered from least to most available, so a reader can see the ladder.
    """

    #: Declared in the catalogue, no implementation behind it. Mai cannot do
    #: this, and no configuration change would make it possible.
    NOT_IMPLEMENTED = "not_implemented"
    #: Implemented, but policy or the operator refuses it -- the tool is
    #: disabled, its category is forbidden, or its risk is above the ceiling.
    IMPLEMENTED_DISABLED = "implemented_disabled"
    #: Implemented and permitted, but execution is switched off for this
    #: deployment. Turning `EXECUTION_ENABLED` on would make it available.
    IMPLEMENTED_UNAVAILABLE = "implemented_unavailable"
    #: Usable, and only after the user approves that specific action.
    AVAILABLE_WITH_APPROVAL = "available_with_approval"
    #: Usable. No registered tool is in this state today -- every executable
    #: tool requires approval -- but the state is real rather than decorative:
    #: it is what a tool with `requires_approval=False` would reach, and a
    #: test builds one to prove the ladder is not collapsed.
    AVAILABLE = "available"


#: The states in which Mai can actually perform the action.
USABLE_STATES = frozenset(
    {CapabilityState.AVAILABLE, CapabilityState.AVAILABLE_WITH_APPROVAL}
)


def describe(state: CapabilityState) -> str:
    """One unambiguous sentence per state.

    Written out rather than derived, because the wording is the deliverable.
    "Mai can write files" and "the file tool exists but execution is
    currently disabled" are both short; only one of them is true when the
    switch is off.
    """
    return _DESCRIPTIONS.get(state, "Availability unknown. Treat as unavailable.")


_DESCRIPTIONS = {
    CapabilityState.NOT_IMPLEMENTED: (
        "declared but NOT implemented -- Mai cannot perform this"
    ),
    CapabilityState.IMPLEMENTED_DISABLED: (
        "implemented but currently forbidden by policy -- Mai cannot perform this"
    ),
    CapabilityState.IMPLEMENTED_UNAVAILABLE: (
        "implemented, but execution is switched off for this deployment -- "
        "Mai cannot perform this right now"
    ),
    CapabilityState.AVAILABLE_WITH_APPROVAL: (
        "available only after the user explicitly approves that specific action"
    ),
    CapabilityState.AVAILABLE: "available",
}


def build(
    settings=None,
    registry=None,
    executable=None,
    integrations=None,
) -> Tuple["ToolCapability", ...]:
    """Derive one `ToolCapability` per registered tool. Never raises.

    Total by design. A failure to read a registry yields an empty tuple, and
    an empty tuple renders as "no tools" -- the conservative answer. A
    capability layer that raised would take the whole prompt down; one that
    guessed would be the bug it exists to fix.
    """
    from app.runtime.schemas import ToolCapability

    try:
        registry = registry if registry is not None else _default_registry()
        executable = executable if executable is not None else _default_executable()
        settings = settings if settings is not None else _default_settings()
        integrations = (
            integrations if integrations is not None else _default_integrations()
        )
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the tool registries; reporting no capabilities")
        return ()

    execution_enabled = bool(getattr(settings, "EXECUTION_ENABLED", False))

    capabilities = []
    for definition in registry.list_registered():
        try:
            capabilities.append(
                ToolCapability(
                    identifier=definition.name,
                    display_name=_readable(definition.name),
                    description=definition.description,
                    category=definition.category.value,
                    risk_level=definition.risk_level.value,
                    enabled=definition.enabled,
                    requires_approval=definition.requires_approval,
                    state=_state_for(
                        definition,
                        has_executor=executable.contains(definition.name),
                        execution_enabled=execution_enabled,
                        integration_ready=_integration_ready(
                            executable.get(definition.name), integrations
                        ),
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            # One malformed declaration degrades one entry, not the section.
            # Omitting it is safe: an omitted tool reads as unavailable, and
            # unavailable is the direction this layer fails in.
            logger.warning(
                "Skipped a tool whose capability could not be described",
                extra={"tool": getattr(definition, "name", "unknown")},
            )

    return tuple(capabilities)


def _integration_ready(tool, integrations) -> bool:
    """Whether this tool's external service is usable right now.

    A tool with no integration is always ready -- the filesystem tools need
    nothing external. A tool that declares one is ready only when that
    adapter is registered and available, so a missing API key makes the
    capability report "implemented, not available right now" instead of
    advertising something that would fail on first use.

    True when there is no tool object at all: a declaration with no executor
    is already `NOT_IMPLEMENTED` for a more fundamental reason, and this
    should not be what decides it.
    """
    name = tool.integration_name if tool is not None else ""
    if not name:
        return True

    integration = integrations.get(name) if integrations is not None else None
    return bool(integration is not None and integration.available)


def _state_for(
    definition,
    has_executor: bool,
    execution_enabled: bool,
    integration_ready: bool = True,
):
    """Map one declaration onto the ladder.

    Order matters, and it runs from the most fundamental obstacle to the
    least. A tool with no implementation is `NOT_IMPLEMENTED` whatever the
    switches say -- reporting it as "disabled" would imply that enabling
    something would help, and nothing would.

    The permission question is delegated to `app.tools.policy`, not
    re-decided here. Duplicating that logic would let the capability section
    and the authorization layer disagree, and the section would then be
    describing a Mai that does not exist.
    """
    from app.tools import policy
    from app.tools.schemas import AuthorizationStatus

    if not has_executor:
        return CapabilityState.NOT_IMPLEMENTED

    status, _ = policy.evaluate(definition, definition.name)

    if status in (AuthorizationStatus.FORBIDDEN, AuthorizationStatus.UNKNOWN_TOOL):
        return CapabilityState.IMPLEMENTED_DISABLED

    if not execution_enabled:
        return CapabilityState.IMPLEMENTED_UNAVAILABLE

    if not integration_ready:
        # Implemented and permitted, but its external service is not usable.
        # `IMPLEMENTED_UNAVAILABLE`, not `IMPLEMENTED_DISABLED`: this is
        # availability, not permission, and reporting a missing credential as
        # "forbidden" would send someone to look at policy instead of at
        # configuration.
        return CapabilityState.IMPLEMENTED_UNAVAILABLE

    if status is AuthorizationStatus.APPROVAL_REQUIRED:
        return CapabilityState.AVAILABLE_WITH_APPROVAL

    return CapabilityState.AVAILABLE


def _readable(identifier: str) -> str:
    """`create_text_file` -> `Create text file`.

    Derived from the identifier rather than stored as a second display field,
    so a name and its label cannot drift apart -- the same reasoning that put
    the tools' argument schemas in one place.
    """
    return (identifier or "").replace("_", " ").strip().capitalize() or "Unnamed tool"


def _default_registry():
    from app.tools import catalog  # noqa: F401  (import registers)
    from app.tools.registry import get_registry

    return get_registry()


def _default_executable():
    from app.execution.tools import get_executable_registry

    return get_executable_registry()


def _default_settings():
    from app.core.config import get_settings

    return get_settings()


def _default_integrations():
    from app.integrations.registry import get_integration_registry

    return get_integration_registry()


__all__ = ["CapabilityState", "USABLE_STATES", "build", "describe"]
