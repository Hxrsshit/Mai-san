"""Authoritative facts about the running system.

Mai was asked which LLM provider it was using and answered "OpenAI / GPT-4".
It is configured for a different provider entirely. The answer was not a
retrieval failure or a bad memory -- **nothing in the prompt had ever told the
model what it runs on**, so it answered from generic pretraining, confidently
and wrongly.

That is the distinction this module exists to draw:

- **Category A -- authoritative runtime facts.** What this assistant *is*: its
  name, its provider and model, its database, which of its capabilities are
  switched on. These come from configuration and live objects. They are
  deterministic, and they are never inferred.
- **Category B -- personal long-term knowledge.** What the *user* said, wants
  and decided. This continues to travel the Stage 2D/3A/3B memory path, and
  continues to be untrusted reference data.

Confusing the two in either direction is a bug. A memory must not decide what
model Mai runs on; a runtime fact must not answer a question about the user's
own projects.

Nothing here is derived from model output, memory, retrieval, entity or
relationship extraction, or conversation history. A `RuntimeFacts` is built
from `Settings` and the live provider object and from nothing else.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class RuntimeFacts(BaseModel):
    """What the application authoritatively knows about itself.

    Frozen. These are read from configuration once per request and rendered;
    a caller holding one cannot edit it into a different claim.

    Structured rather than a prompt string, so the same facts can answer a
    debug endpoint, a log line and a prompt section without three copies
    drifting apart.
    """

    model_config = ConfigDict(frozen=True)

    #: What this assistant is called. From `APP_NAME`.
    assistant_name: str = "Mai"
    #: `development`, `production`, … From `APP_ENV`.
    environment: str = "development"

    #: The configured provider and model. From `LLM_PROVIDER` and the
    #: provider's own `model` property -- the live object, not a guess.
    llm_provider: str = "unknown"
    llm_model: str = "unknown"

    #: The database *dialect* only -- `postgresql`, `sqlite`.
    #:
    #: Never the connection string. Stage 3D established that a DSN carries
    #: the database password, and a prompt is the last place it should appear.
    #: `facts.py` extracts the scheme and discards the rest.
    database: str = "unknown"

    # --- Capability switches, read from configuration -----------------------

    memory_enabled: bool = True
    retrieval_enabled: bool = True
    planning_enabled: bool = True
    intent_classification_enabled: bool = True
    #: The Stage 4C authorization framework is present and reachable.
    tool_authorization_enabled: bool = True
    #: How many tools the application has declared. Registry count, not a guess.
    registered_tool_count: int = 0

    #: Optional, when the deployment supplies one.
    version: Optional[str] = None

    @property
    def can_execute_actions(self) -> bool:
        """Always False, and a property rather than a field on purpose.

        No configuration flag can make this true, because no configuration
        creates an executor. Stage 4C defines no `execute` method on `Tool`
        and Stage 4D added no dispatcher, so "can Mai perform an action?" has
        one correct answer and the type refuses to represent any other.

        The same reasoning as `OrchestrationResult.acted`: a fact that must
        never be wrong should not be a field that could be set.
        """
        return False


__all__ = ["RuntimeFacts"]
