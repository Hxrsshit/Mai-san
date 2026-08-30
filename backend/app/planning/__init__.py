"""Stage 4B goal and planning engine.

Turns a user's goal into a validated, dependency-ordered plan:

    IntentResult (4A) -> policy.decide -> Planner -> PlanProposal
                      -> schema validation -> graph validation -> Plan

Planning only. Stage 4B has no tool, no executor, no agent loop and no
replanning. A `Plan` is inert data: a task reading "Send the outreach email"
is a sentence about future work, and nothing in this codebase can act on it.

Two authority boundaries carry forward:

- The application decides *whether* to plan (`policy.py`), from the Stage 4A
  intent. The model is never asked.
- The application decides whether a proposal *is* a plan (`validator.py`).
  Valid JSON is not a valid plan; the graph must be sound as well.
"""

from app.planning.planner import PlanGenerationError, Planner
from app.planning.policy import PLANNABLE_INTENTS, decide, is_plannable
from app.planning.schemas import (
    Goal,
    Plan,
    PlanProposal,
    PlanRead,
    PlanStatus,
    PlanTask,
    PlanningRead,
    PlanningResult,
    Priority,
    ProposedTask,
)
from app.planning.service import PlanningService
from app.planning.validator import (
    GraphReport,
    PlanValidationError,
    build_plan,
    validate_graph,
)

__all__ = [
    "GraphReport",
    "Goal",
    "PLANNABLE_INTENTS",
    "Plan",
    "PlanGenerationError",
    "PlanProposal",
    "PlanRead",
    "PlanStatus",
    "PlanTask",
    "PlanValidationError",
    "Planner",
    "PlanningRead",
    "PlanningResult",
    "PlanningService",
    "Priority",
    "ProposedTask",
    "build_plan",
    "decide",
    "is_plannable",
    "validate_graph",
]
