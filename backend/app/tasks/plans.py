"""Stage 6B: what a plan must satisfy before it may be attached to a task.

### Why this module exists at all

Stage 4B already validates plans, thoroughly, in `app.planning.validator`:
dependencies exist, nothing depends on itself, the edge budget holds, there
are no cycles, and the topological order is deterministic. None of that is
reimplemented here -- `validate_graph` is imported and called.

What Stage 6A missed is that it never *ran* it. `attach_plan` accepted any
object carrying a `.tasks` list, so a `Plan` built by hand rather than by
`build_plan` could carry a cycle, a self-dependency or a dangling edge and be
materialised into task steps. Measured before this module was written: all
three were accepted, and a duplicate sequence surfaced only as a database
integrity error reported as `persistence_failed`.

So this module is the boundary that makes validation unskippable, plus the
three checks the graph layer does not own because they are properties of the
*persisted* form rather than of the graph:

* every step has the fields a `TaskStep` row requires,
* the sequence is present, positive and unique -- the unique index would
  catch a collision, but as an integrity error with no useful reason,
* the plan carries no execution semantics.

### Capability validation waits for 6C

The brief asks for "valid referenced capabilities/tools according to the
EXISTING capability declarations". There are none to check against, and that
is deliberate rather than missing: no plan type names a tool. `ProposedTask`
and `PlanTask` carry a title, a description, dependencies and criteria --
prose. Stage 4B's own docstring puts it plainly: a task saying "Send the
outreach email" is a sentence, and the planning layer contains nothing
capable of sending one.

So there is no capability reference to validate, and inventing a registry to
validate against would be building 6C early. What this module does instead is
assert the *absence*: `FORBIDDEN_KEYS` refuses a plan that tries to acquire
execution semantics, and a structural test asserts no plan schema has grown a
capability field. When 6C introduces the registry, binding steps to declared
capabilities becomes a check here, against that registry.
"""

from typing import Any, Dict, NamedTuple, Optional, Sequence

from app.core.logging import get_logger
from app.planning.validator import PlanValidationError, validate_graph

logger = get_logger(__name__)

#: Keys that would give a plan execution semantics it must not have.
#:
#: A `PlanTask` drops unknown fields, so a plan built through the schema
#: cannot carry one. This checks the **serialised** plan, which is what
#: reaches the database, so a dict assembled outside the schema is refused
#: too -- and a schema that grows one of these fields later is refused on the
#: day it does, rather than silently persisting it.
FORBIDDEN_KEYS = frozenset({
    "tool", "tool_name", "tools", "capabilities",
    "execute", "execution", "execution_id", "run", "command", "shell",
    "args", "payload", "url", "endpoint", "method", "headers",
    "authorization", "approved", "approval", "credential", "credentials",
    "token", "api_key", "secret", "permissions",
})

#: Deliberately **not** forbidden: `scope`, `capability`, `arguments`.
#:
#: Stage 6C made the latter two legitimate plan vocabulary -- a step declares
#: what it needs, and `app.tasks.capabilities` decides whether that name
#: binds to anything. A blocklist cannot tell a declaration from a grant, so
#: the guarantee moved to where it belongs: the name is inert until the
#: registry resolves it, and a test pins every plan schema's field set.
#:
#: Originally about `scope` alone:
#:
#: `Goal.scope` is prose -- "what this goal covers" -- and has nothing to do
#: with an OAuth scope. The first version of this list refused it and thereby
#: refused every valid plan, which is the failure mode of a keyword blocklist
#: guarding a vocabulary it does not own.
#:
#: The real guarantee against a plan growing execution semantics is not this
#: list but `test_the_plan_schema_fields_are_exactly_these`, which pins the
#: field set of every plan type. A blocklist catches a dict assembled outside
#: the schema; the pin catches the schema itself changing.

#: How deep the forbidden-key scan walks a serialised plan.
MAX_SCAN_DEPTH = 8

#: The fields a step must have to become a `TaskStep` row.
REQUIRED_STEP_FIELDS = ("id", "title")


class PlanCheck(NamedTuple):
    """The verdict. `reason` is an application constant, never model text."""

    ok: bool
    reason: Optional[str] = None
    #: Developer context. Logged, never returned to a client -- it can name a
    #: step id, which came from a model.
    detail: str = ""
    step_count: int = 0
    dependency_count: int = 0


def validate_for_task(plan: Any) -> PlanCheck:
    """Whether this plan may be persisted against a task.

    Deterministic and application-owned: no model is consulted, no clock is
    read, and the same plan always produces the same verdict.
    """
    steps: Sequence[Any] = list(getattr(plan, "tasks", None) or [])

    # An empty plan is not checked here. `validate_graph` already refuses one
    # with the reason `empty_plan`, and a second check producing the same
    # reason is a duplicate guard: mutation testing showed each one made the
    # other unobservable, which is how a check rots without anyone noticing.

    # Layer 1: the fields a persisted step needs.
    for step in steps:
        for field in REQUIRED_STEP_FIELDS:
            value = getattr(step, field, None)
            if not isinstance(value, str) or not value.strip():
                return PlanCheck(False, "step_missing_field", f"{field}")

    # Layer 2: the graph. Stage 4B's validator, unchanged and unduplicated.
    try:
        report = validate_graph(steps)
    except PlanValidationError as refusal:
        # The validator's own reason codes are reused verbatim, so a
        # rejection here reads the same as a rejection during planning.
        reason = refusal.args[0].split(":")[0].strip() if refusal.args else "invalid_plan"
        return PlanCheck(False, reason, str(refusal))
    except Exception as exc:  # noqa: BLE001 - a malformed step must not 500
        return PlanCheck(False, "invalid_plan", type(exc).__name__)

    # Layer 3: the persisted form. A sequence that is absent, not positive or
    # repeated would reach the unique index as an integrity error with no
    # useful reason; refusing here names the fault instead.
    sequences = []
    for step in steps:
        order = getattr(step, "order", None)
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            return PlanCheck(False, "invalid_step_sequence", str(order))
        sequences.append(order)
    if len(set(sequences)) != len(sequences):
        return PlanCheck(False, "duplicate_step_sequence")

    # Layer 4: no execution semantics.
    serialised = _serialise(plan)
    offending = _forbidden_key_in(serialised)
    if offending is not None:
        logger.warning(
            "Refused a plan carrying execution semantics",
            extra={"key": offending},
        )
        return PlanCheck(False, "plan_declares_execution", offending)

    return PlanCheck(
        True,
        step_count=report.task_count,
        dependency_count=report.dependency_count,
    )


def _serialise(plan: Any) -> Any:
    """The plan as it would be stored, or `None` if it cannot be dumped."""
    dump = getattr(plan, "model_dump", None)
    if dump is None:
        return None
    try:
        return dump(mode="json")
    except Exception:  # noqa: BLE001
        return None


def _forbidden_key_in(value: Any, depth: int = 0) -> Optional[str]:
    """The first forbidden key anywhere in a serialised plan, or None.

    Bounded: a plan is already size-limited, and an unbounded walk over data
    that originated with a model is the wrong shape of loop.
    """
    if depth > MAX_SCAN_DEPTH:
        return None
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                return str(key)
            found = _forbidden_key_in(nested, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value[:64]:
            found = _forbidden_key_in(item, depth + 1)
            if found is not None:
                return found
    return None


__all__ = [
    "FORBIDDEN_KEYS",
    "MAX_SCAN_DEPTH",
    "REQUIRED_STEP_FIELDS",
    "PlanCheck",
    "validate_for_task",
]
