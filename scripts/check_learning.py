"""Actual teacher/student learning workflow, local model and responsive screens."""

import io
import json
import time
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from check_responsive import OUTPUT, api, layouts, login
from browser_environment import BASE_URL, browser_args


def main():
    fixture = json.loads(
        Path("data/learning-acceptance.json").read_text(encoding="utf-8")
    )
    participant = fixture["participants"][0]
    suffix = str(int(time.time()))
    group_title = "Группа адресной подготовки " + suffix
    material_title = "Памятка по адресу " + suffix
    module_title = "Проверка адресов " + suffix
    result = {"passed": False, "layouts": [], "errors": []}
    document = io.BytesIO()
    with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Учебная проверка сохранения документа</w:t></w:r></w:p></w:body></w:document>',
        )
    contents = document.getvalue()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=browser_args())
        teacher = browser.new_context(
            viewport={"width": 1440, "height": 900}
        ).new_page()
        student = browser.new_context(
            viewport={"width": 1440, "height": 900}
        ).new_page()
        for page in (teacher, student):
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.set_default_timeout(15000)
        try:
            login(teacher, fixture["teacher"], "teacher")
            teacher.get_by_role("link", name="Обучение и история", exact=True).click()
            teacher.get_by_role("button", name="Группы", exact=True).click()
            teacher.get_by_label("Название", exact=True).fill(group_title)
            teacher.get_by_role(
                "checkbox", name=participant["name"], exact=True
            ).check()
            teacher.get_by_role("button", name="Создать группу", exact=True).click()
            expect(
                teacher.get_by_role("heading", name=group_title, exact=True)
            ).to_be_visible()
            layouts(teacher, "learning-groups", result["layouts"])

            teacher.get_by_role("button", name="Учебные материалы", exact=True).click()
            teacher.locator("summary").filter(has_text="Добавить материал").click()
            form = teacher.locator("form").filter(
                has=teacher.get_by_role("button", name="Добавить материал", exact=True)
            )
            form.get_by_label("Название", exact=True).fill(material_title)
            form.get_by_label("Текст инструкции", exact=True).fill(
                "Сравните названия Дубнинская и Дубининская. Уточните улицу, дом и ориентир перед передачей карточки."
            )
            form.locator('input[type="file"]').set_input_files(
                {
                    "name": "проверка.docx",
                    "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "buffer": contents,
                }
            )
            with teacher.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/teacher/materials")
                )
            ) as uploaded:
                form.get_by_role("button", name="Добавить материал", exact=True).click()
            assert uploaded.value.ok
            material = uploaded.value.json()
            download = teacher.request.get(BASE_URL + material["download_url"])
            assert download.status == 200 and download.body() == contents
            result["document_roundtrip"] = True
            expect(
                teacher.get_by_role("heading", name=material_title, exact=True)
            ).to_be_visible()
            teacher.locator("summary").filter(has_text="Создать модуль").click()
            module_form = teacher.locator("form").filter(
                has=teacher.get_by_role("button", name="Создать модуль", exact=True)
            )
            module_form.get_by_label("Название", exact=True).fill(module_title)
            module_form.get_by_label("Задача и порядок изучения", exact=True).fill(
                "Прочитайте памятку, затем отработайте сценарий на занятии."
            )
            module_form.get_by_role("checkbox", name=material_title, exact=True).check()
            module_form.locator(f'input[value="{fixture["scenario_id"]}"]').check()
            module_form.get_by_role("button", name="Создать модуль", exact=True).click()
            module = teacher.locator("article").filter(
                has=teacher.get_by_role("heading", name=module_title, exact=True)
            )
            module.get_by_label("Выберите группу", exact=True).select_option(
                label=group_title
            )
            module.get_by_role("button", name="Назначить группе", exact=True).click()
            expect(module.get_by_text(group_title, exact=True).first).to_be_visible()
            layouts(teacher, "learning-library", result["layouts"])

            login(student, participant["login"], "student")
            student.get_by_role("link", name="Обучение и история", exact=True).click()
            expect(
                student.get_by_role("heading", name=material_title, exact=True)
            ).to_be_visible()
            layouts(student, "learning-student-module", result["layouts"])
            student_module = student.locator("article").filter(
                has=student.get_by_role("heading", name=module_title, exact=False)
            )
            expect(
                student_module.locator(f'a[href="{material["download_url"]}"]')
            ).to_be_visible()
            download = student.request.get(BASE_URL + material["download_url"])
            assert download.status == 200 and download.body() == contents
            student_module.get_by_role(
                "button", name="Материалы изучены", exact=True
            ).click()
            expect(
                student_module.get_by_text("✓ Изучено:", exact=False)
            ).to_be_visible()
            student.reload()
            expect(
                student_module.get_by_text("✓ Изучено:", exact=False)
            ).to_be_visible()

            teacher.get_by_role(
                "button", name="История и обратная связь", exact=True
            ).click()
            teacher.get_by_label("Выберите обучающегося", exact=True).select_option(
                participant["id"]
            )
            expect(
                teacher.get_by_text("Исходный балл:", exact=False).first
            ).to_be_visible()
            comment = (
                "Адрес сверён верно. Повторите порядок передачи карточки профильной службе. Проверка "
                + suffix
            )
            teacher.get_by_label("Обратная связь преподавателя", exact=True).first.fill(
                comment
            )
            teacher.get_by_role(
                "button", name="Добавить комментарий", exact=True
            ).first.click()
            expect(teacher.get_by_text(comment, exact=True)).to_be_visible()
            layouts(teacher, "learning-teacher-history", result["layouts"])
            student.get_by_role(
                "button", name="История и обратная связь", exact=True
            ).click()
            expect(student.get_by_text(comment, exact=True)).to_be_visible()
            layouts(student, "learning-student-history", result["layouts"])

            teacher.get_by_role("button", name="ИИ и прогноз", exact=True).click()
            teacher.get_by_label(
                "Происхождение данных и ограничения выборки", exact=True
            ).fill(
                "Синтетические траектории: 20 вымышленных учащихся, 300 оценок. Проверка программы, не педагогическая валидация."
            )
            teacher.get_by_role(
                "button", name="Обучить на истории моих занятий", exact=True
            ).click()
            expect(
                teacher.get_by_text(
                    "Модель точнее простого сравнения", exact=True
                ).first
            ).to_be_visible(timeout=120000)
            teacher.get_by_label("Выберите обучающегося", exact=True).select_option(
                participant["id"]
            )
            with teacher.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/teacher/analytics/forecast")
                )
            ) as saved:
                teacher.get_by_role(
                    "button", name="Сохранить прогноз", exact=True
                ).click()
            assert saved.value.ok
            forecast = saved.value.json()
            expect(
                teacher.get_by_text(
                    "Ожидается новое задание этого вида и сложности.", exact=True
                ).first
            ).to_be_visible()
            layouts(teacher, "learning-teacher-ai", result["layouts"])
            student.get_by_role("button", name="ИИ и прогноз", exact=True).click()
            expect(
                student.get_by_text(
                    "Ожидается новое задание этого вида и сложности.", exact=True
                ).first
            ).to_be_visible()
            assert any(
                row["id"] == forecast["id"]
                for row in api(student, "/learning/forecasts")["items"]
            )
            layouts(student, "learning-student-ai", result["layouts"])
            models = api(teacher, "/teacher/analytics/models")["items"]
            result.update(
                passed=True,
                model_version=models[0]["version"],
                mae=models[0]["mae_overall"],
                baseline_mae=models[0]["baseline_mae_overall"],
                test_count=models[0]["test_count"],
                dataset_kind=models[0]["dataset_kind"],
            )
            assert not result["errors"], result["errors"]
            print(
                "PASS: groups, materials, module, feedback, real neural training/forecast, 49 layouts",
                flush=True,
            )
        finally:
            for page, name in ((teacher, "teacher"), (student, "student")):
                page.screenshot(path=str(OUTPUT / f"learning-{name}-final.png"))
            (OUTPUT / "learning-browser.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            browser.close()


if __name__ == "__main__":
    main()
