import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from weasyprint import HTML
from weasyprint.urls import URLFetcher, URLFetcherResponse


def local_font(url: str) -> URLFetcherResponse:
    path = Path(unquote(urlparse(url).path)).resolve()
    if (
        not url.startswith("file:///data/fonts/")
        or not path.is_relative_to("/data/fonts")
        or path.name not in {"PTSans-400.ttf", "PTSans-700.ttf"}
    ):
        raise ValueError("В PDF разрешены только локальные шрифты.")
    return URLFetcherResponse(url, path.read_bytes(), {"Content-Type": "font/ttf"}, 200)


class LocalFonts(URLFetcher):  # type: ignore[misc]
    def fetch(self, url: str, headers: dict[str, str] | None = None) -> URLFetcherResponse:
        return local_font(url)


def render_report(report: dict[str, Any]) -> bytes:
    tokens = Path("/ui/styles/tokens.css").read_text(encoding="utf-8")
    values = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", tokens))
    styles = Path(__file__).with_name("report.css").read_text(encoding="utf-8")
    styles = re.sub(r"var\((--[\w-]+)\)", lambda match: values[match[1]], styles)
    rows = []
    for student in report["students"]:
        deviation = student["time_deviation_pct"]
        cells = [
            student["short_name"],
            student["workstation"] or "—",
            f"{deviation:+.1f} %" if deviation is not None else "—",
            student["errors"],
            student["spelling_errors"],
            student["level"] or "—",
            f"{student['total']}*"
            if student["manually_corrected"]
            else student["total"]
            if student["total"] is not None
            else "—",
        ]
        time_class = "ok" if deviation is not None and deviation <= 0 else "alarm"
        rows.append(
            "<tr>"
            + "".join(
                f'<td class="{time_class if index == 2 else ""}">{escape(str(value))}</td>'
                for index, value in enumerate(cells)
            )
            + "</tr>"
        )
    maximum = max((row["count"] for row in report["reaction_distribution"]), default=1) or 1
    distribution = "".join(
        f"<tr><th>{escape(row['label'])}</th><td>"
        f'<div class="bar" style="width:{100 * row["count"] / maximum}%">'
        f"{row['count']}</div></td></tr>"
        for row in report["reaction_distribution"]
    )
    heat = report["heatmap"]
    heat_rows = "".join(
        "<tr><th>"
        + escape(row["incident_type_name"])
        + "</th>"
        + "".join(
            f'<td class="{"hot" if row["counts"].get(v["code"], 0) else "cold"}">'
            f"{row['counts'].get(v['code'], 0)}</td>"
            for v in heat["violations"]
        )
        + "</tr>"
        for row in heat["rows"]
    )
    headers = "".join(f"<th>{escape(c)}</th>" for c in report["columns"])
    heat_headers = "".join(f"<th>{escape(v['message'])}</th>" for v in heat["violations"])
    started = (
        datetime.fromisoformat(report["lesson"]["started_at"])
        .astimezone(ZoneInfo("Europe/Moscow"))
        .strftime("%d.%m.%Y %H:%M:%S")
        if report["lesson"]["started_at"]
        else "Занятие ещё не началось"
    )
    finished = (
        datetime.fromisoformat(report["lesson"]["finished_at"])
        .astimezone(ZoneInfo("Europe/Moscow"))
        .strftime("%d.%m.%Y %H:%M:%S")
        if report["lesson"]["finished_at"]
        else "Занятие продолжается"
    )
    entry_details = []
    for student in report["students"]:
        for card in student.get("cards", []):
            if card.get("task_mode") != "CARD_ENTRY":
                continue
            score = card.get("score") or {}
            timing = score.get("timings", {})
            elapsed = timing.get("processing_ms")
            limit = timing.get("processing_deadline_ms")
            seconds = (
                f"{elapsed / 1000:.1f} с / норматив {limit / 1000:.1f} с"
                if elapsed is not None and limit
                else "Не приступил к заполнению"
            )
            fields = []
            for field in score.get("entry_fields", []):
                field_values = []
                for side in ("actual", "expected"):
                    value = field.get(side + "_display", field.get(side))
                    field_values.append(
                        ", ".join(str(item) for item in value)
                        if isinstance(value, list)
                        else str(value or "—")
                    )
                fields.append(
                    "<tr>"
                    + "".join(
                        f"<td>{escape(value)}</td>"
                        for value in [
                            field["label"],
                            *field_values,
                            "Совпадает" if field["correct"] else "Отличается",
                        ]
                    )
                    + "</tr>"
                )
            entry_details.append(
                f"<h2>{escape(student['short_name'])} · {escape(card['card_number'])}</h2>"
                f"<p>Заполнение карточки: {seconds}</p><table><thead><tr><th>Поле</th>"
                "<th>Ответ</th><th>Эталон</th><th>Проверка</th></tr></thead>"
                f"<tbody>{''.join(fields)}</tbody></table>"
            )
    html = f"""<!doctype html><html lang="ru"><meta charset="utf-8"><style>{styles}</style>
    <title>Отчёт о практическом занятии</title><h1>Отчёт о практическом занятии</h1>
    <p>{escape(report["lesson"]["title"])}</p>
    <p>{started} — {finished}</p>
    <table class="results"><thead><tr>{headers}</tr></thead>
    <tbody>{"".join(rows)}</tbody></table>
    <p>* Ручная корректировка преподавателя. Время: отклонение средней реакции от норматива;
    без первичного статуса учитывается время до закрытия.</p>
    <h2>Распределение времени реакции</h2><table>{distribution}</table>
    <h2>Ошибки × типы происшествий</h2>
    <table><thead><tr><th>Тип происшествия</th>{heat_headers}</tr></thead>
    <tbody>{heat_rows}</tbody></table>
    {"<p>Нарушений не обнаружено.</p>" if not heat_rows else ""}
    {"".join(entry_details)}</html>"""
    result: bytes = HTML(string=html, url_fetcher=LocalFonts(fail_on_errors=True)).write_pdf()
    return result
