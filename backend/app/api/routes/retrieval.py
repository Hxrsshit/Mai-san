"""Retrieval debug endpoints.

These exist to make retrieval decisions inspectable: which entities matched,
which memories were considered, what each scored, and what survived the budget.
Without this, ranking quality is impossible to judge.

No secrets are exposed -- only knowledge already retrievable through the
memory, entity and relationship APIs.
"""

import uuid
from typing import List

from fastapi import APIRouter, Query

from app.api.deps import Conversations, Retrieval
from app.retrieval.schemas import (
    RetrievalResult,
    DebugScoredMemory,
    RetrievalDebugRequest,
    RetrievalDebugResponse,
    RetrievedEntity,
    RetrievedRelationship,
)
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/api/retrieval", tags=["retrieval"])


@router.post(
    "/debug",
    response_model=RetrievalDebugResponse,
    summary="Explain what retrieval would return for a query",
)
async def debug_retrieval(
    payload: RetrievalDebugRequest, retrieval: Retrieval
) -> RetrievalDebugResponse:
    """Run retrieval and expose every intermediate step.

    Shows candidates *and* selections, so it is visible not just what was
    chosen but what was considered and rejected.
    """
    analysis = await retrieval.analyse_only(payload.query)

    (
        matches,
        entity_strength,
        relationship_candidates,
        evidence,
        pool,
    ) = await retrieval.candidates_for_debug(analysis)

    ranked_memories = retrieval.ranker.rank_memories(
        pool, analysis.keywords, entity_strength
    )
    keyword_matched = {c.memory.id for c in pool if c.keyword_hits}
    ranked_relationships = retrieval.ranker.rank_relationships(
        relationship_candidates, evidence, keyword_matched
    )

    # The real package, so debug output matches what chat would actually use.
    package: RetrievalResult = await retrieval.retrieve(payload.query)
    selected_memory_ids = {memory.id for memory in package.memories}

    matched_entities: List[RetrievedEntity] = [
        RetrievedEntity(
            id=match.entity.id,
            canonical_name=match.entity.canonical_name,
            entity_type=match.entity.entity_type.value,
            description=match.entity.description,
            match_strength=match.strength,
            matched_via=match.matched_via,
            matched_text=match.matched_text,
            rank=position,
        )
        for position, match in enumerate(matches, start=1)
    ]

    candidate_memories = [
        DebugScoredMemory(
            id=memory.id,
            content=memory.content,
            memory_type=memory.memory_type,
            score=memory.score,
            signals=memory.signals,
            selected=memory.id in selected_memory_ids,
        )
        for memory in ranked_memories
    ]

    return RetrievalDebugResponse(
        query=payload.query,
        normalized_query=analysis.normalized,
        keywords=list(analysis.keywords),
        matched_entities=matched_entities,
        candidate_memories=candidate_memories,
        candidate_relationships=ranked_relationships,
        selected_memories=package.memories,
        selected_entities=package.matched_entities,
        selected_relationships=package.relationships,
        assembled_context=retrieval.render(package),
        metadata=package.metadata,
        weights=retrieval.ranker.weights,
    )


conversation_router = APIRouter(prefix="/api/conversations", tags=["retrieval"])


@conversation_router.get(
    "/{conversation_id}/context-preview",
    response_model=RetrievalDebugResponse,
    responses={404: {"model": ErrorResponse, "description": "Conversation not found"}},
    summary="What knowledge would be assembled for this conversation now",
)
async def context_preview(
    conversation_id: uuid.UUID,
    conversations: Conversations,
    retrieval: Retrieval,
    query: str = Query(
        default=None,
        description="Query to preview. Defaults to the conversation's last user message.",
    ),
) -> RetrievalDebugResponse:
    """Preview retrieval for a conversation's current state.

    With no `query`, the conversation's most recent user message is used --
    which is exactly what the next turn would retrieve against.
    """
    await conversations.get_conversation(conversation_id)

    if not query:
        messages = await conversations.get_messages(conversation_id)
        user_messages = [m for m in messages if m.role.value == "user"]
        query = user_messages[-1].content if user_messages else ""

    if not query:
        # Nothing to retrieve against yet.
        return RetrievalDebugResponse(
            query="",
            normalized_query="",
            keywords=[],
            matched_entities=[],
            candidate_memories=[],
            candidate_relationships=[],
            selected_memories=[],
            selected_entities=[],
            selected_relationships=[],
            assembled_context="",
            metadata=RetrievalResult().metadata,
            weights=retrieval.ranker.weights,
        )

    return await debug_retrieval(RetrievalDebugRequest(query=query), retrieval)


__all__ = ["router", "conversation_router", "RetrievedRelationship"]
