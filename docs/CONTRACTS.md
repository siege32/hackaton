# Контракт `ai_engine`

`ai_engine` — Python-пакет для извлечения фактов из новости, оценки для компании
и анализа влияния. Пакет не предоставляет HTTP API или пользовательский
интерфейс. Публичный фасад: `NewsIntelligenceService` из
`ai_engine.service`; основные модели экспортируются из `ai_engine`.

## Порядок вызовов

Сначала необходимо обработать новости: `analyze_company_impact` не извлекает
факты автоматически и вернёт `failed` с причиной `no processing result`, если
`process_news` для новости ещё не завершился.

```python
from ai_engine import Settings, build_service

service = build_service(Settings.from_env())
processing = service.process_news("news-123")
result = service.analyze_company_impact("company-456", ["news-123"])
```

`analyze_company_impact` автоматически классифицирует обработанную новость для
компании, если оценки ещё нет. Методы фасада:

- `process_news(news_id)` — извлечь и сохранить факты;
- `classify_and_extract(news, company_id=None, force=False)` — обработка и,
  опционально, оценка новости для компании;
- `classify_news_for_company(news_id, company_id)` — отдельная оценка;
- `analyze_company_impact(company_id, event_ids, **kwargs)` — отчёт по событиям;
- `get_processing_result(news_id)` и `get_company_news_assessment(...)` — чтение;
- `get_analysis(analysis_id)` и `adapt_company_analysis(...)` — чтение и
  уточнение сохранённого анализа.

## Модели и статусы

`NewsProcessingResult.status` — технический статус (`pending`, `processing`,
`processed`, `failed`). `CompanyNewsAssessment.relevance_class` — бизнес-оценка
(`KEEP`, `DROP`, `REVIEW`). Ошибка обработки не означает `DROP`.

Анализ компании возвращает `InsightResult` со статусом
`completed`/`partial`/`failed`, списком `ImpactEvent`, summary, рисками,
возможностями, unknowns, рекомендациями, ограничениями, использованными
источниками и числом дополнительных поисков. Лимит дополнительных поисков
жёстко ограничен двумя.

`ImpactEvent.evidence` ссылается на известные факты, связи, аналитику,
расчёты или пользовательские утверждения. Пользовательское уточнение имеет
`kind="user_statement"`, `confirmed=False` и само по себе не становится
подтверждённым внешним фактом. При настроенной LLM уточнения, кроме явной
смены горизонта и исключения события, повторно проходят adaptation prompt;
ответ сверяется с доступными фактическими данными. Без LLM применяется
детерминированная осторожная отметка гипотезы.

## Ошибки LLM

Ошибки наследуются от `IntelError` и содержат безопасный `code` и `details`.
`LLMRetriesExhausted.details` включает число попыток и последнюю причину;
обрезанный ответ с `finish_reason="length"` не повторяется тем же запросом и
возвращает `LLMInvalidResponseError` с причиной в `details`.

## Конфигурация и команды

`Settings.from_env()` читает окружение процесса и `.env` в текущей директории;
переменные процесса имеют приоритет. API-ключ не включается в публичную
конфигурацию и логи. Значение `LLM_MAX_TOKENS` по умолчанию — `8000`.

```powershell
python -m ai_engine check-config
python -m ai_engine init-db --path .\intel.db
python -m ai_engine worker --max-jobs 10
```

Установленная команда `sxsvn-news-worker` запускает тот же worker. Для LLM
доступен extra `.[llm]`, для Redis — `.[redis]`.

Worker выполняет задания из очереди асинхронно относительно вызывающего
приложения. Приложение может отслеживать статус по сохранённому
`NewsProcessingResult`/`InsightResult`; отдельный HTTP endpoint статуса модуль
не предоставляет. `analyze_company_impact` в текущем API синхронен, поэтому
UI/host-приложение должно вызывать его внутри фоновой задачи и отдавать клиенту
идентификатор/статус своей задачи.

Хранилища, профиль компании, отношения, подготовленная аналитика и внешние
поиски подключаются через адаптеры `NewsIntelligenceService`. Модуль создаёт
только собственные таблицы `intel_*` в SQLite-адаптере; production-хранилища
подключаются явно.
