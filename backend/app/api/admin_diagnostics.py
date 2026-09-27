from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import DB, Admin
from app.db.models import AuditLog
from app.domain.pagination import page_offset
from app.operations.backup_client import file_integrity
from app.operations.diagnostics import report_period, technical_report

router = APIRouter(prefix="/admin/diagnostics", tags=["admin"])


@router.get("/files")
async def files(user: Admin) -> JSONResponse:
    return JSONResponse(await file_integrity(), headers={"Cache-Control": "private, no-store"})


@router.get("/report")
async def report(
    db: DB,
    user: Admin,
    request: Request,
    from_at: Annotated[datetime | None, Query(alias="from")] = None,
    to_at: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    export: bool = False,
) -> JSONResponse:
    start, end = report_period(from_at, to_at, datetime.now(UTC))
    routes = {route.path for route in request.app.routes if hasattr(route, "path")}
    value = await technical_report(db, start, end, routes, page_offset(cursor), limit, export)
    db.add(
        AuditLog(
            user_id=user.id,
            action="TECHNICAL_REPORT_EXPORTED" if export else "TECHNICAL_REPORT_VIEWED",
            entity_type="system",
            payload={"period": value["period"], "failures": value["failures"]["total"]},
        )
    )
    await db.commit()
    headers = {"Cache-Control": "private, no-store"}
    if export:
        headers["Content-Disposition"] = 'attachment; filename="technical-report.json"'
    return JSONResponse(value, headers=headers)
