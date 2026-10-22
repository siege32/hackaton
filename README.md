# SXSVN Intelligence

Сквозной прототип мониторинга новостей для компаний. `data_pipeline` собирает и
очищает события, строит профили и платёжные показатели; `ai_engine` извлекает
проверяемые факты, оценивает влияние и связывает выводы с источниками; `api`
объединяет модули и отдаёт веб-интерфейс.

## Запуск

Нужен Python 3.11+. Установка для API, UI и тестов:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Откройте `.env` и задайте `LLM_API_KEY` для извлечения фактов и анализа через
разрешённую LLM. По умолчанию `LLM_TIMEOUT=180`, `LLM_MAX_TOKENS=8000`.
Таймаут не повторяется автоматически: повтор длинного запроса расходовал время
и бюджет, не повышая шанс успеха.

CSV кейса хранится отдельно и не коммитится. Если он предоставлен, положите
файлы в `data/raw/` и соберите базу компаний:

```powershell
python -m data_pipeline build
```

Загрузите ленты новостей (требуется extra `news`, уже включённый в `dev`):

```powershell
python -m data_pipeline news-fetch --max-items 30
```

Запустите приложение:

```powershell
python -m uvicorn api.main:app --reload
```

Веб-интерфейс: `http://127.0.0.1:8000/`. Схема и интерактивная проверка API:
`http://127.0.0.1:8000/docs`.

Без базы компаний профиль и расчёты не доступны; API вернёт `503` с командой
сборки, не создавая тестовые профили. Банковские OCR-страницы уже хранятся в
`data/bank_reports/ocr/`; отдельный LLM-вызов для поиска по ним не нужен.

## Поток

1. Выберите компанию и событие в веб-интерфейсе.
2. API запускает фоновую задачу: `process_news` извлекает факты и сохраняет
   их; затем `analyze_company_impact` строит разбор с доказательствами.
3. UI опрашивает job status и показывает `queued`, `running`, `partial`,
   `completed` или `failed`; детали результата включают ограничения и источники.
4. Пользователь может добавить новость, обновить RSS и отправить уточнение.

Долгие запросы возвращают `job_id`; проверяйте результат через
`GET /api/v1/jobs/{job_id}`. Job status сохраняется в SQLite. Redis-очередь
движка не дублируется; worker продолжает использовать существующий протокол.

## API

- `GET /api/v1/health` — готовность данных, каталога, OCR-индекса и LLM.
- `GET /api/v1/companies` и `GET /api/v1/companies/{id}` — выбор и профиль.
- `GET /api/v1/companies/{id}/relationships` и `/exposure` — связи и расчёты.
- `GET /api/v1/news`, `/search`, `/{id}` — лента, поиск и первоисточники.
- `POST /api/v1/news` и `/news/fetch` — ручное добавление и загрузка RSS.
- `POST /api/v1/analysis` — фоновый анализ; уточнения создаются через
  `/api/v1/analysis/{analysis_id}/refinements`.
- `GET /api/v1/reports` и `/reports/search` — каталог и цитаты аналитики банка.

Полное описание находится в [api/README.md](api/README.md), контракты модулей —
в [docs/INTEGRATION.md](docs/INTEGRATION.md) и
[docs/DATA_PIPELINE.md](docs/DATA_PIPELINE.md).

## Тесты

```powershell
python -m unittest discover -s tests -t .
```

Тесты используют временные SQLite базы и фейковые ответы LLM; платные сетевые
вызовы не выполняются.
