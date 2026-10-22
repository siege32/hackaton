# SXSVN AI news-intelligence (`ai_engine`)

Модуль аналитики новостей для SXSVN: извлекает проверяемые факты из новости,
оценивает её значимость для конкретной компании и строит обоснованный прогноз
влияния. Не создаёт вторую очередь, БД, API или UI — работает поверх уже
существующих Redis-очереди, таблиц новостей/компаний и подготовленной
банковской аналитики.

## Что реализовано

- **Стадия A — факты, независимые от компании.** LLM извлекает факты со
  ссылкой на дословную цитату; валидатор проверяет цитату, числа, даты,
  дедуплицирует факты и сохраняет версию контента.
- **Стадия B — оценка для компании.** Отдельная сущность
  `CompanyNewsAssessment`: `KEEP` / `DROP` / `REVIEW`, направление, тип,
  механизм, горизонт, уверенность, доказательства, ограничения, недостающие
  данные, версии модели/промпта.
- **Стадия C — insight.** Детерминированная цепочка «событие → связь с
  компанией → механизм → последствие» с явным разделением факта, вывода и
  гипотезы. Дополнительные поиски жёстко ограничены **двумя** вызовами на
  анализ (в коде, не в промпте).
- **Уточнения пользователя.** Принимаются как `user_statement`
  (`confirmed=False`). При доступной LLM зависимые выводы пересматриваются
  через adaptation prompt; без LLM уточнение сохраняется как гипотеза и не
  выдаётся за внешний факт.
- **Очередь.** Reserve → process → persist → ack. Ack выполняется только после
  успешного сохранения; есть retry, delayed retry, visibility timeout,
  восстановление зависших задач и dead-letter.
- **Идемпотентность.** Повторная обработка не выполняется, если совпадают
  `news_id`, `content_version`, `profile_version`, `processing_version`,
  `model_id` и версия промпта.
- **Тесты без платных вызовов.** Тесты на stdlib `unittest` с фейковым LLM.

## Структура

```
ai_engine/
  config.py            настройки из env, список разрешённых моделей
  models.py            доменные модели и enum'ы
  service.py           публичный фасад NewsIntelligenceService
  worker.py            consumer существующей Redis-очереди
  __main__.py          CLI: check-config / init-db / worker
  facts/               извлечение и валидация фактов
  classification/      контекст компании, правила, LLM-классификация
  insight/             движок анализа, инструменты, бюджет поисков
  storage/             протоколы, in-memory и SQL-адаптеры, schema.sql
  queue/               Redis reliable queue и in-memory fake
  llm/                 OpenAI-совместимый клиент, промпты, JSON-парсинг
tests/                 61 тест, фейковый LLM, без сети
docs/INTEGRATION.md    контракты для интеграции
```

## Быстрый старт

Требуется Python 3.11+. OpenAI SDK используется для LLM, а `redis` — для
реальной очереди.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
# для обработки через LLM и реальной Redis-очереди:
pip install -e ".[llm,redis]"
# только для LLM или только для Redis:
pip install -e ".[llm]"
pip install -e ".[redis]"
Copy-Item .env.example .env   # заполнить LLM_API_KEY и параметры БД
```

Настройки читаются из окружения процесса и локального `.env`. Значения из
процесса имеют приоритет; `.env` не перезаписывает уже заданные переменные.

### Проверка конфигурации и схемы

```powershell
python -m ai_engine check-config
python -m ai_engine init-db --path .\intel.db
```

`check-config` печатает только безопасные поля; API-ключ не выводится.

### Запуск worker

```powershell
python -m ai_engine worker
# обработать не более 10 задач и выйти:
python -m ai_engine worker --max-jobs 10
```

Worker использует **существующие** ключи Redis (`NEWS_QUEUE_KEY` и др.).
Продюсер кладёт JSON-сообщение в очередь; формат описан в
[docs/INTEGRATION.md](docs/INTEGRATION.md).

### Программный API

```python
from ai_engine import Settings, build_service

settings = Settings.from_env()
service = build_service(settings)          # нужен INTEL_SQLITE_PATH

processing = service.process_news("news-123")
print(processing.status, processing.general_relevance)

result = service.classify_and_extract("news-123", company_id="company-456")
assessment = result.assessment
print(assessment.relevance_class, assessment.impact_direction)
print(assessment.impact_mechanism, assessment.limitations)

insight = service.analyze_company_impact("company-456", ["news-123"])
print(insight.analysis_status, insight.additional_searches_used)

