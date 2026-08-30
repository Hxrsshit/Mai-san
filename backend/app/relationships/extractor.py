"""Turns a memory plus its entities into validated relationship candidates.

Owns the untrusted boundary: calls the model through the generic provider
interface, then parses and validates. Writes nothing, and resolves nothing --
entity resolution happens in the service, against entities that already exist.
"""

import json
import re
from typing import List, Optional, Sequence

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import get_logger
from app.llm.base import LLMMessage, LLMProvider
from app.relationships.prompts import (
    build_extraction_system_prompt,
    build_extraction_user_prompt,
)
from app.relationships.schemas import (
    RelationshipCandidate,
    RelationshipExtractionResult,
)

logger = get_logger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class RelationshipExtractor:
    """Produces validated `RelationshipCandidate`s from one memory."""

    def __init__(
        self, provider: LLMProvider, settings: Optional[Settings] = None
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    async def extract(
        self,
        memory_content: str,
        memory_type: str,
        entity_names: Sequence[str],
    ) -> List[RelationshipCandidate]:
        """Return validated candidates, or an empty list.

        Never raises. Relationship extraction runs after the memory and its
        entities are already committed, so a failure must stay contained.
        """
        if len(entity_names) < 2:
            # Nothing to relate. Guarded here as well as by the caller so the
            # extractor cannot be misused into inventing a relationship.
            return []

        messages = [
            LLMMessage(role="system", content=build_extraction_system_prompt()),
            LLMMessage(
                role="user",
                content=build_extraction_user_prompt(
                    memory_content, memory_type, entity_names
                ),
            ),
        ]

        try:
            response = await self._provider.generate_response(
                messages,
                temperature=self._settings.RELATIONSHIP_EXTRACTION_TEMPERATURE,
                max_tokens=self._settings.RELATIONSHIP_EXTRACTION_MAX_TOKENS,
                json_mode=True,
            )
        except LLMError as exc:
            logger.warning(
                "Relationship extraction failed at the model call",
                extra={"error_code": exc.code, "provider": self._provider.name},
            )
            return []
        except Exception as exc:  # noqa: BLE001 - must never escape
            logger.error(
                "Unexpected error during relationship extraction",
                extra={"error": str(exc)},
                exc_info=exc,
            )
            return []

        return self._parse(response.content)

    # --- Parsing / validation ----------------------------------------------

    def _parse(self, raw: str) -> List[RelationshipCandidate]:
        payload = self._load_json(raw)
        if payload is None:
            return []

        try:
            result = RelationshipExtractionResult.model_validate(payload)
            candidates = result.relationships
        except ValidationError as exc:
            logger.warning(
                "Relationship extraction payload failed validation",
                extra={"error_count": exc.error_count()},
            )
            candidates = self._salvage(payload)

        return self._cap(self._deduplicate(candidates))

    @staticmethod
    def _deduplicate(
        candidates: Sequence[RelationshipCandidate],
    ) -> List[RelationshipCandidate]:
        """Drop repeats within a single batch, keeping the most confident."""
        best = {}
        for candidate in candidates:
            key = (
                candidate.source_entity.lower(),
                candidate.relationship_type,
                candidate.target_entity.lower(),
            )
            if key not in best or candidate.confidence_score > best[key].confidence_score:
                best[key] = candidate
        return list(best.values())

    def _cap(
        self, candidates: List[RelationshipCandidate]
    ) -> List[RelationshipCandidate]:
        limit = self._settings.RELATIONSHIP_EXTRACTION_MAX_PER_MEMORY
        if len(candidates) > limit:
            logger.warning(
                "Relationship extraction over-extracted; truncating",
                extra={"proposed": len(candidates), "limit": limit},
            )
        return candidates[:limit]

    @staticmethod
    def _load_json(raw: str) -> Optional[dict]:
        if not raw or not raw.strip():
            logger.warning("Relationship extraction returned an empty response")
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
                logger.warning("Relationship extraction returned no parsable JSON")
                return None
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("Relationship extraction returned malformed JSON")
                return None

        if not isinstance(payload, dict):
            logger.warning("Relationship extraction returned a non-object payload")
            return None
        return payload

    @staticmethod
    def _salvage(payload: dict) -> List[RelationshipCandidate]:
        """Keep individually valid candidates from an otherwise bad payload."""
        raw_items = payload.get("relationships")
        if not isinstance(raw_items, list):
            return []

        kept: List[RelationshipCandidate] = []
        rejected = 0
        for entry in raw_items:
            try:
                kept.append(RelationshipCandidate.model_validate(entry))
            except ValidationError:
                rejected += 1

        if rejected:
            logger.warning(
                "Rejected invalid relationship candidates",
                extra={"rejected": rejected, "kept": len(kept)},
            )
        return kept
