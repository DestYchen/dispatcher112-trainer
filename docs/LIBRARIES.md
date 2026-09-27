# Зависимости и лицензии

Перечень относится к установленной и проверенной поставке. Включены прямые и транзитивные зависимости, инструменты разработки, optional-пакеты из lock-файла и системные пакеты образов. Полные таблицы вынесены в приложения, а исходные notices сохранены рядом: просмотр не требует сети. Обновление инвентаря — `python scripts/build_libraries.py` при работающих контейнерах.

Этап 13: SIP.js 0.21.2 — MIT (в составе [NPM.md](licenses/NPM.md)); Asterisk 22.9.0 — GPL-2.0-only WITH OpenSSL-Exception. eSpeak NG, SoX и все 83 системных пакета голосового сервера перечислены в [SYSTEM-telephony.md](licenses/SYSTEM-telephony.md). Контроллер SIP использует существующие Python-зависимости backend. Инвентарь npm обновлён до 325 записей.

## Прикладные пакеты

Этап 15: Python-инвентарь нового локального backend-образа содержит 77 пакетов, включая cryptography 50.0.1 (Apache-2.0 OR BSD-3-Clause) для Ed25519-подписей обновлений. METADATA и полные notices получены из образа без сети командой `python scripts/build_libraries.py --python-only --python-image dispatcher112-backend`; рабочие сервисы для этого не перезапускались.

Этап 14: Python-инвентарь — 76 записей, включая scikit-learn 1.9.1, NumPy 2.4.6, SciPy 1.17.1 и threadpoolctl 3.7.0. Ollama 0.34.4 — [MIT](licenses/Ollama-LICENSE.txt), 123 системных пакета — [SYSTEM-ollama.md](licenses/SYSTEM-ollama.md). Qwen2.5:3b распространяется по [Qwen Research License](licenses/Qwen2.5-3B-LICENSE.txt); её условия отличаются от лицензии сервера Ollama. Веса и manifest подготавливаются `make models` до отключения сети. Ограничения модели и методика проверки — [LEARNING-AI.md](LEARNING-AI.md).

| Контур | Полный перечень с версиями и лицензиями | Первичные сведения |
| --- | --- | --- |
| Backend, worker, Python-проверки | [PYTHON.md](licenses/PYTHON.md) | [METADATA и notices](licenses/python.json), `backend/requirements.lock` |
| React, Vite, инструменты и тесты | [NPM.md](licenses/NPM.md), 325 записей | [npm lock metadata](licenses/npm.json), `frontend/package-lock.json` |
| LanguageTool 6.6 и библиотеки Java | [JAVA.md](licenses/JAVA.md), 139 JAR | [POM/вложенные notices](licenses/java.json), [лицензии дистрибутива](licenses/languagetool-notices.json) |

`LanguageTool third-party-licenses/README.txt` сохранён [без изменений](licenses/LANGUAGETOOL-PUBLISHER.txt). Указанные в нём версии местами старее фактических JAR, поэтому для версии приоритет имеют POM, MANIFEST и хеш конкретного файла. Лицензии словарей могут отличаться от LGPL основного LanguageTool; они перечислены отдельно в Java-инвентаре.

## Системные компоненты

