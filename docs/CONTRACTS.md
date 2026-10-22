# Контракт модуля новостной аналитики

Документ описывает, какие данные передавать в модуль и что получать на выходе.

## 0. Поток классификации новости

Классификация публикации — отдельный общий этап, не привязанный к конкретной
компании. Входом служит `NewsItem`; `SQLiteNewsStore.save_news(item)` очищает
HTML, сохраняет исходный и очищенный текст, нормализованный URL, хеш текста и
content-version. Версия меняется при изменении заголовка, текста или источника.
Разные публикации не объединяются по похожести: они могут нести новые цифры,
факты и опровержения.

`NewsClassifier(llm).classify(news, model_id=...)` возвращает
`ClassifiedNews(classification, extraction)`. `NewsClassification` валидирует
`KEEP` / `DROP` / `REVIEW`, причину, неопределённости и идентификаторы источника.
`EventExtraction` содержит цитируемые факты; даты события остаются `null`, если
источник не сообщает их. Ошибка модели или схемы считается технической ошибкой,
не успешной классификацией.

Задача `NewsTask` содержит `task_id`, `news_id`, `news_version`, `task_type`,
`created_at`, `attempt`. `RedisTaskQueue` принимает настроенный приложением
redis-py совместимый клиент и обслуживает логические очереди `news.classify`,
`news.review`, `analysis.deep`. Для первичного worker реализован только
`news.classify`; остальные очереди — интерфейс для интеграции. Список
`<prefix>:<queue>:processing` позволяет восстановить взятую задачу через
`recover_claimed`; DB outbox восстанавливается `recover_pending_tasks`.
Outbox фиксирует момент отправки и повторно выдаёт задачу, если она дольше
`SXSVN_OUTBOX_STALE_SECONDS` (по умолчанию 300 с) остаётся без результата.

Worker проверяет сохранённый результат до LLM-вызова и журналирует каждый запуск.
Classification и facts записываются транзакционно. Сообщение подтверждается только
после успешного commit. Технический статус новости становится `processed` для любого
бизнес-решения, включая `REVIEW`; сам класс и причина сохраняются отдельно.
Исчерпание попыток ставит технический статус `failed` и удаляет outbox-запись.
SQLite — локальный MVP persistence adapter, не основная БД SXSVN; для production
приложение должно внедрить тот же репозиторный контракт на существующую БД. Redis,
сборщик и внешние данные тут не сконфигурированы по умолчанию.

После возврата классификатора worker фиксирует полный результат в
`news_staged_results`, затем основной транзакцией переносит его в результаты и
факты, меняет статус и удаляет checkpoint. Если основная фиксация не удалась,
повторная доставка сначала использует checkpoint без повторного запроса модели.
Сбой процесса в очень коротком промежутке между ответом внешней модели и записью
checkpoint всё ещё может привести к повторному запросу; устойчивый exactly-once
вызов удалённого провайдера без его idempotency API гарантировать нельзя.

Redis-ключи по умолчанию имеют вид `sxsvn:<queue>:ready`, `:processing`,
`:delayed`, `:dead` и `:leases`. Сообщение — JSON с `task_id`, `news_id`,
`news_version`, `task_type`, `created_at` и `attempt`; полные статьи в Redis не
кладутся. Claim выполняется через `BRPOPLPUSH` и записывает lease; ack удаляет
сообщение из processing и lease. Временная ошибка увеличивает attempt и
планирует ограниченный повтор (`SXSVN_WORKER_MAX_ATTEMPTS`,
`SXSVN_WORKER_RETRY_DELAY_SECONDS`), после лимита задача фиксируется как `failed`
и подтверждается. Некорректный JSON сразу уходит в bounded dead-letter и не
возвращается в ready. `run_news_worker` при старте возвращает только
просроченные/осиротевшие processing-сообщения в ready, поэтому не отбирает
задачу у живого worker; `recover_pending_tasks` повторно отправляет актуальные
outbox-записи старше `SXSVN_OUTBOX_STALE_SECONDS`. Устаревшая версия новости
подтверждается как stale и не перезаписывает актуальный результат. Redis должен
быть настроен приложением; его надёжность зависит от конфигурации
persistence/HA хоста.

## 0.1 Оценка новости для компании

Общая классификация публикации не определяет влияние на произвольную компанию.
После неё приложение получает конкретный профиль и вызывает:

```python
from ai_engine.api import classify_news_for_company, get_company_news_assessment

assessment = classify_news_for_company(
    news_id, news_version, company_profile, extraction,
    llm=llm_client, model_id=settings.llm_model, store=store,
)
```

