"""Execution failures, as typed refusals.

Every one of these means *nothing ran*. The dispatcher raises them before it
reaches a tool, so a caller seeing any of them knows the side effect did not
happen -- which is what makes truthful reporting possible downstream.

`reason` is an application constant. Nothing here carries model output, user
text, a filesystem path or an exception string, so a refusal cannot be used to
echo an attacker's input back or to leak where the workspace lives.
"""


class ExecutionError(Exception):
    """Base class. Carries a stable reason code and nothing else."""

    reason: str = "execution_failed"

    def __init__(self, reason: str = "", detail: str = "") -> None:
        self.reason = reason or self.reason
        #: Developer context. Logged, never returned to a client.
        self.detail = detail
        super().__init__(self.reason)


class ExecutionDisabled(ExecutionError):
    reason = "execution_disabled"


class UnknownExecution(ExecutionError):
    reason = "no_such_execution"


class InvalidStateTransition(ExecutionError):
    reason = "invalid_state_transition"


class ApprovalRequired(ExecutionError):
    reason = "approval_required"


class ApprovalInvalid(ExecutionError):
    """The approval does not match what is about to run."""

    reason = "approval_does_not_match_this_action"


class ApprovalExpired(ExecutionError):
    reason = "approval_expired"


class ApprovalRevoked(ExecutionError):
    reason = "approval_revoked"


class NotAuthorized(ExecutionError):
    """Stage 4C policy refuses it. No approval can lift this."""

    reason = "not_authorized"


class NotExecutable(ExecutionError):
    """The tool is registered but has no implementation behind it."""

    reason = "tool_is_not_executable"


class AlreadyRunning(ExecutionError):
    """Another attempt claimed this execution first."""

    reason = "execution_already_claimed"


class WorkspaceViolation(ExecutionError):
    """A path resolved outside the workspace, or could not be made safe."""

    reason = "path_outside_workspace"


class ToolFailure(ExecutionError):
    """The tool ran and failed. The only error meaning something was attempted."""

    reason = "tool_execution_failed"


__all__ = [
    "AlreadyRunning",
    "ApprovalExpired",
    "ApprovalInvalid",
    "ApprovalRequired",
    "ApprovalRevoked",
    "ExecutionDisabled",
    "ExecutionError",
    "InvalidStateTransition",
    "NotAuthorized",
    "NotExecutable",
    "ToolFailure",
    "UnknownExecution",
    "WorkspaceViolation",
]
