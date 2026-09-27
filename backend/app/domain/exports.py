"""Local report exchange formats with canonical JSON keys and safe spreadsheet cells."""

import csv
import io
from html import escape
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from weasyprint import HTML

from app.domain.report_pdf import LocalFonts


def csv_cell(value: object) -> object:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return "" if value is None else value


def report_csv(report: dict[str, Any]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
    writer.writerow(report["columns"])
    for row in report["students"]:
        values = [
            row["short_name"],
            row["workstation"],
            row["time_deviation_pct"],
            row["errors"],
            row["spelling_errors"],
            row["level"],
            f"{row['total']}*" if row["manually_corrected"] else row["total"],
        ]
        writer.writerow(csv_cell(value) for value in values)
    return stream.getvalue().encode("utf-8-sig")


def report_xlsx(report: dict[str, Any]) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Результаты занятия")
    rows = [report["columns"]]
    for row in report["students"]:
        rows.append(
            [
                row["short_name"],
                row["workstation"],
                row["time_deviation_pct"],
                row["errors"],
                row["spelling_errors"],
                row["level"],
                f"{row['total']}*" if row["manually_corrected"] else row["total"],
            ]
        )
    for values in rows:
        cells = []
        for value in values:
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, str):
                cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def xml_value(parent: Element, value: Any) -> None:
    if isinstance(value, dict):
        for name, child in value.items():
            # All names originate from fixed application schemas, never user-entered XML tags.
            xml_value(SubElement(parent, name), child)
    elif isinstance(value, list):
        for item in value:
            xml_value(SubElement(parent, "item"), item)
    elif value is None:
        parent.set("nil", "true")
    else:
        parent.text = str(value).lower() if isinstance(value, bool) else str(value)


def report_xml(report: dict[str, Any]) -> bytes:
    root = Element("lesson_report", {"schema": "1"})
    xml_value(root, report)
    result = tostring(root, encoding="utf-8", xml_declaration=True)
    assert isinstance(result, bytes)
    return result


def attendance_pdf(report: dict[str, Any], student: dict[str, Any]) -> bytes:
    # The existing report stylesheet resolves every colour/size through the UI tokens.
    import re

    tokens = Path("/ui/styles/tokens.css").read_text(encoding="utf-8")
    values = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", tokens))
    styles = Path(__file__).with_name("report.css").read_text(encoding="utf-8")
    styles = re.sub(r"var\((--[\w-]+)\)", lambda match: values[match[1]], styles)
    lesson = report["lesson"]
    total = escape(str(student["total"] if student["total"] is not None else "не рассчитан"))
    correction = (
        "<p>Баллы скорректированы преподавателем.</p>" if student["manually_corrected"] else ""
    )
    date = lesson["finished_at"] or ""
    if date:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        date = (
            datetime.fromisoformat(date)
            .astimezone(ZoneInfo("Europe/Moscow"))
            .strftime("%d.%m.%Y %H:%M МСК")
        )
    body = f"""<!doctype html><html lang="ru"><meta charset="utf-8"><style>{styles}</style>
    <title>Подтверждение обучения</title><h1>Подтверждение участия в учебном занятии</h1>
    <p>АРМ-112 · Учебный режим</p><h2>{escape(student["short_name"])}</h2>
    <p>Занятие: {escape(lesson["title"])}</p>
    <p>Завершено: {escape(date)}</p>
    <p>Выдано карточек: {len(student["cards"])}.</p>
    <p>Итоговый балл: {total}.</p>{correction}
    <p>Документ подтверждает участие в симуляции и содержит сохранённый результат занятия.
    Профессиональная квалификация или допуск к работе этим документом не присваиваются.</p>
    <p>Занятие: {escape(lesson["id"])}. Участник: {escape(student["student_id"])}.</p></html>"""
    result = HTML(string=body, url_fetcher=LocalFonts()).write_pdf()
    assert isinstance(result, bytes)
    return result
