"""Create an explicitly synthetic 90-row PDF for real OCR regression testing."""
from pathlib import Path

from playwright.sync_api import sync_playwright

pages = []
for page in range(5):
    rows = []
    for index in range(page * 18 + 1, page * 18 + 19):
        rows.append(f'<tr><td>{index}</td><td>Пожар в частном доме. Иванов Иван Иванович, +7 999 123-45-67.</td><td>ул. Дубнинская, д. {index}</td></tr>')
    pages.append('<section><h1>Синтетическая фикстура OCR — не билеты заказчика</h1><table><thead><tr><th>№</th><th>Ситуация</th><th>Адрес</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></section>')
html = '''<html lang="ru"><meta charset="utf-8"><style>
@font-face {font-family:PT;src:url(http://localhost:5173/fonts/pt-sans-cyrillic-400-normal.woff2)}
body {font-family:PT,Arial;font-size:16px} h1{font-size:18px} table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px;border:1px solid #999;white-space:nowrap}section{break-after:page}
</style>''' + ''.join(pages) + '</html>'
target = Path('data/tickets/sample-ocr.pdf')
target.parent.mkdir(exist_ok=True)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content(html)
    page.evaluate('document.fonts.ready')
    page.pdf(path=str(target), format='A4', landscape=True, margin={"top":"10mm","bottom":"10mm","left":"10mm","right":"10mm"})
    browser.close()
print(target)
