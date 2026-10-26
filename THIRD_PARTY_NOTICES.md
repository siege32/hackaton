# THIRD_PARTY_NOTICES

SXSVN Intelligence использует сторонние компоненты, перечисленные ниже. Версии
совпадают с `requirements.lock`. Лицензии взяты из метаданных пакетов; полные
тексты лицензий лежат в установленных пакетах (каталог `*.dist-info`) и на
страницах проектов.

## Важно: PyMuPDF (AGPL-3.0)
`pymupdf` (и устанавливаемые вместе с ним `pymupdf_layout`, `pymupdf4llm`)
распространяется по GNU AGPL-3.0 или по коммерческой лицензии Artifex.
В проекте он нужен **только** для разового распознавания PDF-аналитики банка
(`python -m data_pipeline ocr-reports`, extra `ocr`): страницы рендерятся в
картинки. Модуль импортируется лениво внутри `data_pipeline/reports/ocr.py`;
веб-сервис, API и анализ новостей его не загружают. Результат распознавания
уже лежит в `data/bank_reports/ocr/`. Для работы сервиса extra `ocr` не нужен:
`pip install ".[api,news,redis]"`. Исходный код проекта открыт в этом
репозитории.

## Python-зависимости
| Пакет | Версия | Лицензия | Проект |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | https://github.com/fastapi/annotated-doc |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anyio | 4.15.1 | MIT | https://github.com/agronholm/anyio |
| babel | 2.18.0 | BSD License | https://babel.pocoo.org/ |
| cachetools | 7.2.4 | MIT | https://github.com/tkem/cachetools/ |
| certifi | 2026.10.19 | Mozilla Public License 2.0 (MPL 2.0) | https://github.com/certifi/python-certifi |
| charset-normalizer | 3.5.2 | MIT | https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md |
| click | 8.5.0 | BSD-3-Clause | https://github.com/pallets/click/ |
| colorama | 0.4.6 | BSD License | https://github.com/tartley/colorama |
| courlan | 1.4.0 | Apache-2.0 | https://github.com/adbar/courlan |
| dateparser | 1.4.3 | BSD-3-Clause | https://github.com/scrapinghub/dateparser |
| fastapi | 0.162.0 | MIT | https://github.com/fastapi/fastapi |
| flatbuffers | 25.12.19 | Apache Software License | https://google.github.io/flatbuffers/ |
| h11 | 0.16.0 | MIT License | https://github.com/python-hyper/h11 |
| htmldate | 1.11.0 | Apache-2.0 | https://htmldate.readthedocs.io |
| httpcore | 1.0.9 | BSD-3-Clause | https://www.encode.io/httpcore/ |
| httpcore2 | 2.16.0 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| httptools | 0.9.0 | MIT | https://github.com/MagicStack/httptools |
| httpx | 0.28.1 | BSD License | https://github.com/encode/httpx |
| httpx2 | 2.16.0 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| iniconfig | 2.3.1 | MIT | https://github.com/pytest-dev/iniconfig |
| jiter | 0.17.0 | MIT | https://github.com/pydantic/jiter/ |
| jusText | 3.0.2 | BSD License | https://github.com/miso-belica/jusText |
| lxml | 6.1.3 | BSD-3-Clause | https://lxml.de/ |
| lxml_html_clean | 0.4.5 | BSD-3-Clause | https://github.com/fedora-python/lxml_html_clean/ |
| networkx | 3.6.1 | BSD-3-Clause | https://networkx.org/ |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://numpy.org |
| onnxruntime | 1.31.0 | MIT License | https://onnxruntime.ai |
| openai | 3.36.1 | Apache-2.0 | https://github.com/openai/openai-python |
| opentelemetry-api | 1.45.1 | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| pluggy | 1.7.0 | MIT License | https://github.com/pytest-dev/pluggy |
| protobuf | 7.36.2 | 3-Clause BSD License | https://developers.google.com/protocol-buffers/ |
| psutil | 7.2.2 | BSD-3-Clause | https://github.com/giampaolo/psutil |
| pydantic | 2.14.1 | MIT | https://github.com/pydantic/pydantic |
| pydantic_core | 2.50.1 | MIT | https://github.com/pydantic/pydantic |
| pydantic-settings | 2.16.1 | MIT | https://github.com/pydantic/pydantic-settings |
| Pygments | 2.21.0 | BSD-2-Clause | https://pygments.org |
| pymupdf | 2.0 | AGPL-3.0 или коммерческая лицензия Artifex | https://github.com/pymupdf/pymupdf |
| pymupdf_layout | 2.0 | AGPL-3.0 или коммерческая лицензия Artifex (как PyMuPDF; метаданные лицензии не заполнены) | https://pymupdf.readthedocs.io |
| pymupdf4llm | 2.0 | AGPL-3.0 или коммерческая лицензия Artifex (как PyMuPDF; метаданные лицензии не заполнены) | https://github.com/pymupdf/pymupdf4llm |
| pytest | 9.1.1 | MIT | https://docs.pytest.org/en/latest/ |
| python-dateutil | 3.0.0 | BSD License; Apache Software License | https://github.com/dateutil/dateutil |
| python-dotenv | 1.2.4 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| pytz | 2026.5 | MIT License | http://pythonhosted.org/pytz |
| PyYAML | 6.0.3 | MIT License | https://pyyaml.org/ |
| redis | 8.1.0 | MIT | https://github.com/redis/redis-py |
| regex | 2026.9.29 | Apache-2.0 AND CNRI-Python | https://github.com/mrabarnett/mrab-regex |
| six | 1.17.0 | MIT License | https://github.com/benjaminp/six |
| sniffio | 1.3.1 | MIT License; Apache Software License | https://github.com/python-trio/sniffio |
| starlette | 1.9.0 | BSD-3-Clause | https://github.com/Kludex/starlette |
| tabulate | 0.10.0 | MIT | https://github.com/astanin/python-tabulate |
| tld | 0.13.2 | MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later | https://github.com/barseghyanartur/tld/ |
| trafilatura | 2.3.1 | Apache-2.0 | https://trafilatura.readthedocs.io |
| truststore | 0.10.5 | MIT | https://github.com/sethmlarson/truststore |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| tzdata | 2026.5 | Apache-2.0 | https://github.com/python/tzdata |
| tzlocal | 5.4.4 | MIT | https://github.com/regebro/tzlocal |
| urllib3 | 2.8.0 | MIT | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst |
| uvicorn | 0.54.0 | BSD-3-Clause | https://uvicorn.dev/ |
| watchfiles | 1.3.0 | MIT License | https://github.com/samuelcolvin/watchfiles |
| websockets | 17.3 | BSD-3-Clause | https://github.com/python-websockets/websockets |

