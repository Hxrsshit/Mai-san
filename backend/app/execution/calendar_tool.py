"""The `calendar_list_events` executable tool.

The same shape as `web_search`: a bounded payload goes in, an integration
operation comes out. No URL, no endpoint, no method, no header, and no token
anywhere in this file -- so there is nothing here for a request, a plan or a
model reply to redirect.

There is deliberately no `calendar_create_event`, `calendar_update_event` or
`calendar_delete_event` in this module or anywhere else. They are absent
rather than disabled: a capability that does not exist cannot be reached by a
bug or a mistaken authorization decision, and the OAuth scope Mai holds would
have Google refuse a write in any case.
"""

from typing import Any, Dict, Type

from app.execution.integration_tools import AsyncIntegrationTool
from app.tools.base import ToolArguments
from app.tools.catalog import CalendarListEventsArguments


class CalendarListEventsTool(AsyncIntegrationTool):
    """Read a bounded window of the user's calendar. Read-only."""

    name = "calendar_list_events"
    integration_name = "google_calendar"
    operation = "calendar_list_events"

    @property
    def arguments_model(self) -> Type[ToolArguments]:
        return CalendarListEventsArguments

    def build_operation_arguments(self, arguments) -> Dict[str, Any]:
        # What crosses the boundary: two timestamps, a count, which of two
        # renderings to use, and the user's own words for the window so the
        # block can name it. Not the conversation, not a memory, not a context
        # package, and nothing the application did not compute from the
        # user's question.
        return {
            "starts_at": arguments.starts_at,
            "ends_at": arguments.ends_at,
            "max_results": arguments.max_results,
            # A member of a closed enum, chosen by the application from the
            # recognised question. It selects between two renderings that
            # already exist; it cannot widen the read.
            "intent": arguments.intent.value,
            "window_label": arguments.window_label,
        }


__all__ = ["CalendarListEventsTool"]
