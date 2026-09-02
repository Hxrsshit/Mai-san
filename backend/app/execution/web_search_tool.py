"""The `web_search` executable tool.

Twelve lines of behaviour, and the shape is the point: a query goes in, an
integration call comes out. There is no URL, no endpoint, no method and no
header anywhere in this file, so there is nothing for a request, a plan or a
model reply to redirect.
"""

from typing import Any, Dict, Type

from app.execution.integration_tools import AsyncIntegrationTool
from app.tools.base import ToolArguments
from app.tools.catalog import WebSearchArguments


class WebSearchTool(AsyncIntegrationTool):
    """Search the public web. Read-only, bounded, approved per query."""

    name = "web_search"
    integration_name = "web_search"
    operation = "search"

    @property
    def arguments_model(self) -> Type[ToolArguments]:
        return WebSearchArguments

    def build_operation_arguments(self, arguments) -> Dict[str, Any]:
        # Three values cross the boundary. Not the argument object, not a
        # context, not a conversation, not a memory -- and nothing the user
        # did not put in the approved payload.
        return {
            "query": arguments.query,
            "max_results": arguments.max_results,
            "safe_search": arguments.safe_search,
        }


__all__ = ["WebSearchTool"]
