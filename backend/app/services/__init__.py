"""Application services.

Re-exports are resolved lazily. `ChatService` sits above Stage 3A context
assembly, which in turn depends on `ConversationService` from this same
package -- so importing both eagerly here makes `app.context.service` pull
`app.services` back in mid-initialisation and fail. Deferring the lookup to
first attribute access keeps `from app.services import ChatService` working
without forcing an import order the layering cannot satisfy.
"""

from typing import Any

from app.services.conversation_service import ConversationService

__all__ = ["ChatService", "ConversationService"]


def __getattr__(name: str) -> Any:
    if name == "ChatService":
        from app.services.chat_service import ChatService

        return ChatService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
