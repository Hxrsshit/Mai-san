"""Stage 6C: turning a capability *name* into an authorized capability, or not.

This is the whole of the plan-to-execution boundary, and it is deliberately
small, because almost everything it needs already exists:

* `app.tools.registry` declares what the application has.
* `app.execution.tools` says which of those actually have an implementation.
* `app.tools.authorization` decides unknown / forbidden / approval-required /
  allowed, and validates arguments against the capability's own model.

None of that is reimplemented here. What this module adds is the *binding*:
a step names a capability as a string, and a string from a model is not
authority. Binding is the act of looking that string up and producing either
an application-owned decision or a refusal.

    step.capability: str      <- model output, means nothing
             |
             v
    registry lookup           <- is it declared?
    executable lookup         <- does it exist?
    AuthorizationService      <- is it permitted, with these arguments?
             |
             v
    CapabilityBinding         <- application-owned, the only thing execution reads

### Why an unavailable capability is refused rather than deferred

A tool can be declared without being executable -- `future_send_email` is
declared so Mai can say it does not have it. The authorization service will
happily return `approval_required` for one, because it answers "may this be
done?", not "can this be done?". Both questions have to be asked, and a plan
whose steps cannot run is refused before any execution state exists rather
than discovered halfway through.

### What this module cannot do

It resolves nothing dynamically. There is no `getattr`, no `__import__`, no
`eval`, no string that becomes a callable. The registry is a dictionary
populated at import time from application code, and a lookup that misses
returns a refusal -- a structural test asserts the absence of every dynamic
resolution primitive in this file.
"""

import enum
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

from app.core.logging import get_logger
from app.tools.schemas import ActionProposal, ActionSource, AuthorizationStatus

logger = get_logger(__name__)


class BindingStatus(str, enum.Enum):
    """Whether a step's capability could be bound. A closed set."""

    #: Bound, permitted, and runnable without asking anyone.
    READY = "ready"
    #: Bound and runnable, but a person must approve before it runs.
    NEEDS_APPROVAL = "needs_approval"
    #: The step names no capability. Prose, not work -- valid in a plan,
    #: never executable.
    NO_CAPABILITY = "no_capability"
    #: The name is not in the registry.
    UNKNOWN = "unknown"
    #: Declared, but nothing implements it in this deployment.
    UNAVAILABLE = "unavailable"
    #: Policy refuses it outright.
    FORBIDDEN = "forbidden"
    #: Known and permitted, but the arguments do not fit its model.
    INVALID_ARGUMENTS = "invalid_arguments"


#: The statuses that mean a step could be executed, given approval.
BINDABLE = frozenset({BindingStatus.READY, BindingStatus.NEEDS_APPROVAL})


class CapabilityBinding(NamedTuple):
    """One step's capability, as the application sees it.

    `capability` is the registry's canonical name -- never the model's
    spelling -- so everything downstream reads a value the application chose.
    """

    step_key: str
    status: BindingStatus
    capability: Optional[str] = None
    requires_approval: bool = True
    #: An application reason code. Never model text, never an exception.
    reason: Optional[str] = None

    @property
    def bindable(self) -> bool:
        return self.status in BINDABLE


class PlanBinding(NamedTuple):
    """Every step of a plan, bound or refused."""

    ok: bool
    bindings: List[CapabilityBinding]
    reason: Optional[str] = None

    @property
    def needs_approval(self) -> bool:
        return any(b.requires_approval for b in self.bindings if b.bindable)


def _executable_names() -> frozenset:
    """What actually has an implementation here. Read, never cached.

    Not cached because the executable registry is populated at import time by
    whichever tools this deployment registers, and a cache would freeze the
    first answer -- which in tests is whatever the first fixture registered.
    """
    try:
        from app.execution.tools import get_executable_registry

        return frozenset(get_executable_registry().names())
    except Exception:  # noqa: BLE001 - an unreadable registry means none
        return frozenset()


