from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


async def handle_api_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, APIError)
    request.state.error_code = error.code
    return JSONResponse(
        {
            "error": {
                "code": error.code,
                "message": error.message,
                "details": error.details,
                "request_id": request.state.request_id,
            }
        },
        status_code=error.status,
    )
