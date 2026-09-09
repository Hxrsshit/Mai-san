"""What a research turn produced. Application state, never model output."""

import enum
import uuid
from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.search import MAX_QUERY_CHARS


class ResearchOutcome(str, enum.Enum):
    """What the research layer did on this turn.

    Explicit states rather than a boolean, for the reason every enum in this
    codebase is explicit: "nothing happened" has several causes and they call
    for completely different things to say to the user. "I did not search
    because you have not configured a provider" and "I did not search because
    you declined" are both no, and telling someone the wrong one is a lie of
    a small but avoidable kind.
    """

    #: Nothing about this turn concerned research.
    NOT_RESEARCH = "not_research"
    #: A search was identified and is waiting for the user to confirm.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    #: The user confirmed and the search ran.
    COMPLETED = "completed"
    #: The user confirmed and the search failed.
    FAILED = "failed"
    #: The user declined.
    DECLINED = "declined"
    #: A pending proposal was dropped because the next turn was about
    #: something else.
    ABANDONED = "abandoned"
    #: Research is switched off for this deployment.
    DISABLED = "disabled"
    #: No search provider is configured, so there is nothing to search with.
    NOT_CONFIGURED = "not_configured"
    #: A research request whose subject could not be determined. Mai asks
    #: what to search for rather than guessing -- guessing here would send a
    #: query nobody wrote to a third party.
    NEEDS_CLARIFICATION = "needs_clarification"


#: Outcomes where a search genuinely ran and returned results.
#:
#: One member, and written as membership rather than as "not a failure" so a
#: state added later is unsuccessful by default.
SUCCESSFUL_OUTCOMES = frozenset({ResearchOutcome.COMPLETED})


class ResearchResult(BaseModel):
    """The research layer's report for one chat turn.

    Returned to the caller and used by the chat service to decide what to do.
    It is **not** handed to the formatter as-is: only `results_block`, the
    rendered untrusted content, reaches a prompt, and only when a search
    actually succeeded.
    """

    model_config = ConfigDict(frozen=True)

    outcome: ResearchOutcome = ResearchOutcome.NOT_RESEARCH

    #: The query, when there is one. Bounded by the search layer's own limit.
    query: str = Field(default="", max_length=MAX_QUERY_CHARS)
    #: The execution record backing this, when one was created.
    execution_id: Optional[uuid.UUID] = None

    #: A deterministic, application-written message to send instead of
    #: calling the model. Present for every outcome that does not end in
    #: synthesis -- a confirmation prompt is application text, not something
    #: a model should be trusted to phrase.
    reply: str = ""

    #: The rendered search results, as untrusted external content. Only ever
    #: set when `outcome is COMPLETED`.
    results_block: str = ""
    result_count: int = 0

    #: An application reason code when something went wrong. Never a provider
    #: message and never an exception string.
    reason: Optional[str] = Field(default=None, max_length=64)

    #: Research adds no model call of its own. Identification, confirmation
    #: and proposal text are all deterministic; the only model call on a
    #: completed turn is the one the chat path was already making.
    model_calls: int = 0

    @property
    def succeeded(self) -> bool:
        return self.outcome in SUCCESSFUL_OUTCOMES

    @property
    def has_reply(self) -> bool:
        """Whether the application is answering instead of the model."""
        return bool(self.reply)


__all__ = ["SUCCESSFUL_OUTCOMES", "ResearchOutcome", "ResearchResult"]