adapted = service.adapt_company_analysis(
    insight.analysis_id,
    text="Компания не закупает этот ресурс; зависимость отсутствует.",
    kind="dependency",
    affected_event_ids=["news-123"],
)
print(adapted.parent_analysis_id, adapted.changed_event_ids)
```

Для PostgreSQL и реальных схем нужно передать адаптеры явно (см.
`docs/INTEGRATION.md`), потому что таблицы новостей и компаний принадлежат
другим командам.

## Ключевые контракты

### Технический статус ≠ бизнес-значимость

| Поле | Значения | Смысл |
|---|---|---|
| `NewsProcessingResult.status` | `pending`, `processing`, `processed`, `failed` | техническая обработка |
| `CompanyNewsAssessment.relevance_class` | `KEEP`, `DROP`, `REVIEW` | значимость для компании |

`failed` не означает `DROP`. Ошибка обработки сохраняется как `failed`
(с `REVIEW` по умолчанию), а не как отрицательный факт о компании.

### Оценка компании

Обязательные поля: `company_id`, `impact_direction` (`positive` / `negative` /
`mixed` / `unknown`), `impact_type` (`direct` / `indirect` / `unknown`),
`impact_mechanism`, `impact_horizon`, `confidence` (0…1), `evidence`,
`limitations`, `missing_data`, `content_version`, `profile_version`,
`model_id`, `prompt_version`, `processing_version`.

Правила:

- отсутствие данных о компании — это `missing_data`/`limitations`, а не
  негативный факт;
- совпадение отрасли или названия само по себе недостаточно для `KEEP`;
- `KEEP` требует подтверждённой связи с деятельностью/ресурсами/контрагентами
  компании;
- платёжная ссылка или единичный платёж **не** доказывают зависимость;
- неподтверждённая связь остаётся гипотезой (`confirmed=false`).

### Версионирование и идемпотентность

Версии хранятся в каждом результате: `content_version` (хеш новости),
`profile_version` (хеш профиля), `processing_version` (`PROCESSING_VERSION`),
`model_id`, `prompt_version` (`news-facts.v1`, `company-relevance.v1`,
  `company-insight.v1`, `company-adaptation.v3`). Изменение любой версии приводит к пересчёту.

### Insight и лимит поисков

`InsightEngine` использует только реальные источники: извлечённые факты,
профиль компании, подтверждённые связи, подготовленную банковскую аналитику,
существующие расчёты и уточнения пользователя. Бюджет `SearchBudget`
программно запрещает больше двух поисковых шагов на анализ; неудачный вызов
тоже расходует шаг. Если аналитика не подключена, результат получает
`limitations`/`missing_data`, а не выдуманные данные.

### Уточнения пользователя

`adapt_company_analysis` создаёт новую версию анализа (`parent_analysis_id`),
сохраняет уточнение с `source="user_statement"`, `confirmed_external=False`,
помечает затронутые события и добавляет ограничения. Это не «переписывание
LLM»: при доступной LLM создаётся новая версия анализа через adaptation
prompt, а ответы проверяются по сохранённым фактам. Уточнение остаётся
неподтверждённым пользовательским свидетельством.

## Redis-сообщение

Минимально:

```json
{"job_type": "news.process", "news_id": "news-123"}
```

Типы: `news.process`, `company.assess` (нужен `company_id`),
`company.analyze` (нужен `company_id`; `payload.event_ids` опционально).
Сообщение без `news_id` или с неизвестным `job_type` уходит в dead-letter.
Ack — только после сохранения результата.

## Хранение

Модуль создаёт только аддитивные таблицы `intel_*`:

- `intel_news_processing`
- `intel_news_facts`
- `intel_company_assessments`
- `intel_company_assessment_history`
- `intel_insight_analyses`
- `intel_refinements`

Таблицы новостей/компаний/связей только читаются через адаптеры. Схема
совместима с SQLite и PostgreSQL; `schema.sql` можно применять повторно.

## Тесты

```powershell
python -m unittest discover -s tests -t .
```

Фактический результат на 2026-10-22 (Python 3.12): **Ran 61 tests, OK**.
Тесты не обращаются к платному LLM: используется фейковый клиент; Redis
проверяется на in-memory fake. `unittest` пишет итог в stderr, поэтому в
PowerShell вывод может выглядеть как ошибка — смотрите строку `OK` и код
возврата.

Покрыто: retry/ошибки LLM, обоснованность цитат и чисел, дедупликация фактов,
версии, порядок persist→ack, malformed/missing/duplicate/stale jobs, лимиты
retry, мультикомпанийная классификация, отказ LLM-валидации, бюджет поисков,
платёжные ссылки, частичные результаты, адаптация, SQLite/адаптеры.

## Ограничения и статус проверки

Реальный end-to-end прогон **не выполнен** и не может считаться
подтверждённым, пока не предоставлены:

1. `LLM_API_KEY` и сетевой доступ к `https://changellenge.up.railway.app/v1`
   (живой вызов провайдера не проверялся);
2. работающий Redis и установленный пакет `redis` (сейчас очередь проверена
   только на fake);
3. реальные таблицы новостей/компаний/связей или их колонночные маппинги;
4. подготовленный индекс банковской аналитики и расчёты (сейчас
   `UnavailableAnalyticsIndex` честно сообщает об отсутствии);
5. подтверждение выбора модели: `deepseek/deepseek-v4.1-flash` — предварительный
   выбор, сравнительных бенчмарков в этом модуле не проводилось.

Модуль не создаёт API/UI и не дублирует очередь или БД. Все секреты читаются
из env и не логируются; `Settings.public_dict()` не содержит ключ.

## Документация для интеграции

- [`docs/INTEGRATION.md`](docs/INTEGRATION.md) — протоколы адаптеров, SQL-
  маппинги, формат очереди, чек-лист для Даниила/Владислава.
