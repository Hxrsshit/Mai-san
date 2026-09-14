"""The two Gmail executable tools.

The same shape as `calendar_list_events`: a bounded payload goes in, an
integration operation comes out. No URL, no endpoint, no method, no header and
no token anywhere in this file -- so there is nothing here for a request, a
plan, a model reply or an email to redirect.

There is deliberately no `gmail_send_message`, `gmail_reply`,
`gmail_forward`, `gmail_trash`, `gmail_archive`, `gmail_modify_labels`,
`gmail_create_draft` or `gmail_request` in this module or anywhere else. They
are absent rather than disabled: a capability that does not exist cannot be
reached by a bug or a mistaken authorization decision, and the read-only scope
Mai holds would have Google refuse a write in any case.
"""

from typing import Any, Dict, Type

from app.execution.integration_tools import AsyncIntegrationTool
from app.tools.base import ToolArguments
from app.tools.catalog import GmailGetMessageArguments, GmailListMessagesArguments


class GmailListMessagesTool(AsyncIntegrationTool):
    """List bounded metadata for the user's messages. Read-only."""

    name = "gmail_list_messages"
    integration_name = "google_gmail"
    operation = "gmail_list_messages"

    @property
    def arguments_model(self) -> Type[ToolArguments]:
        return GmailListMessagesArguments

    def build_operation_arguments(self, arguments) -> Dict[str, Any]:
        # Named one at a time rather than dumped wholesale. A `model_dump()`
        # here would forward any field the schema ever gains, which is how a
        # future addition reaches the provider without anyone deciding it
        # should.
        return {
            "sender": arguments.sender,
            "subject_terms": list(arguments.subject_terms),
            "text_terms": list(arguments.text_terms),
            "unread_only": arguments.unread_only,
            "newer_than_days": arguments.newer_than_days,
            "max_results": arguments.max_results,
            "body_count": arguments.body_count,
        }


class GmailGetMessageTool(AsyncIntegrationTool):
    """Read one selected message. Read-only."""

    name = "gmail_get_message"
    integration_name = "google_gmail"
    operation = "gmail_get_message"

    @property
    def arguments_model(self) -> Type[ToolArguments]:
        return GmailGetMessageArguments

    def build_operation_arguments(self, arguments) -> Dict[str, Any]:
        return {"message_id": arguments.message_id}


__all__ = ["GmailGetMessageTool", "GmailListMessagesTool"]
