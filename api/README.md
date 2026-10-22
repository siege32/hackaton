# API и веб-интерфейс

Из корня репозитория:

```powershell
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m uvicorn api.main:app --reload
```

Веб-интерфейс доступен на `http://127.0.0.1:8000/`, Swagger UI — на
`http://127.0.0.1:8000/docs`.

## Источники данных и конфигурация

При запуске API читает `.env` и подключает существующие модули:

- база компаний `DATA_DB_PATH` (по умолчанию `data/processed/companies.sqlite`, read-only);
- каталог новостей `NEWS_DB_PATH` (по умолчанию `data/processed/news.sqlite`);
- SQLite с результатами анализа `INTEL_SQLITE_PATH` (по умолчанию `./intel.db`);
- OCR-фрагменты `BANK_REPORTS_OCR_DIR` (по умолчанию `data/bank_reports/ocr`);
- LLM — переменные `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_TIMEOUT`, `LLM_MAX_TOKENS`.

API не строит базу компаний автоматически: положите исходные CSV в `data/raw/`
и выполните `python -m data_pipeline build`. Каталог новостей создаётся
идемпотентно при первом обращении. Чтение профилей, связей и расчётов идёт через
`SqliteCompanyRepository`, `PaymentRelationshipRepository` и
`PaymentCalculationRepository`; индекс банка — через `BankReportsIndex`.

## Маршруты

| Метод и путь | Назначение |
|---|---|
| `GET /api/v1/health` | Готовность базы компаний, новостей, OCR и LLM |
| `GET /api/v1/data/report` | Отчёт качества собранной базы |
| `GET /api/v1/companies` | Выбор компании, поиск, pagination |
| `GET /api/v1/companies/{id}` | Профиль и `missing_data` |
| `GET /api/v1/companies/{id}/relationships` | Связи из выгрузки; платежи остаются неподтверждёнными наблюдениями |
| `GET /api/v1/companies/{id}/exposure` | Секторальная структура; `okved_filter` добавляет расчёт затронутых отраслей |
| `GET /api/v1/companies/{id}/calculations` | Базовые платёжные расчёты |
| `GET /api/v1/news` | Лента с фильтром `prefilter`, источником и дублями |
| `GET /api/v1/news/search?query=...` | BM25-поиск по каталогу новостей |
| `GET /api/v1/news/{id}` | Новость и все публикации кластера |
| `POST /api/v1/news` | Очистка и сохранение ручной новости, дедупликация |
| `POST /api/v1/news/fetch` | Загрузка включённых RSS (опционально `source_ids`, `max_items`) |
| `GET /api/v1/reports` | Каталог 9 банковских отчётов |
| `GET /api/v1/reports/search?query=...` | Цитируемые фрагменты OCR с датой, страницей и confidence |
| `POST /api/v1/analysis` | Фоновая задача анализа выбранной компании и новостей |
| `GET /api/v1/jobs/{job_id}` | Этап, прогресс, статус и готовый результат |
| `POST /api/v1/analysis/{analysis_id}/refinements` | Фоновое применение пользовательского уточнения |
| `GET /api/v1/analysis/{analysis_id}` | Сохранённый результат анализа |
| `GET /api/v1/processing/{news_id}` | Сохранённые факты и общий результат обработки |

Пример запуска анализа:

```json
{
  "company_id": "U_26030",
  "event_ids": ["news_0123456789abcdef"]
}
```

Ответ содержит `job_id` и `status_url`. Job сначала запускает `process_news`,
проверяет сохранённый результат извлечения фактов, затем вызывает
`analyze_company_impact`. Поэтому разбор нельзя начать без фактов. Статусы:
`queued`, `running`, `partial`, `completed`, `failed`. Ошибки и недостающие
основания не маскируются успешным результатом. До создания job API проверяет,
что `company_id` и каждый `event_id` существуют; неизвестный идентификатор
возвращает `404` и не запускает обработку или платный LLM-вызов. Уточнение
также проверяет наличие сохранённого `analysis_id` до создания job.

Job status хранится в `API_JOBS_DB_PATH` или в `INTEL_SQLITE_PATH`.
Фоновые задачи выполняются процессом FastAPI: для локальной демонстрации это
достаточно, но для production при перезапуске процесса незавершённые задачи
нужно восстанавливать или переносить в очередь worker’а. Redis-очередь движка
не дублируется.

## Ограничения безопасности

- Endpoint RSS запускает загрузку только фиксированного allowlist из
  `data_pipeline.news.sources`; произвольные URL не загружаются этим маршрутом.
- База компаний открывается в read-only режиме. Пересборка остаётся CLI-задачей.
- Платёжная экспозиция — доля известных платежей, не доля выручки и не
  вероятность убытка; неизвестные отрасли отражаются верхней границей и coverage.
- Все пользовательские утверждения показываются как уточнения, а не внешние
  факты. Выводы сохраняют цитаты, ограничения и недостающие данные.
