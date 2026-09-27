import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Assignment, LessonParticipant, User, Workstation
from app.domain.report_pdf import local_font
from app.domain.reports import REPORT_COLUMNS, lesson_report, training_level
from tests.test_auth import sign_in
from tests.test_lessons import lesson_fixture


@pytest.mark.parametrize(
    ("score", "level"),
    [
        (None, None),
        (0, "начальный"),
        (59.99, "начальный"),
        (60, "базовый"),
        (84.99, "базовый"),
        (85, "уверенный"),
        (100, "уверенный"),
    ],
)
def test_training_level_boundaries(score: float | None, level: str | None) -> None:
    assert training_level(score) == level


async def test_report_counts_time_errors_override_empty_and_access(
    db: AsyncSession, client: AsyncClient
) -> None:
    lesson, rows = await lesson_fixture(db, 3)
    now = datetime.now(UTC)
    score = {
        "total": 80,
        "axes": {
            axis: {"score": 80, "weight": weight}
            for axis, weight in lesson.settings["weights"].items()
        },
        "violations": [
            {"code": "SPELLING", "message": "Орфографическая ошибка"},
            {"code": "ADDRESS_TYPO", "message": "Ошибка в адресе"},
        ],
    }
    for row in rows[:2]:
        row.delivered_at, row.primary_status_at = now, now + timedelta(seconds=24)
        row.closed_at, row.state, row.score = now + timedelta(seconds=60), "CLOSED", deepcopy(score)
    rows[0].teacher_override = {"total": 100, "comment": "Учтён доклад"}
    await db.flush()
    before = deepcopy(rows[0].score)
    result = await lesson_report(db, lesson)
    student = result["students"][0]
    assert result["columns"] == ["Фамилия", "АРМ", "Время", "Ошибки", "Орфогр.", "Уровень", "Балл"]
    assert student["total"] == 90 and student["level"] == "уверенный"
    assert (
        student["time_deviation_pct"] == -20
        and student["errors"] == student["spelling_errors"] == 2
    )
    assert student["manually_corrected"] and len(student["cards"]) == 2
    assert sum(bin["count"] for bin in result["reaction_distribution"]) == 2
    assert result["heatmap"]["rows"][0]["counts"]["ADDRESS_TYPO"] == 2
    assert rows[0].score == before
    await sign_in(client)
    for suffix in ("report", "report.pdf"):
        assert (
            await client.get(f"/api/v1/teacher/lessons/{lesson.id}/{suffix}")
        ).status_code == 403
    client.cookies.clear()
    await sign_in(client, "teacher", "teacher")
    assert (await client.get(f"/api/v1/teacher/lessons/{uuid4()}/report")).status_code == 404
    assert (await client.get(f"/api/v1/teacher/lessons/{lesson.id}/report")).json()[
        "columns"
    ] == REPORT_COLUMNS
    for row in rows:
        row.delivered_at = None
    await db.flush()
    empty = await lesson_report(db, lesson)
    assert empty["students"][0]["total"] is None and empty["cards_total"] == 0


async def test_pdf_twenty_students_ten_cards_under_thirty_seconds(
    db: AsyncSession, client: AsyncClient
) -> None:
    lesson, rows = await lesson_fixture(db, 10)
    original = await db.get(User, rows[0].student_id)
    assert original
    now = datetime.now(UTC)
    score = {
        "total": 90,
        "axes": {
            axis: {"score": 90, "weight": weight}
            for axis, weight in lesson.settings["weights"].items()
        },
        "violations": [{"code": "SPELLING", "message": "Орфографическая ошибка"}],
    }
    for index in range(1, 20):
        student = User(
            login=f"report{index}",
            password_hash=original.password_hash,
            role="STUDENT",
            last_name=f"Проверяев-{index:02d}",
            first_name="Иван",
            service_id=original.service_id,
        )
        station = Workstation(number=f"ОТЧ-{index:02d}")
        db.add_all([student, station])
        await db.flush()
        db.add(
            LessonParticipant(lesson_id=lesson.id, student_id=student.id, workstation_id=station.id)
        )
        for number in range(10):
            row = Assignment(
                lesson_id=lesson.id,
                student_id=student.id,
                scenario_id=rows[0].scenario_id,
                card_number=f"R-{index}-{number}",
            )
            db.add(row)
            rows.append(row)
    for row in rows:
        row.delivered_at, row.primary_status_at = now, now + timedelta(seconds=24)
        row.closed_at, row.state, row.score = now + timedelta(seconds=60), "CLOSED", deepcopy(score)
    lesson.finished_at, lesson.status = now + timedelta(seconds=100), "FINISHED"
    await db.flush()
    await sign_in(client, "teacher", "teacher")
    start = perf_counter()
    response = await client.get(f"/api/v1/teacher/lessons/{lesson.id}/report.pdf")
    duration = perf_counter() - start
    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    assert duration < 30
    pdf = PdfReader(BytesIO(response.content))
    text = "\n".join(page.extract_text() for page in pdf.pages)
    assert "Отчёт о практическом занятии" in text and "Проверяев-19" in text
    assert all(column in text for column in REPORT_COLUMNS)
    assert "�" not in text and 1 <= len(pdf.pages) <= 4
    report = await lesson_report(db, lesson)
    assert len(report["students"]) == 20 and report["cards_total"] == 200
    await asyncio.to_thread(Path("/data/report-acceptance.pdf").write_bytes, response.content)
    print(f"PDF 20 × 10: {duration:.3f} s; {len(pdf.pages)} pages")


@pytest.mark.parametrize(
    "url", ["https://example.com", "file:///etc/passwd", "file:///ui/fonts/../secret"]
)
def test_pdf_never_fetches_network_or_arbitrary_files(url: str) -> None:
    with pytest.raises(ValueError):
        local_font(url)
