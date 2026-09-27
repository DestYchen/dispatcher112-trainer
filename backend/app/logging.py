import json
import logging
import logging.config
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.metrics import record_request
from app.operations.diagnostics import METHODS, save_http_failure


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "request_id": getattr(record, "request_id", None),
            "user_id": getattr(record, "user_id", None),
            "route": getattr(record, "route", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


LOG_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "app.logging.JSONFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "uvicorn": {"handlers": [], "propagate": True},
        "uvicorn.error": {"handlers": [], "propagate": True},
        "uvicorn.access": {"handlers": [], "propagate": False, "level": "WARNING"},
        "arq": {"handlers": [], "propagate": True},
    },
}


def configure_logging() -> None:
    logging.config.dictConfig(LOG_CONFIG)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        start = perf_counter()
        status = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode("ascii")),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            status = 500
            scope["state"]["error_code"] = "INTERNAL_ERROR"
            logging.getLogger("app.errors").exception(
                "Unhandled request error", extra={"request_id": request_id}
            )
            if not response_started:
                from starlette.responses import JSONResponse

                response = JSONResponse(
                    {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "Внутренняя ошибка сервера.",
                            "details": {},
                            "request_id": request_id,
                        }
                    },
                    status_code=500,
                )
                await response(scope, receive, send_with_id)
            else:
                raise
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            record_request(scope["method"], route, status, perf_counter() - start)
            if status >= 500 and getattr(
                getattr(scope.get("app"), "state", None), "capture_system_errors", False
            ):
                await save_http_failure(
                    {
                        "request_id": request_id,
                        "method": scope["method"] if scope["method"] in METHODS else "OTHER",
                        "route": route,
                        "status": status,
                        "code": scope["state"].get("error_code", "HTTP_ERROR"),
                    }
                )
            logging.getLogger("app.requests").info(
                "HTTP %s %s",
                scope["method"],
                status,
                extra={
                    "request_id": request_id,
                    "user_id": scope["state"].get("user_id"),
                    "route": route,
                    "duration_ms": round((perf_counter() - start) * 1000, 3),
                },
            )