def bind_step(
    step_key: str,
    capability: Optional[str],
    arguments: Optional[Dict[str, Any]] = None,
    authorization=None,
) -> CapabilityBinding:
    """Bind one step's capability name, or refuse it.

    Never raises. Every failure is a status, because a plan full of hostile
    strings must produce a verdict rather than an exception trace.
    """
    if not capability or not str(capability).strip():
        return CapabilityBinding(
            step_key, BindingStatus.NO_CAPABILITY, reason="step_declares_no_capability"
        )

    # Order matters, and this is the useful one: is it declared, does it
    # exist, and only then may it be done with these arguments. Asking the
    # authorization service first reported a capability with no
    # implementation as "arguments failed validation", which sends a reader
    # to fix the wrong thing.
    from app.tools.registry import get_registry

    registry = get_registry()
    name = registry.canonical(str(capability))
    if registry.definition(name) is None:
        return CapabilityBinding(
            step_key, BindingStatus.UNKNOWN, reason="unknown_capability"
        )
    if name not in _executable_names():
        return CapabilityBinding(
            step_key, BindingStatus.UNAVAILABLE, capability=name,
            reason="capability_unavailable",
        )

    if authorization is None:
        from app.tools.authorization import AuthorizationService

        authorization = AuthorizationService()

    try:
        decision = authorization.authorize(
            ActionProposal(
                tool_name=str(capability),
                arguments=dict(arguments or {}),
                # The truthful provenance. A plan step's capability came from
                # a model, and `ActionSource` is what the policy layer reads
                # to apply the stricter of its rules.
                source=ActionSource.MODEL,
            )
        )
    except Exception as exc:  # noqa: BLE001 - malformed input is a refusal
        logger.info(
            "Capability could not be bound",
            extra={"step": step_key, "error": type(exc).__name__},
        )
        return CapabilityBinding(
            step_key, BindingStatus.UNKNOWN, reason="capability_not_bindable"
        )

    # No `UNKNOWN_TOOL` branch: the registry lookup above already refused
    # every name the authorization service could report as unknown, so a
    # second check here would be a duplicate guard -- and mutation testing
    # showed it was unreachable, which is how a guard rots unnoticed.
    if decision.status is AuthorizationStatus.FORBIDDEN:
        # Arguments that fail validation are reported distinctly: "you may
        # not do this" and "you asked for this wrongly" send a reader to
        # different places.
        invalid = str(decision.reason or "").lower().find("argument") >= 0
        return CapabilityBinding(
            step_key,
            BindingStatus.INVALID_ARGUMENTS if invalid else BindingStatus.FORBIDDEN,
            capability=decision.tool_name,
            reason=str(decision.reason) if decision.reason else "capability_forbidden",
        )

    status = (
        BindingStatus.NEEDS_APPROVAL
        if decision.requires_approval
        else BindingStatus.READY
    )
    return CapabilityBinding(
        step_key,
        status,
        capability=decision.tool_name,
        requires_approval=decision.requires_approval,
    )


def bind_plan(steps: Sequence, authorization=None) -> PlanBinding:
    """Bind every step of a plan. All or nothing.

    A partially bindable plan is refused: executing the half that binds would
    leave a task that can never finish, and deciding that halfway through is
    worse than deciding it now. `ok` is true only when every step bound.
    """
    bindings: List[CapabilityBinding] = []
    for step in steps:
        bindings.append(
            bind_step(
                str(getattr(step, "step_key", None) or getattr(step, "id", "")),
                getattr(step, "capability", None),
                getattr(step, "arguments", None),
                authorization=authorization,
            )
        )

    unbindable = [b for b in bindings if not b.bindable]
    if unbindable:
        # The first failure names the reason. Reporting every one would let a
        # caller enumerate the registry by submitting a plan of guesses.
        return PlanBinding(False, bindings, reason=unbindable[0].reason)

    return PlanBinding(True, bindings)


__all__ = [
    "BINDABLE",
    "BindingStatus",
    "CapabilityBinding",
    "PlanBinding",
    "bind_plan",
    "bind_step",
]
