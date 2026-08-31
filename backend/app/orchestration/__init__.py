"""Stage 4D action proposal and orchestration.

Connects Mai's decision systems to the Stage 4C authorization boundary:

    intent (4A) -> eligibility -> matching -> ActionProposal (4C)
                                           -> authorization (4C)
                                           -> OrchestrationResult

**Orchestration only. Nothing executes.** Stage 4C's guarantee is preserved
untouched: no `execute` method exists, no dispatcher was added, and this
package imports nothing that could reach a subprocess, a file, or the network.

Three authority boundaries carry forward, and one is added:

- Stage 4A decides which turns carry execution capability (`eligibility.py`
  follows it rather than restating it).
- Stage 4C decides what tools exist and whether one is permitted. This package
  calls that policy; it does not reimplement it.
- Application code decides what a message could mean (`matching.py`). A model
  never names a tool, so a model cannot name one that does not exist.
"""

from app.orchestration.eligibility import (
    ACTION_CAPABLE_INTENTS,
    decide,
    is_action_capable,
)
from app.orchestration.matching import find_candidates, known_trigger_phrases
from app.orchestration.schemas import (
    MAX_PROPOSALS,
    ActionCandidate,
    ActionOutcome,
    OrchestrationRead,
    OrchestrationResult,
    ProposalOutcome,
)
from app.orchestration.service import OrchestrationService

__all__ = [
    "ACTION_CAPABLE_INTENTS",
    "MAX_PROPOSALS",
    "ActionCandidate",
    "ActionOutcome",
    "OrchestrationRead",
    "OrchestrationResult",
    "OrchestrationService",
    "ProposalOutcome",
    "decide",
    "find_candidates",
    "is_action_capable",
    "known_trigger_phrases",
]
