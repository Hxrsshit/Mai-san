"""Turns a conversation turn into validated memory candidates.

The extractor owns the untrusted boundary: it calls the model through the
generic provider interface, then parses and validates whatever comes back.
Nothing here writes to the database.
"""

import json
import re
from typing import List, Optional

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import get_logger
from app.llm.base import LLMMessage, LLMProvider
from app.memory.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_user_prompt,
)
from app.memory.schemas import MemoryCandidate, MemoryExtractionResult

logger = get_logger(__name__)

# Models sometimes wrap JSON in a markdown fence despite instructions.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class MemoryExtractor:
    """Produces validated `MemoryCandidate`s from one conversation turn."""

    def __init__(
        self, provider: LLMProvider, settings: Optional[Settings] = None
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    async def extract(
        self,
        user_message: str,
        assistant_message: str,
        recent_context: Optional[List[str]] = None,
    ) -> List[MemoryCandidate]:
        """Return validated candidates, or an empty list.

        Never raises: extraction is best-effort and must not be able to affect
        the chat turn that triggered it. Every failure path logs and returns [].
        """
        messages = [
            LLMMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=build_extraction_user_prompt(
                    user_message=user_message,
                    assistant_message=assistant_message,
                    recent_context=recent_context,
                ),
            ),
        ]

        try:
            response = await self._provider.generate_response(
                messages,
                # Low temperature: extraction is a classification task, not
                # creative writing.
                temperature=self._settings.MEMORY_EXTRACTION_TEMPERATURE,
                max_tokens=self._settings.MEMORY_EXTRACTION_MAX_TOKENS,
                json_mode=True,
            )
        except LLMError as exc:
            logger.warning(
                "Memory extraction failed at the model call",
                extra={"error_code": exc.code, "provider": self._provider.name},
            )
            return []
        except Exception as exc:  # noqa: BLE001 - must never escape
            logger.error(
                "Unexpected error during memory extraction",
                extra={"error": str(exc)},
                exc_info=exc,
            )
            return []

        return self._parse(response.content)

    # --- Parsing / validation ----------------------------------------------

    def _parse(self, raw: str) -> List[MemoryCandidate]:
        payload = self._load_json(raw)
        if payload is None:
            return []

        try:
            result = MemoryExtractionResult.model_validate(payload)
        except ValidationError as exc:
            # A malformed batch should not discard well-formed entries, so
            # fall back to validating each candidate on its own.
            logger.warning(
                "Memory extraction payload failed validation",
                extra={"error_count": exc.error_count()},
            )
            return self._salvage_candidates(payload)

        if not result.should_store_memory or not result.memories:
            logger.info("Memory extraction produced no candidates")
            return []

        return result.memories

    @staticmethod
    def _load_json(raw: str) -> Optional[dict]:
        """Best-effort JSON extraction from a model response."""
        if not raw or not raw.strip():
            logger.warning("Memory extraction returned an empty response")
            return None

        text = raw.strip()
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # Some models prepend prose; try the outermost JSON object.
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                logger.warning("Memory extraction returned no parsable JSON")
                return None
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("Memory extraction returned malformed JSON")
                return None

        if not isinstance(payload, dict):
            logger.warning("Memory extraction returned a non-object payload")
            return None
        return payload

    @staticmethod
    def _salvage_candidates(payload: dict) -> List[MemoryCandidate]:
        """Keep individually valid candidates from an otherwise bad payload."""
        raw_memories = payload.get("memories")
        if not isinstance(raw_memories, list):
            return []

        salvaged: List[MemoryCandidate] = []
        rejected = 0
        for entry in raw_memories[:5]:
            try:
                salvaged.append(MemoryCandidate.model_validate(entry))
            except ValidationError:
                rejected += 1

        if rejected:
            logger.warning(
                "Rejected invalid memory candidates",
                extra={"rejected": rejected, "kept": len(salvaged)},
            )
        return salvaged
