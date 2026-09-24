"""What a mail turn produced. Application state, never model output."""

import enum
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class MailOutcome(str, enum.Enum):
    """What the mail layer did this turn.

    Explicit states, because "nothing happened" has several causes and each
    calls for a different sentence. Telling someone Gmail is not connected
    when execution is switched off sends them to the wrong place -- and
    telling them their mailbox is empty when the provider refused the request
    is the specific untruth this enum exists to prevent.
    """

    NOT_MAIL = "not_mail"
    #: Recognised, and put to the user. Nothing has been read.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    #: Read successfully.
    COMPLETED = "completed"
    #: The read was attempted and failed.
    FAILED = "failed"
    #: The user said no.
    DECLINED = "declined"
    #: The user moved on; the proposal is discarded.
    ABANDONED = "abandoned"
    #: Execution is switched off for this deployment.
    DISABLED = "disabled"
    #: No Google OAuth client is configured.
    NOT_CONFIGURED = "not_configured"
    #: Configured, but Gmail has not been connected. Distinct from Calendar
    #: being connected, which grants nothing here.
    NOT_CONNECTED = "not_connected"
    #: The grant no longer covers gmail.readonly, or was revoked.
    REAUTHORISATION_REQUIRED = "reauthorisation_required"
    #: A request to change the mailbox. Mai has no such capability.
    WRITE_NOT_SUPPORTED = "write_not_supported"


class MailResult(BaseModel):
    """The mail layer's report for one chat turn.

    Carries no token, no header, no endpoint, no scope and no message id -- a
    test pins the field set, so a future addition has to be a deliberate one.
    """

    model_config = ConfigDict(frozen=True)

    outcome: MailOutcome = MailOutcome.NOT_MAIL

    #: Application-written text to send instead of calling the model.
    reply: str = ""

    #: The rendered messages, as labelled personal data. Only set on success,
    #: and never stored: this field lives for the length of one request.
    messages_block: str = ""
    message_count: int = 0
    #: How many bodies were read, so the answer can be honest about depth.
    body_count: int = 0

    #: Which kind of mail question this was. Metadata about the *question*,
    #: never about the mail.
    intent: Optional[str] = Field(default=None, max_length=32)

    #: Whether the user asked which messages matter rather than for all of
    #: them. Metadata about the question, like `intent` -- it selects an
    #: application-written synthesis instruction and nothing else. It is not
    #: derived from any message and never reaches Gmail.
    priority: bool = False

    #: An application reason code. Never a Google message, never an exception.
    reason: Optional[str] = Field(default=None, max_length=64)

    execution_id: Optional[uuid.UUID] = None

    @property
    def has_reply(self) -> bool:
        return bool(self.reply)

    @property
    def needs_synthesis(self) -> bool:
        """Whether the model should answer this turn from the messages."""
        return self.outcome is MailOutcome.COMPLETED

    @property
    def touched_mail(self) -> bool:
        """Whether this turn involved the mailbox at all.

        Used for memory suppression, and deliberately true for the failure
        states as well: a turn that tried to read the user's mail is a turn
        about their mail, whether or not the provider answered.
        """
        return self.outcome is not MailOutcome.NOT_MAIL


__all__ = ["MailOutcome", "MailResult"]
