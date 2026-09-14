"""What a client is told about a mail turn.

Deliberately small, and deliberately carrying **no message content**. A client
learns that a read happened and how many messages were found; the messages
themselves are private personal data that reached the model to answer the
question, and handing them to a client as structured data would invite a UI to
render them as though Mai had said them.

Absent by design: the access token, the refresh token, the rendered messages,
senders, subjects, bodies, Gmail message ids, the rendered query, the
execution id and the OAuth scope.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.mail.schemas import MailOutcome, MailResult


class MailRead(BaseModel):
    """One turn's mail state, as the wire sees it."""

    model_config = ConfigDict(frozen=True)

    outcome: MailOutcome
    #: Which kind of mail question this was. Metadata about the *question*,
    #: never about the mail.
    intent: Optional[str] = Field(default=None, max_length=32)
    #: How many messages were found, and how many bodies were read. Counts
    #: only -- enough for a UI to say "read 3 messages" honestly.
    message_count: int = 0
    body_count: int = 0
    reason: Optional[str] = None

    @classmethod
    def from_result(cls, result: MailResult) -> Optional["MailRead"]:
        """None when the turn had nothing to do with mail."""
        if result is None or result.outcome is MailOutcome.NOT_MAIL:
            return None
        return cls(
            outcome=result.outcome,
            intent=result.intent,
            message_count=result.message_count,
            body_count=result.body_count,
            reason=result.reason,
        )


__all__ = ["MailRead"]
