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

from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.runtime.capabilities import CapabilityState, USABLE_STATES


class ToolCapability(BaseModel):
    """One registered tool, as the application describes it to itself.

    Every field is copied from a `ToolDefinition` or computed from the two
    registries. None is settable by a request, none comes from a model, and
    none is read from the database -- so a memory claiming "Mai can send
    email" cannot produce an entry here, because entries are not produced
    from text at all.

    No field can carry a secret. There is no URL, no path, no key, no
    connection string and no argument value: a name, a sentence, two
    enumerated labels and three booleans. A test asserts the field set, so a
    future addition has to be a deliberate one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The canonical registry name. What an execution request would carry.
    identifier: str
    #: `create_text_file` -> `Create text file`. Derived, never stored twice.
    display_name: str
    #: The declaration's own description. Application text, not user text.
    description: str
    category: str
    risk_level: str

    #: The operator switch on this specific tool.
    enabled: bool
    #: Whether a human must approve each use.
    requires_approval: bool

    #: Where this tool sits on the availability ladder. See `CapabilityState`.
    state: CapabilityState

    @property
    def usable(self) -> bool:
        """Whether Mai can actually perform this, with or without approval.

        A property rather than a field, for the reason every load-bearing
        fact in this package is: a value that must never be wrong should not
        be one a caller can set.
        """
        return self.state in USABLE_STATES


class RuntimeFacts(BaseModel):
    """What the application authoritatively knows about itself.

    Frozen. These are read from configuration once per request and rendered;
    a caller holding one cannot edit it into a different claim.

    Structured rather than a prompt string, so the same facts can answer a
    debug endpoint, a log line and a prompt section without three copies
    drifting apart.
    """

    #: `extra="forbid"` as well as frozen. The property already wins over any
    #: passed value -- `can_execute_actions` is computed, so a forged one was
    #: silently ignored rather than believed. Ignoring is safe and quiet;
    #: refusing says the caller tried, which is what a fact this load-bearing
    #: deserves.
    model_config = ConfigDict(frozen=True, extra="forbid")

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
    #: Whether a turn is examined for a proposed action at all (Stage 4D).
    action_orchestration_enabled: bool = True
    #: The Stage 4C authorization framework is present and reachable.
    tool_authorization_enabled: bool = True
    #: How many tools the application has declared. Registry count, not a guess.
    registered_tool_count: int = 0

    #: Whether the operator has switched controlled execution on (Stage 4E).
    #: False by default, and False is the state Mai ships in.
    execution_enabled: bool = False
    #: How many declared tools have an implementation behind them. Registry
    #: count again -- the length of the executable registry, not a guess.
    executable_tool_count: int = 0

    #: Every registered tool and how available it is. Derived from the
    #: registries by `app.runtime.capabilities.build`, never enumerated by
    #: hand -- which is what makes the rendered section follow the catalogue
    #: without anyone editing prompt text.
    capabilities: Tuple[ToolCapability, ...] = ()

    #: Optional, when the deployment supplies one.
    version: Optional[str] = None

    @property
    def usable_capabilities(self) -> Tuple[ToolCapability, ...]:
        """The tools Mai can actually perform right now."""
        return tuple(item for item in self.capabilities if item.usable)

    @property
    def can_execute_actions(self) -> bool:
        """Whether Mai can perform an action right now.

        **This guarantee changed in Stage 4E, and the change is a weakening.**

        Through Stage 4D this returned an unconditional `False`, and that was
        not a policy -- it was a description of the code. No `Tool` defined an
        `execute` method, no dispatcher existed, and no configuration could
        conjure one. The answer could not be anything else.

        Stage 4E built a dispatcher, so the honest answer is now conditional:
        two things must both hold, and either alone is not enough.

            execution_enabled       an operator switched it on, default off
            executable_tool_count   an implementation actually exists

        What survives: this is still a *derived* property, not a settable
        field, so nothing -- model output, request data, a caller holding an
        instance -- can assert it. It reports what the application is, and the
        application decides. What is gone: the answer no longer depends only
        on code that cannot change at runtime. `EXECUTION_ENABLED=true` in an
        environment file is now sufficient to make it true, which is exactly
        what "configuration-gated" means and exactly why the default is off.
        """
        return self.execution_enabled and self.executable_tool_count > 0


__all__ = ["RuntimeFacts", "ToolCapability"]