`CompanyNewsAssessment` содержит обязательный `company_id`, версию новости и
профиля, `relevance_class` (`KEEP`/`DROP`/`REVIEW`), отдельно
`impact_direction`, `impact_type`, механизм, горизонт, уверенность, evidence,
ограничения и пробелы. Модельные цитаты сверяются с извлечёнными фактами и
подтверждёнными graph links; `EvidenceCitation` обязан содержать ровно один
`fact_id` или `link_id`, `source_id`, дословную `quote` и `supports`. `KEEP`
требует хотя бы одну цитату, `DROP` не может утверждать направление влияния.
Без модели детерминированный fallback консервативен: совпадение компании или
контрагента даёт связь, совпадение только отрасли/географии даёт `REVIEW`, а
неизвестное направление остаётся `unknown`. Упоминание контрагента само по себе
не доказывает зависимость; для косвенной связи нужен подтверждённый путь в
`company_links`.

SQLite сохраняет результаты отдельно по паре новости—компания и версиям
содержимого, профиля, модели и промпта. `classify_stored_news_for_company`
читает общую классификацию и факты из store, профиль — через
`CompanyProfileRepository`, и переиспользует кэш, пока эти версии не изменились.
Фактический профиль и основную БД компаний/контрагентов предоставляет
приложение; в репозитории нет production-репозитория профилей. Схема SQLite
является адаптером MVP, не миграцией production БД.

## 0.2 Python API и ошибки

`ai_engine.api` предоставляет `classify_and_extract(news, classifier)`,
`process_news(news_id, store, classifier)`, `get_processing_result`,
`classify_news_for_company`, `classify_stored_news_for_company`,
`get_company_news_assessment`, `analyze_company_impact(company_id, event_ids,
context_loader=...)`, `analyze_company_impact_report(...)` и
`adapt_company_analysis(context, clarification)`. Загрузчик контекста, профиль и
внешние инструменты предоставляет host-приложение. Отсутствующая новость или
профиль вызывают `LookupError`; невалидный или не подтверждённый ответ модели —
`LLMError` (с безопасным `code` и `retryable`); ошибки БД и Redis пробрасываются
вызывающему коду. Worker повторяет техническую ошибку до настроенного лимита,
затем помечает запись `failed`; бизнес-решение `REVIEW` успешно сохраняется в
поле класса при техническом статусе `processed`.

Настройки LLM используют `SXSVN_LLM_API_KEY`, `SXSVN_LLM_BASE_URL`,
`SXSVN_LLM_MODEL`, `SXSVN_LLM_TIMEOUT`, `SXSVN_LLM_MAX_RETRIES`,
`SXSVN_LLM_TEMPERATURE`, `SXSVN_LLM_MAX_INPUT_CHARS`,
`SXSVN_LLM_MAX_OUTPUT_TOKENS` и `SXSVN_NEWS_MAX_CHARS`. Допустимые модели
перечислены в `ai_engine/llm_constants.py`; модель по умолчанию выбрана
предварительно, без сравнительных измерений в этом репозитории.

Локальный запускаемый вариант использует `SXSVN_REDIS_URL`,
`SXSVN_NEWS_DB_PATH`, `SXSVN_QUEUE_PREFIX`, `SXSVN_WORKER_MAX_ATTEMPTS`,
`SXSVN_WORKER_RETRY_DELAY_SECONDS`, `SXSVN_WORKER_POLL_TIMEOUT`,
`SXSVN_PROCESSING_LEASE_SECONDS` и `SXSVN_OUTBOX_STALE_SECONDS`; команда
`python -m ai_engine.worker_cli` требует пакет `redis`
(`pip install '.[redis]'`). В приложении API вместо SQLite подключает адаптер к
основной БД SXSVN; worker CLI предназначен для локального MVP.

## 1. Вызов

```python
from ai_engine.insight_engine import insight_engine

result = insight_engine(
    context,
    llm=llm_client,          # опционально; OpenAI-compatible клиент
    toolkit=external_tools,  # адаптер внешних источников
)
```

`context` имеет тип `AnalysisContext`, результат — `AnalysisResult`.

## 2. Вход: `AnalysisContext`

Минимально необходимы:

```python
AnalysisContext(
    company_id="company-123",
    event_id="event-456",
    as_of_date="2026-10-21",
    profile=CompanyProfile(
        company_id="company-123",
        name="Название компании",
        industries=("retail",),
        geographies=("RU",),
        counterparties=("supplier-789",),
    ),
    event_facts=(...),
)
```

Дополнительно можно передать:

