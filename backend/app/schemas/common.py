"""Shared response schemas."""

from typing import Optional

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: Optional[str] = None


class ErrorResponse(BaseModel):
    """The single error shape every failing endpoint returns."""

    error: ErrorDetail


class ComponentHealth(BaseModel):
    healthy: bool
    detail: Optional[str] = None
    #: LLM only. Observed from real requests, never probed:
    #: "unverified" (no request yet), "reachable" or "failing".
    reachability: Optional[str] = None


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    app: str
    environment: str
    database: ComponentHealth
    llm: ComponentHealth