| Компонент | Лицензия / источник |
| --- | --- |
| Python 3.11 | PSF-2.0; также лицензии встроенных компонентов в образе |
| Node.js 22 | MIT; отдельные notices встроенных компонентов |
| PostgreSQL 15, pg_dump/pg_restore | PostgreSQL License; системные copyright в образе |
| Redis server 7.4.11 | RSALv2 OR SSPLv1, [лицензия именно этой версии](https://raw.githubusercontent.com/redis/redis/7.4.11/LICENSE.txt); это отдельный компонент от Python-пакета redis |
| Nginx 1.28 | BSD-2-Clause; пакеты и зависимости в APK-инвентаре |
| Temurin/OpenJDK 21 | GPL-2.0 с Classpath Exception; дополнительные notices в `/opt/java/openjdk/legal` |
| OCRmyPDF, Tesseract, Ghostscript, Poppler | соответственно MPL-2.0, Apache-2.0, AGPL-3.0, GPL-2.0-or-later; точные версии и полные условия — в backend OS notices |
| Pango, HarfBuzz, FreeType | LGPL/MIT/FTL/GPL по конкретному компоненту; точные условия в backend OS notices |

Полный список системных пакетов: [backend/worker](licenses/SYSTEM-backend.md), [frontend](licenses/SYSTEM-frontend.md), [PostgreSQL](licenses/SYSTEM-postgres.md), [Redis](licenses/SYSTEM-redis.md), [LanguageTool](licenses/SYSTEM-languagetool.md), [backup](licenses/SYSTEM-backup.md), [gateway](licenses/SYSTEM-gateway.md). Машиночитаемый свод — [system.json](licenses/system.json).

Для пакетов Debian/Ubuntu приложены исходные `/usr/share/doc/<package>/copyright` и `/usr/share/common-licenses`: [backend](licenses/backend-os-notices.tar.gz), [frontend](licenses/frontend-os-notices.tar.gz), [postgres](licenses/postgres-os-notices.tar.gz), [redis](licenses/redis-os-notices.tar.gz), [LanguageTool](licenses/languagetool-os-notices.tar.gz), [backup](licenses/backup-os-notices.tar.gz). Если copyright не имеет поля DEP-5 `License:`, таблица отсылает к полному тексту в архиве, а не приписывает пакету выдуманную SPDX-лицензию. Поле лицензии Alpine берётся из базы APK.

## Шрифты, звук и средства приёмки

PT Sans/PT Mono — SIL Open Font License 1.1; оригинальные тексты лежат в `frontend/public/fonts/pt-sans-LICENSE.txt` и `pt-mono-LICENSE.txt`. PDF-шрифты собраны из тех же локальных файлов: [происхождение](../data/fonts/README.md). Локальные сигналы интерфейса созданы скриптом проекта.

Голоса — Silero v5 CIS base nostress, MIT; [модель, speakers, хеш и лицензия](../data/voices/README.md). PyTorch нужен только для повторной сборки WAV, в runtime его нет. PyTorch 2.9.0 CPU — BSD-style, [лицензия](https://github.com/pytorch/pytorch/blob/v2.9.0/LICENSE); остальные зависимости инструментов сборки/приёмки перечислены в [TOOLS.md](licenses/TOOLS.md).

Playwright 1.55.0 — Apache-2.0; HTTPX 0.28.1 и websockets 15.0.1 — BSD-3-Clause. Chromium для приёмки поставляет Playwright; его notices доступны на локальной странице `chrome://credits` установленного браузера. Docker Engine/Compose и GNU Make — внешние инструменты запуска, не включённые библиотеки приложения; условия Docker Desktop относятся к установленному на рабочей станции продукту.

При передаче изолированной поставки сохраняются `docs/licenses`, лицензии шрифтов и голоса, а также notices внутри экспортированных Docker-образов. Изменение образа или lock-файла требует повторного формирования инвентаря.
## Инструменты формирования комплекта документов

DOCX/PPTX создаются локально библиотеками python-docx и python-pptx; PDF — существующим WeasyPrint в контейнере. Зафиксированные версии и исходные тексты лицензий инструментов сборки: [DELIVERY-TOOLS.json](licenses/DELIVERY-TOOLS.json), [notices](licenses/delivery-tools). Эти зависимости не добавлены в runtime интерфейса. Прямые версии закреплены в scripts/requirements-delivery.txt; wheel-файлы комплекта имеют SHA-256 в manifest.json.

MP3-экспорт использует локальный LAME/libmp3lame. Версии и notices включены в [SYSTEM-backend.md](licenses/SYSTEM-backend.md); инвентарь backend содержит 257 системных пакетов.