- `event` — структурированные метаданные события (`EventExtraction`);
- `company_links` — подтверждённые рёбра графа зависимостей (`CompanyLink`);
- `bank_evidence` — уже найденные банковские материалы;
- `calculations` — рассчитанные показатели, например доля платежей;
- `claims`, `sources`, `news`, `missing_data` — результаты предыдущих этапов;
- `clarification` — уточнение пользователя для повторного анализа;
- `run_id` — идентификатор запуска для трассировки (иначе генерируется).

Все даты передаются в ISO-формате (`YYYY-MM-DD` или ISO datetime). Если дата
события неизвестна, передаётся `null`; дату публикации нельзя подставлять вместо
даты события.

## 3. Внешние источники и функции адаптера

Модуль работает через объект `AgentToolkit`. Реализация адаптера должна иметь
следующие функции и возвращать `ToolResult`:

| Функция | Что принимает | Что возвращает |
|---|---|---|
| `get_company_profile(company_id)` | ID компании | профиль компании |
| `get_counterparty_exposure(company_id, event_context=None)` | ID компании и контекст события | контрагентов, суммы и периоды платежей |
| `search_news(query, period=None, filters=None)` | поисковый запрос, период, фильтры | найденные новости и источники |
| `search_bank_reports(topic, company_context=None)` | тема и контекст компании | банковские документы/выдержки |
| `calculate_exposure(data, parameters=None)` | платежные данные и параметры | рассчитанные показатели |
| `validate_evidence(claims, evidence)` | утверждения и доказательства | статус проверки и замечания |

Статусы `ToolResult`:

- `success` — данные получены;
- `empty` — источник доступен, но данных нет;
- `partial` — получена только часть данных;
- `error` — источник недоступен или произошла ошибка.

Пример успешного ответа:

```python
ToolResult(
    tool="get_company_profile",
    status="success",
    origin="external",
    data={"company_id": "company-123", "name": "Название компании"},
)
```

При `empty` возвращайте пустой `data` и пояснение в `message`. Не подставляйте
синтетические или демонстрационные значения.

## 4. Требования к данным источников

### Новости и событие

Для новости желательно передавать `NewsItem`:

```text
news_id, text, source_id, title, url, published_at, language
```

Событие (`EventFact`) должно содержать `fact_id`, текст факта и `source_ids`.
Для повышения качества релевантности также нужны:

```text
participants, industries, geographies, occurred_at, published_at
```

Для LLM-извлечения и проверки цитат используются `quote`, `value`, `topic`,
`fact_type`, `certainty`, `related_fact_ids`, `verified`. `quote` должен быть
дословным фрагментом исходного текста (допускается отличие только в пробелах,
регистре и завершающей пунктуации); иначе вся экстракция отклоняется.

`source_id` должен однозначно ссылаться на объект `Source` с названием, URL,
датой публикации и типом источника (`news`, `bank`, `registry` и т. п.).

### Профиль компании

Нужны: `company_id`, название, отрасли, географии, список контрагентов и, если
есть, дополнительные признаки в `features`.

### Платежи и контрагенты

Для расчёта зависимости передавайте строки с полями:

```text
counterparty, amount, currency, period, source_id
```

В `parameters` для `calculate_exposure` передаётся общий объём платежей:
`total_outgoing_payments` (допустимо старое имя `total_payments`).

Результат расчёта — аналитический показатель зависимости от контрагента. Это
не доля выручки и не вероятность финансового убытка.

### Граф подтверждённых зависимостей

`AnalysisContext.company_links` содержит `CompanyLink`:

```text
link_id, company_id, counterparty_id, counterparty_name, relation,
direction, confirmed, dependency_confirmed, importance, as_of,
source_ids, evidence_quote
```

Модуль обходит только рёбра с `confirmed=True` **и**
`dependency_confirmed=True`, максимум на два ребра. `relation="payment"` — это
наблюдение, не доказательство зависимости, поэтому такие рёбра исключены из
поиска путей. `evidence_quote` обязателен, если ребро должно цитироваться в
evidence. Простое имя в `profile.counterparties` не создаёт ребро. Флаги
подтверждения выставляет владелец данных; модуль не выводит их из платежей или
упоминаний.

### Банковские материалы

Для подтверждённого материала используйте `BankEvidence`:

```text
source_id, text, verified, value
```

Для поиска документов используются поля `document_id`, `title`, `excerpt`,
`topics`, `published_at`, `sensitivity`. Утверждение о позиции конкретного банка
допустимо только при наличии реального банковского источника.

## 5. Выход: `AnalysisResult`

Основные поля результата:

