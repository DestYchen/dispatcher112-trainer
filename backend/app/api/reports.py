import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response

from app.api.deps import DB, Teacher
from app.api.errors import APIError
from app.api.teacher import own_lesson
from app.db.models import AuditLog
from app.domain.exports import attendance_pdf, report_csv, report_xlsx, report_xml
from app.domain.report_pdf import render_report
from app.domain.reports import lesson_report

router = APIRouter(prefix="/teacher", tags=["reports"])
pdf_slots = asyncio.Semaphore(2)


@router.get("/lessons/{lesson_id}/report")
async def report(lesson_id: UUID, db: DB, user: Teacher) -> dict[str, Any]:
    lesson = await own_lesson(db, lesson_id, user, lock=False)
    return await lesson_report(db, lesson)


@router.get("/lessons/{lesson_id}/report.pdf")
async def report_pdf(lesson_id: UUID, db: DB, user: Teacher) -> Response:
    lesson = await own_lesson(db, lesson_id, user, lock=False)
    data = await lesson_report(db, lesson)
    async with pdf_slots:
        pdf = await asyncio.to_thread(render_report, data)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="lesson-{lesson_id}.pdf"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/lessons/{lesson_id}/report.csv")
async def csv_report(lesson_id: UUID, db: DB, user: Teacher) -> Response:
    lesson = await own_lesson(db, lesson_id, user, lock=False)
    content = report_csv(await lesson_report(db, lesson))
    db.add(
        AuditLog(
            user_id=user.id,
            action="LESSON_REPORT_EXPORTED",
            entity_type="lesson",
            entity_id=lesson_id,
            payload={"format": "CSV"},
        )
    )
    await db.commit()
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="lesson-{lesson_id}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/lessons/{lesson_id}/report.xlsx")
async def xlsx_report(lesson_id: UUID, db: DB, user: Teacher) -> Response:
    lesson = await own_lesson(db, lesson_id, user, lock=False)
    content = await asyncio.to_thread(report_xlsx, await lesson_report(db, lesson))
    db.add(
        AuditLog(
            user_id=user.id,
            action="LESSON_REPORT_EXPORTED",
            entity_type="lesson",
            entity_id=lesson_id,
            payload={"format": "XLSX"},
        )
    )
    await db.commit()
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="lesson-{lesson_id}.xlsx"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/lessons/{lesson_id}/report.xml")
async def xml_report(lesson_id: UUID, db: DB, user: Teacher) -> Response:
    lesson = await own_lesson(db, lesson_id, user, lock=False)
    content = report_xml(await lesson_report(db, lesson))
    db.add(
        AuditLog(
            user_id=user.id,
            action="LESSON_REPORT_EXPORTED",
            entity_type="lesson",
            entity_id=lesson_id,
            payload={"format": "XML"},
        )
    )
    await db.commit()
    return Response(
        content,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="lesson-{lesson_id}.xml"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/lessons/{lesson_id}/students/{student_id}/certificate.pdf")
async def certificate(lesson_id: UUID, student_id: UUID, db: DB, user: Teacher) -> Response:
    lesson = await own_lesson(db, lesson_id, user, lock=False)
    if lesson.status != "FINISHED":
        raise APIError(409, "INVALID_TRANSITION", "Сначала завершите занятие.")
    data = await lesson_report(db, lesson)
    student = next((row for row in data["students"] if row["student_id"] == str(student_id)), None)
    if student is None:
        raise APIError(404, "NOT_FOUND", "Участник не найден.")
    async with pdf_slots:
        content = await asyncio.to_thread(attendance_pdf, data, student)
    db.add(
        AuditLog(
            user_id=user.id,
            action="TRAINING_CERTIFICATE_EXPORTED",
            entity_type="lesson",
            entity_id=lesson_id,
            payload={"student_id": str(student_id)},
        )
    )
    await db.commit()
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="certificate-{student_id}.pdf"',
            "Cache-Control": "no-store",
        },
    )
