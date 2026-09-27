import asyncio
import gc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from starlette.exceptions import HTTPException

from app import runtime_configuration
from app.api import deps
from app.api.admin import router as admin_router
from app.api.admin_configuration import router as admin_configuration_router
from app.api.admin_diagnostics import router as admin_diagnostics_router
from app.api.admin_operations import monitor_operations
from app.api.admin_operations import router as admin_operations_router
from app.api.admin_policy import router as admin_policy_router
from app.api.admin_system import router as admin_system_router
from app.api.admin_updates import router as admin_updates_router
from app.api.admin_users import router as admin_users_router
from app.api.analytics import router as analytics_router
from app.api.auth import router as auth_router
from app.api.card_entry import router as card_entry_router
from app.api.errors import APIError, handle_api_error
from app.api.generation import router as generation_router
from app.api.history import router as history_router
from app.api.learning import router as learning_router
from app.api.phone import router as phone_router
from app.api.reports import router as reports_router
from app.api.sip import router as sip_router
from app.api.student import router as student_router
from app.api.teacher import router as teacher_router
from app.api.teacher_live import router as teacher_live_router
from app.config import settings
from app.logging import RequestContextMiddleware, configure_logging
from app.metrics import render_metrics
from app.realtime.clock import run_clock
from app.realtime.relay import run_relay
from app.realtime.socket import router as socket_router
from app.scoring import grammar
from app.transport_security import http_verify, redis_tls

configure_logging()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Imported schemas/font libraries live for the process lifetime. Excluding this
    # static graph avoids measured 60 ms full-heap pauses; request GC stays enabled.
    gc.collect()
    gc.freeze()
    grammar.shared_client = httpx.AsyncClient(timeout=1.5, trust_env=False, verify=http_verify())
    deps.shared_cache = Redis.from_url(settings.redis_url, decode_responses=True, **redis_tls())
    from app.realtime.hub import hub

    await hub.start(deps.shared_cache)
    configuration = await runtime_configuration.start("backend")
    clock = asyncio.create_task(run_clock())
    relay = asyncio.create_task(run_relay())
    operations = asyncio.create_task(monitor_operations())
    application.state.capture_system_errors = True
    try:
        yield
    finally:
        application.state.capture_system_errors = False
        clock.cancel()
        relay.cancel()
        operations.cancel()
        configuration.cancel()
        with suppress(asyncio.CancelledError):
            await clock
        with suppress(asyncio.CancelledError):
            await relay
        with suppress(asyncio.CancelledError):
            await operations
        with suppress(asyncio.CancelledError):
            await configuration
        await grammar.shared_client.aclose()
        grammar.shared_client = None
        await deps.shared_cache.aclose()
        hub.cache = None
        deps.shared_cache = None
        gc.unfreeze()


app = FastAPI(title="АРМ-112 · учебный режим", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(RequestContextMiddleware)
app.add_exception_handler(APIError, handle_api_error)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(teacher_router, prefix="/api/v1")
app.include_router(teacher_live_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(admin_configuration_router, prefix="/api/v1")
app.include_router(admin_diagnostics_router, prefix="/api/v1")
app.include_router(admin_operations_router, prefix="/api/v1")
app.include_router(admin_updates_router, prefix="/api/v1")
app.include_router(admin_policy_router, prefix="/api/v1")
app.include_router(student_router, prefix="/api/v1")
app.include_router(card_entry_router, prefix="/api/v1")
app.include_router(phone_router, prefix="/api/v1")
app.include_router(sip_router, prefix="/api/v1")
app.include_router(learning_router, prefix="/api/v1")
app.include_router(history_router, prefix="/api/v1")
app.include_router(analytics_router, prefix="/api/v1")
app.include_router(reports_router, prefix="/api/v1")
app.include_router(admin_users_router, prefix="/api/v1")
app.include_router(admin_system_router, prefix="/api/v1")
app.mount("/media/voices", StaticFiles(directory="/data/voices", check_dir=False), name="voices")
app.include_router(generation_router, prefix="/api/v1")
app.include_router(socket_router, prefix="/api/v1")


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(render_metrics(), media_type="text/plain; version=0.0.4")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    messages = {
        404: ("NOT_FOUND", "Объект не найден."),
        405: ("VALIDATION_ERROR", "Метод запроса не поддерживается."),
    }
    code, message = messages.get(exc.status_code, ("VALIDATION_ERROR", "Запрос отклонён."))
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "details": {},
                "request_id": request.state.request_id,
            }
        },
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Проверьте поля запроса.",
                "details": {"fields": [list(error["loc"]) for error in exc.errors()]},
                "request_id": request.state.request_id,
            }
        },
        status_code=400,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