```text
company_id, event, priority, company_features, bank_view,
ai_implications, risks, opportunities,
missing_data, next_check, evidence, trace_id, status,
relevance, evidence_quality, trace,
clarification_applied, additional_searches_used, sources_used
```

`status` принимает значения:

- `completed` — анализ завершён;
- `needs_review` — есть результат, но нужны ручная проверка или дополнительные данные;
- `failed` — анализ не выполнен.

Каждое существенное утверждение (`Claim`) должно ссылаться на `evidence_ids`.
Типы доказательств: `event`, `bank`, `link`, `calculation`, `hypothesis`.
Для `event`/`bank`/`link` цитата должна быть подтверждена исходным фактом,
банковским материалом или подтверждённым ребром; несвязанные дополнительные
записи каталога не считаются опорой, если на них нет ссылки. Поля
`missing_data` и `next_check` показывают, что нужно проверить дальше.
`additional_searches_used` жёстко ограничен двумя дополнительными поисками.

`priority`/`priority_score` — приоритет внимания к событию, а не вероятность
убытка или дефолта.

## 6. Релевантность новости

До глубокого анализа модуль оценивает связь события с компанией:

- `KEEP` — событие связано напрямую или через подтверждённого значимого
  контрагента/зависимость;
- `REVIEW` — связь вероятна по отрасли/географии, нужна проверка;
- `DROP` — подтверждённой связи нет.

`impact_direction` (`positive`/`negative`/`mixed`/`unknown`) и `impact_type`
(`direct`/`indirect`/`unknown`) хранятся отдельно от бизнес-класса. `DROP` не
создаёт impact-утверждений и не утверждает направление. Подтверждённый граф
может повысить решение до `KEEP` с `impact_type="indirect"`, но направление и
размер эффекта остаются `unknown`, пока нет данных.

Для `REVIEW` и неполных данных результат обычно содержит `missing_data` и статус
`needs_review`.

## 7. Правила интеграции

- Используйте стабильные ID для компаний, событий, фактов и источников.
- Не смешивайте дату события с датой публикации.
- Не скрывайте отсутствие данных: возвращайте `empty` или `partial`.
- Ошибку подключения возвращайте как `error`, не как пустой успешный ответ.
- Не передавайте API-ключи, пароли и персональные секреты в payload и логи.
- Внешние данные должны быть проверяемыми: храните `source_id`, URL и цитату.
- Перед отправкой в модуль проверьте обязательные поля и ISO-формат дат.

## 8. Агрегированный отчёт

`CompanyImpactReport` собирается из per-event `AnalysisResult` и не заменяет
их контракт. Вызов:

```python
from ai_engine.api import analyze_company_impact_report

report = analyze_company_impact_report(
    company_id, event_ids, context_loader=load_context,
    llm=llm_client, toolkit=external_toolkit,
)
```

Поля: `company_id`, `analysis_status` (`completed`/`partial`/`failed`),
`summary`, `events` (`ImpactEvent`), `key_risks`, `potential_opportunities`,
`unknowns`, `recommended_checks`, `sources_used`,
`additional_searches_used`, `priority_score`. `partial` означает, что хотя бы
одно событие имеет `needs_review`/`failed`; `failed` — что не проанализировано
ни одно событие. Эти состояния нельзя выдавать за завершённый анализ.
Результаты другой компании отклоняются с `ValueError`.

## 9. Совместимость и известные ограничения

- Новые поля моделей имеют значения по умолчанию, поэтому старые вызовы
  `AnalysisContext`/`AnalysisResult`/`EventFact` остаются валидными. Новые поля
  добавлены, существующие имена не переименованы.
- `KEEP`/`DROP`/`REVIEW` — бизнес-класс; технический статус обработки хранится
  отдельно и не должен использоваться как бизнес-решение.
- SQLite-схема и `SQLiteNewsStore` — MVP-адаптер. Для основной БД нужно
  реализовать тот же набор операций с той же транзакционной границей
  (результат + факты + outbox/статус).
- Redis и внешние данные предоставляет приложение; модуль не создаёт очередь,
  БД, UI или HTTP-сервер.
- Exactly-once вызов внешнего LLM без idempotency API провайдера невозможен:
  сбой между ответом модели и checkpoint может привести к одному повторному
  запросу; всё после checkpoint воспроизводимо без нового вызова.
- Автоматическая глубокая оценка каждой новости не выполняется: приложение
  выбирает компанию и события и вызывает агент отдельно.
- Тесты не делают реальных платных LLM-вызовов; для ручной проверки нужен
  `SXSVN_RUN_INTEGRATION=1` и ключ в локальном `.env`.