## Прочие внешние компоненты и сервисы
| Компонент | Использование | Лицензия / условия |
|---|---|---|
| Шрифты Manrope, DM Mono (Google Fonts) | интерфейс, загружаются из fonts.googleapis.com | SIL Open Font License 1.1 |
| Docker-образы `python:3.12-slim`, `redis:7-alpine` | сборка и запуск в Docker | PSF License; Redis — RSALv2/SSPLv1/AGPLv3 (используется без изменений как отдельный сервис) |
| LLM через OpenAI-совместимый шлюз организаторов (DeepSeek, GLM, Qwen) | извлечение фактов, анализ, OCR | условия хакатона и провайдеров моделей |
| RSS-ленты СМИ (РБК, Интерфакс, ТАСС, Ведомости, Коммерсантъ, Лента.ру, Банки.ру) | заголовки, анонсы и тексты новостей для анализа | права принадлежат изданиям; в интерфейсе указан источник и ссылка на оригинал |
| Аналитические отчёты Альфа-Банка (PDF) | материалы кейса; распознанный текст в `data/bank_reports/ocr/` | права принадлежат АО «Альфа-Банк»; используются в рамках хакатона |
| Данные кейса (CSV) | профили, платежи, связи | предоставлены организаторами; в репозиторий не включены |
