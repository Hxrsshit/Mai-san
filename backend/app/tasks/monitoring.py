"""Stage 6G: what a monitoring check looks for, as inert data.

A monitoring task repeats one read-only check on an interval and stops when a
condition holds. This module owns the condition: its shape, its bounds, and
the one function that evaluates it. Nothing else interprets a condition.

### Inert by construction

A condition is a typed record -- a kind, a path, an operator, an expected
value -- and evaluation is a fixed function of those four fields over the
data a capability returned. There is no expression language, no `eval`, no
`exec`, no import, no `getattr`, and no callable named by a string. A path is
a few lowercase dictionary keys, resolved by dictionary lookup alone, so it
cannot reach an attribute, a method, an index or anything a key was not.

Anything outside the closed sets -- an unknown kind, an unsupported operator,
a malformed path, an out-of-bounds value -- is refused when the monitoring
task is configured, and refused again if a stored value ever fails to parse.
Configuration fails closed; evaluation reports `UNABLE` rather than guessing.

### Unable is not false

Three results, never two. A check that could not be evaluated -- the path is
missing, the value is the wrong type -- is `UNABLE`, which the runtime counts
as a failure with bounded retries. Collapsing it into "not satisfied" would
let a broken check run forever while reporting that nothing had happened.

### External content is data

The value a check observes came from outside Mai: a web page, an inbox, a
file. It is compared and nothing more. It cannot change the condition, the
interval or the capability, because those are read from the task row, which
only `TaskService.configure_monitoring` writes, once, before the plan is
authorised.
"""

import enum
import math
import re
from typing import Any, Dict, FrozenSet, NamedTuple, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# --- Bounds ---------------------------------------------------------------
#
# Constants in application code. None is read from configuration or derived
# from a model's proposal, so nothing a user or a page says can widen them.

#: Fewest seconds between checks. Five minutes.
MIN_INTERVAL_SECONDS = 300
#: Most seconds between checks. Seven days.
MAX_INTERVAL_SECONDS = 604_800

#: Path segments, and their shape. A path is dictionary keys, nothing else.
MAX_PATH_SEGMENTS = 4
_SEGMENT = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

MAX_EXPECTED_TEXT_CHARS = 200
MAX_EXPECTED_NUMBER = 1_000_000_000
#: The most of an observed string compared. Bounds the work a hostile page can
#: cause by being enormous.
MAX_OBSERVED_TEXT_CHARS = 100_000
#: The most items a `contains` check scans in a list of strings.
MAX_LIST_ITEMS = 1_000

#: The capabilities a monitoring check may use. Literal and closed.
#:
#: Every one only reads. A monitoring task repeats its check, so a capability
#: with a side effect would repeat the side effect -- "check every hour" would
#: become "write a file every hour". Nothing in the tool declarations marks
#: side effects (`create_text_file` shares a category with
#: `list_workspace_files`), so the rule is written down here instead, and a
#: capability is monitorable only by being named. Widening this set is the
#: change a reviewer would have to argue for.
MONITORABLE_CAPABILITIES: FrozenSet[str] = frozenset({
    "web_search",
    "calendar_list_events",
    "gmail_list_messages",
    "gmail_get_message",
    "list_workspace_files",
    "read_text_file",
})


class ConditionKind(str, enum.Enum):
    #: How many items a list holds.
    COUNT = "count"
    #: A single scalar value.
    VALUE = "value"
    #: Whether text appears, case-insensitively.
    CONTAINS = "contains"


