"""Turns a stored memory into validated entity candidates.

Owns the untrusted boundary: calls the model through the generic provider
interface, then parses and validates whatever comes back. Writes nothing.
"""

import json
import re
from typing import List, Optional

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import get_logger
from app.entities.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    build_extraction_user_prompt,
)
from app.entities.schemas import EntityCandidate, EntityExtractionResult
from app.llm.base import LLMMessage, LLMProvider

logger = get_logger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class EntityExtractor:
    """Produces validated `EntityCandidate`s from one memory statement."""

    def __init__(
        self, provider: LLMProvider, settings: Optional[Settings] = None
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    async def extract(
        self, memory_content: str, memory_type: str
    ) -> List[EntityCandidate]:
        """Return validated candidates, or an empty list.

        Never raises. Entity extraction runs after the memory is already
        committed, so a failure here must stay contained.
        """
        messages = [
            LLMMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=build_extraction_user_prompt(memory_content, memory_type),
            ),
        ]

        try:
            response = await self._provider.generate_response(
                messages,
                temperature=self._settings.ENTITY_EXTRACTION_TEMPERATURE,
                max_tokens=self._settings.ENTITY_EXTRACTION_MAX_TOKENS,
                json_mode=True,
            )
        except LLMError as exc:
            logger.warning(
                "Entity extraction failed at the model call",
                extra={"error_code": exc.code, "provider": self._provider.name},
            )
            return []
        except Exception as exc:  # noqa: BLE001 - must never escape
            logger.error(
                "Unexpected error during entity extraction",
                extra={"error": str(exc)},
                exc_info=exc,
            )
            return []

        return self._parse(response.content)

    # --- Parsing / validation ----------------------------------------------

    def _parse(self, raw: str) -> List[EntityCandidate]:
        payload = self._load_json(raw)
        if payload is None:
            return []

        try:
            result = EntityExtractionResult.model_validate(payload)
            candidates = result.entities
        except ValidationError as exc:
            # A malformed batch must not discard well-formed entries.
            logger.warning(
                "Entity extraction payload failed validation",
                extra={"error_count": exc.error_count()},
            )
            candidates = self._salvage(payload)

        return self._cap(candidates)

    def _cap(self, candidates: List[EntityCandidate]) -> List[EntityCandidate]:
        """Bound the batch. A long list means the model is over-extracting."""
        limit = self._settings.ENTITY_EXTRACTION_MAX_PER_MEMORY
        if len(candidates) > limit:
            logger.warning(
                "Entity extraction over-extracted; truncating",
                extra={"proposed": len(candidates), "limit": limit},
            )
        return candidates[:limit]

    @staticmethod
    def _load_json(raw: str) -> Optional[dict]:
        if not raw or not raw.strip():
            logger.warning("Entity extraction returned an empty response")
            return None

        text = raw.strip()
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                logger.warning("Entity extraction returned no parsable JSON")
                return None
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("Entity extraction returned malformed JSON")
                return None

        if not isinstance(payload, dict):
            logger.warning("Entity extraction returned a non-object payload")
            return None
        return payload

    @staticmethod
    def _salvage(payload: dict) -> List[EntityCandidate]:
        """Keep individually valid candidates from an otherwise bad payload."""
        raw_entities = payload.get("entities")
        if not isinstance(raw_entities, list):
            return []

        kept: List[EntityCandidate] = []
        rejected = 0
        for entry in raw_entities:
            try:
                kept.append(EntityCandidate.model_validate(entry))
            except ValidationError:
                rejected += 1

        if rejected:
            logger.warning(
                "Rejected invalid entity candidates",
                extra={"rejected": rejected, "kept": len(kept)},
            )
        return kept
