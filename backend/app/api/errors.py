"""Exception handlers producing a single, stable error envelope."""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import DatabaseError, MaiError
from app.core.logging import get_logger, get_request_id

logger = get_logger(__name__)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": get_request_id(),
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(MaiError)
    async def handle_mai_error(_: Request, exc: MaiError) -> JSONResponse:
        # Expected, already-classified failures: log at the level they warrant.
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "Request failed",
            extra={"code": exc.code, "status_code": exc.status_code},
        )
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Surface the first problem in a form a UI can show directly.
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        detail = first.get("msg", "The request was invalid.")
        message = f"{location}: {detail}" if location else detail

        logger.warning("Request validation failed", extra={"detail": message})
        return _error_response(
            422, "validation_error", message
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return _error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(SQLAlchemyError)
    async def handle_sqlalchemy_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("Unhandled database error", exc_info=exc)
        error = DatabaseError()
        return _error_response(error.status_code, error.code, error.message)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to the client; the traceback goes to the logs.
        logger.error("Unhandled server error", exc_info=exc)
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred.",
        )
