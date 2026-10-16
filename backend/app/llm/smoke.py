"""Provider smoke test: one minimal, harmless request to the configured provider.

    docker exec -w /app mai-backend python -m app.llm.smoke

Builds the provider exactly as the application does (`build_provider`, from
settings), so it exercises the same factory, `SecureHttpClient` policy,
timeouts, retries and error mapping -- and nothing else. It touches no
database, starts no conversation, runs no tool and does not change the
running server's provider. The prompt is a fixed string with no user data.

Reports one log line of sanitised facts -- provider, model, outcome, the
error *code* on failure, latency and the reply's length; never its text, a
prompt, a header or a credential -- through Mai's own logging, so the
formatter, level filter and redaction policy apply as everywhere else. Exit
status 0 means the provider answered; 1 means it did not. A configured key is
not proof of a working provider; this is the check that is.
"""

import asyncio
import logging
import sys
import time
from typing import Any, Dict, Optional

from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import configure_logging, get_logger
from app.llm.base import LLMMessage
from app.llm.factory import build_provider

logger = get_logger(__name__)

#: Fixed, harmless and private-data-free by construction.
SMOKE_PROMPT = "Reply with exactly the word: ok"


async def run(settings: Optional[Settings] = None, provider=None) -> Dict[str, Any]:
    """One request through the configured provider. Never raises."""
    settings = settings or get_settings()
    result: Dict[str, Any] = {
        "provider": settings.LLM_PROVIDER,
        "model": None,
        "ok": False,
        "error_code": None,
        "latency_ms": None,
    }
    started = time.monotonic()
    try:
        provider = provider or build_provider(settings)
    except Exception as exc:  # unknown or unavailable provider: fails closed
        result["error_code"] = getattr(exc, "code", "provider_unavailable")
        return result
    result["provider"] = provider.name
    result["model"] = provider.model
    try:
        response = await provider.generate_response(
            [LLMMessage(role="user", content=SMOKE_PROMPT)]
        )
        result.update(
            ok=True,
            response_model=response.model,
            finish_reason=response.finish_reason,
            content_chars=len(response.content.strip()),
        )
    except LLMError as exc:
        result["error_code"] = exc.code
    finally:
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        await provider.close()
    return result


def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    result = asyncio.run(run(settings))
    logger.log(
        logging.INFO if result["ok"] else logging.WARNING,
        "Provider smoke test",
        extra={
            "provider": result["provider"],
            "model": result["model"],
            "ok": result["ok"],
            "error_code": result["error_code"],
            "latency_ms": result["latency_ms"],
            "response_model": result.get("response_model"),
            "finish_reason": result.get("finish_reason"),
            "content_chars": result.get("content_chars"),
        },
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
