"""
app/core/exceptions.py

Custom exceptions → consistent JSON error responses.
Register the handlers in app/main.py.
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base application error."""
    status_code: int = 500
    detail: str = "Internal server error"

    def __init__(self, detail: str | None = None, status_code: int | None = None):
        self.detail = detail or self.__class__.detail
        self.status_code = status_code or self.__class__.status_code
        super().__init__(self.detail)


class NotFoundError(AppError):
    status_code = 404
    detail = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    detail = "Conflict with existing resource"


class ValidationError(AppError):
    status_code = 422
    detail = "Validation failed"


class ForbiddenError(AppError):
    status_code = 403
    detail = "You do not have permission to perform this action"


class GRNLockedError(AppError):
    status_code = 409
    detail = "This GRN is confirmed and cannot be modified"


class EtimsSubmissionError(AppError):
    status_code = 502
    detail = "eTIMS submission failed"


# ── FastAPI exception handlers ────────────────────────────────────────────────

async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.__class__.__name__,
            "detail": exc.detail,
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    import logging
    logging.getLogger("app").exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": "InternalServerError", "detail": "Something went wrong"},
    )