class Operator(str, enum.Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    CONTAINS = "contains"


#: Which operators each kind accepts. Anything else is refused.
ALLOWED_OPERATORS: Dict[ConditionKind, FrozenSet[Operator]] = {
    ConditionKind.COUNT: frozenset({
        Operator.EQ, Operator.NE, Operator.GT, Operator.GTE, Operator.LT, Operator.LTE,
    }),
    ConditionKind.VALUE: frozenset({
        Operator.EQ, Operator.NE, Operator.GT, Operator.GTE, Operator.LT, Operator.LTE,
    }),
    ConditionKind.CONTAINS: frozenset({Operator.CONTAINS}),
}

_ORDERING = frozenset({Operator.GT, Operator.GTE, Operator.LT, Operator.LTE})

Scalar = Union[bool, int, float, str]


class Condition(BaseModel):
    """One typed, bounded condition. Frozen and closed to extra fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ConditionKind
    path: str = Field(..., min_length=1, max_length=200)
    operator: Operator
    expected: Scalar

    @field_validator("path")
    @classmethod
    def _path_is_keys(cls, value: str) -> str:
        segments = value.split(".")
        if not 1 <= len(segments) <= MAX_PATH_SEGMENTS:
            raise ValueError("path depth out of range")
        for segment in segments:
            # Lowercase keys only: no dunders, no indices, no brackets, no
            # spaces, nothing that could name more than one dictionary key.
            if not _SEGMENT.match(segment):
                raise ValueError("path segment is not a plain key")
        return value

    @model_validator(mode="after")
    def _kind_fits(self) -> "Condition":
        if self.operator not in ALLOWED_OPERATORS[self.kind]:
            raise ValueError("operator not allowed for this kind")

        expected = self.expected
        if self.kind is ConditionKind.COUNT:
            if isinstance(expected, bool) or not isinstance(expected, int):
                raise ValueError("count expects a whole number")
            if not 0 <= expected <= MAX_EXPECTED_NUMBER:
                raise ValueError("count expectation out of range")
        elif self.kind is ConditionKind.CONTAINS:
            if not isinstance(expected, str):
                raise ValueError("contains expects text")
            if not 1 <= len(expected.strip()) <= MAX_EXPECTED_TEXT_CHARS:
                raise ValueError("contains text out of range")
        else:  # VALUE
            if isinstance(expected, str):
                if len(expected) > MAX_EXPECTED_TEXT_CHARS:
                    raise ValueError("expected text too long")
                if self.operator in _ORDERING:
                    raise ValueError("ordering needs a number")
            elif isinstance(expected, bool):
                if self.operator in _ORDERING:
                    raise ValueError("ordering needs a number")
            elif not math.isfinite(expected) or abs(expected) > MAX_EXPECTED_NUMBER:
                # NaN compares false to everything, so a NaN bound would also
                # slip past the range check -- and PostgreSQL's JSONB refuses it.
                raise ValueError("expected number out of range")
        return self


class MonitorSpec(BaseModel):
    """What a monitoring task checks for, and how often. Persisted whole."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition: Condition
    interval_seconds: int

    @field_validator("interval_seconds", mode="before")
    @classmethod
    def _interval_in_bounds(cls, value: Any) -> int:
        # Refused rather than clamped. A user who asked for every ten seconds
        # did not ask for every five minutes, and guessing what they would
        # accept is a decision this module does not get to make.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("interval must be a whole number of seconds")
        if not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS:
            raise ValueError("interval out of range")
        return value


class SpecRefused(Exception):
    """A monitoring spec that may not be stored. `reason` is a constant."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_spec(raw: Any) -> MonitorSpec:
    """Validate a proposed or stored spec, or raise `SpecRefused`.

    Never raises anything else: every malformed shape becomes one of a few
    application reason codes, and pydantic's own message -- which can echo
    the offending input -- is not passed on.
    """
    if not isinstance(raw, dict):
        raise SpecRefused("monitor_not_an_object")
    try:
        return MonitorSpec.model_validate(raw)
    except ValidationError as exc:
        fields = {str(error.get("loc", ("",))[0]) for error in exc.errors()}
        if "interval_seconds" in fields:
            raise SpecRefused("interval_out_of_range") from None
        if "condition" in fields:
            raise SpecRefused("invalid_condition") from None
        # A field the spec does not have.
        raise SpecRefused("invalid_monitor") from None
    except Exception:  # noqa: BLE001 - malformed input is a refusal, not a 500
        raise SpecRefused("invalid_monitor") from None


def interval_of(raw: Any) -> Optional[int]:
    """The stored interval, or None if the stored spec does not parse."""
    try:
        return parse_spec(raw).interval_seconds
    except SpecRefused:
        return None


# --- Evaluation ---------------------------------------------------------------


class CheckResult(str, enum.Enum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    #: The check ran, but its data did not answer the question.
    UNABLE = "unable"


class Evaluation(NamedTuple):
    result: CheckResult
    #: The observed number, when it is one. Never observed text: that came
    #: from outside Mai and belongs to neither a journal nor a log.
    observed_number: Optional[float] = None
    #: How long an observed string was, when it was one.
    observed_chars: Optional[int] = None
    reason: Optional[str] = None


_MISSING = object()


def _resolve(data: Any, path: str) -> Any:
    """Follow plain dictionary keys. Nothing else is ever followed."""
    current = data
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _compare(operator: Operator, observed: Any, expected: Any) -> bool:
    if operator is Operator.EQ:
        return observed == expected
    if operator is Operator.NE:
        return observed != expected
    if operator is Operator.GT:
        return observed > expected
    if operator is Operator.GTE:
        return observed >= expected
    if operator is Operator.LT:
        return observed < expected
    if operator is Operator.LTE:
        return observed <= expected
    raise ValueError("operator not comparable")


def _is_number(value: Any) -> bool:
    # Finite only: an observed NaN or infinity is not a reading to compare.
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value)
    )


def evaluate(condition: Condition, data: Any) -> Evaluation:
    """Evaluate one condition over one check's data. Pure; never raises."""
    try:
        value = _resolve(data, condition.path)
        if value is _MISSING:
            return Evaluation(CheckResult.UNABLE, reason="path_not_found")

        if condition.kind is ConditionKind.COUNT:
            if not isinstance(value, list):
                return Evaluation(CheckResult.UNABLE, reason="not_a_list")
            observed = len(value)
            met = _compare(condition.operator, observed, condition.expected)
            return Evaluation(
                CheckResult.SATISFIED if met else CheckResult.NOT_SATISFIED,
                observed_number=float(observed),
            )

        if condition.kind is ConditionKind.CONTAINS:
            needle = str(condition.expected).strip().lower()
            if isinstance(value, str):
                haystacks = [value]
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                haystacks = value[:MAX_LIST_ITEMS]
            else:
                return Evaluation(CheckResult.UNABLE, reason="not_text")
            met = any(
                needle in item[:MAX_OBSERVED_TEXT_CHARS].lower() for item in haystacks
            )
            return Evaluation(
                CheckResult.SATISFIED if met else CheckResult.NOT_SATISFIED,
                observed_chars=sum(len(item) for item in haystacks),
            )

        # VALUE
        expected = condition.expected
        if isinstance(expected, bool):
            if not isinstance(value, bool):
                return Evaluation(CheckResult.UNABLE, reason="type_mismatch")
            met = _compare(condition.operator, value, expected)
            return Evaluation(CheckResult.SATISFIED if met else CheckResult.NOT_SATISFIED)
        if _is_number(expected):
            if not _is_number(value):
                return Evaluation(CheckResult.UNABLE, reason="type_mismatch")
            met = _compare(condition.operator, value, expected)
            return Evaluation(
                CheckResult.SATISFIED if met else CheckResult.NOT_SATISFIED,
                observed_number=float(value),
            )
        if not isinstance(value, str):
            return Evaluation(CheckResult.UNABLE, reason="type_mismatch")
        met = _compare(condition.operator, value[:MAX_OBSERVED_TEXT_CHARS], expected)
        return Evaluation(
            CheckResult.SATISFIED if met else CheckResult.NOT_SATISFIED,
            observed_chars=len(value),
        )
    except Exception:  # noqa: BLE001 - an evaluation that fails is UNABLE
        return Evaluation(CheckResult.UNABLE, reason="evaluation_error")


__all__ = [
    "ALLOWED_OPERATORS",
    "MAX_INTERVAL_SECONDS",
    "MAX_PATH_SEGMENTS",
    "MIN_INTERVAL_SECONDS",
    "MONITORABLE_CAPABILITIES",
    "CheckResult",
    "Condition",
    "ConditionKind",
    "Evaluation",
    "MonitorSpec",
    "Operator",
    "SpecRefused",
    "evaluate",
    "interval_of",
    "parse_spec",
]
