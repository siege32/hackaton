# Контракт модуля новостной аналитики

Документ описывает, какие данные передавать в модуль и что получать на выходе.

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
- `bank_evidence` — уже найденные банковские материалы;
- `calculations` — рассчитанные показатели, например доля платежей;
- `claims`, `sources`, `news`, `missing_data` — результаты предыдущих этапов.

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
event, priority, company_features, bank_view,
ai_implications, risks, opportunities,
missing_data, next_check, evidence, trace_id, status
```

`status` принимает значения:

- `completed` — анализ завершён;
- `needs_review` — есть результат, но нужны ручная проверка или дополнительные данные;
- `failed` — анализ не выполнен.

Каждое существенное утверждение (`Claim`) должно ссылаться на `evidence_ids`.
Типы доказательств: `event`, `bank`, `calculation`, `hypothesis`. Поля
`missing_data` и `next_check` показывают, что нужно проверить дальше.

`priority`/`priority_score` — приоритет внимания к событию, а не вероятность
убытка или дефолта.

## 6. Релевантность новости

До глубокого анализа модуль оценивает связь события с компанией:

- `KEEP` — событие связано напрямую или через значимого контрагента;
- `REVIEW` — связь вероятна по отрасли/географии, нужна проверка;
- `DROP` — подтверждённой связи нет.

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
