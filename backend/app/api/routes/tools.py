"""Tool registry and authorization endpoints.

Two read-only endpoints. Neither runs anything, and neither could: no tool in
this codebase defines a way to be run.

`POST /api/tools/authorize` is a **dry run**. It answers "would this be
permitted?" and returns a decision. The decision is not a token, a handle or a
grant -- there is nothing that accepts one.
"""

from fastapi import APIRouter

from app.api.deps import Authorization, Tools
from app.tools.schemas import (
    ActionProposal,
    AuthorizationRead,
    AuthorizationRequest,
    ToolListRead,
    ToolRead,
)

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get(
    "",
    response_model=ToolListRead,
    summary="List the tools the application has declared",
)
async def list_tools(tools: Tools) -> ToolListRead:
    """Every registered declaration, name-sorted.

    Registration happens in application code at import time. Nothing reachable
    from a request can add to this list, and the definitions returned are
    frozen -- a caller cannot edit one into a different capability.
    """
    definitions = tools.list_registered()
    return ToolListRead(
        items=[ToolRead.model_validate(definition) for definition in definitions],
        total=len(definitions),
    )


@router.post(
    "/authorize",
    response_model=AuthorizationRead,
    summary="Ask whether an action would be permitted, without doing it",
)
async def authorize_action(
    payload: AuthorizationRequest, authorization: Authorization
) -> AuthorizationRead:
    """Evaluate a proposed action against the registry and policy.

    Returns one of four explicit states: `unknown_tool`, `forbidden`,
    `approval_required` or `allowed`. `allowed` means the authorization layer
    does not forbid the action; it does not mean anything happened, and
    nothing did.

    Any `approved`, `requires_approval` or `risk_level` field in the request
    body is dropped before it is read -- those are not fields on a proposal,
    so there is nothing for them to override.
    """
    proposal = ActionProposal(
        tool_name=payload.tool_name,
        arguments=payload.arguments,
        source=payload.source,
    )
    return AuthorizationRead.from_decision(authorization.authorize(proposal))
