# Контракт интеграции SXSVN

Этот документ описывает текущий код в репозитории. `data_pipeline` владеет
каталогами и адаптерами данных, `ai_engine` выполняет извлечение и анализ,
`api` соединяет их и обслуживает веб-интерфейс. Бизнес-логика данных и модели
остается внутри соответствующих модулей.

## Поток запроса

```text
Web UI -> FastAPI -> data_pipeline repositories -> ai_engine service -> SQLite intel store
                        |                                  |
                        +-- company/news/report reads -----+
```

`api.main._service_context()` открывает базу компаний в read-only режиме,
каталог новостей и `SqlStore`, после чего передает адаптеры в
`ai_engine.service.build_service()`. Используются:

- `data_pipeline.news.SqliteNewsRepository` — `NewsRepository`;
- `data_pipeline.profiles.SqliteCompanyRepository` — `CompanyRepository`;
- `data_pipeline.PaymentRelationshipRepository` — `RelationshipRepository`;
- `data_pipeline.PaymentCalculationRepository` — `CalculationRepository`;
- `data_pipeline.reports.BankReportsIndex` — `AnalyticsIndex`;
- `ai_engine.storage.sql.SqlStore` — собственное хранилище результатов и фактов AI.

База компаний не изменяется API. Каталог новостей создается идемпотентно
модулем данных. Задачи HTTP API и данные AI хранятся в SQLite; API не создает
вторую Redis-очередь и не запускает worker поверх очереди модуля.

## Данные на границе модулей

### Новости

`SqliteNewsRepository.get_news(news_id)` возвращает `ai_engine.models.NewsRecord`:
стабильный `news_id`, заголовок, текст, источник, URL, дату публикации и
`content_version`. При изменении текста версия обновляется новостным каталогом.
Публикации одного события связываются через `cluster_id`; ссылки на них
возвращает `GET /api/v1/news/{news_id}`.

### Профиль и связи

`SqliteCompanyRepository.get_company(company_id)` возвращает
`ai_engine.models.CompanyProfile`, включая `data_sources`, `profile_version`
и `raw.missing_data`. Пустые поля означают отсутствие сведений. Платежные
связи — наблюдения с `confirmed=False`, а не подтверждение зависимости.

### Аналитика и расчеты

`BankReportsIndex.search()` возвращает только найденные OCR-фрагменты с датой,
страницей, источником и confidence. `PaymentCalculationRepository` передает
готовые расчеты без повторного вычисления в AI-модуле. Если данных нет,
инструменты возвращают пустой результат или ошибку; вымышленные основания не
подставляются.

## Сценарии API

Основные маршруты описаны в [api/README.md](../api/README.md) и схеме
`/docs`. Главный сценарий анализа:

1. UI выбирает `company_id` и `news_id` из API-каталогов.
2. `POST /api/v1/analysis` проверяет, что компания и все новости существуют,
   затем создает job.
3. Фоновая задача вызывает `NewsIntelligenceService.process_news()` для
   извлечения фактов и сохраняет результат через `SqlStore`.
4. После извлечения вызывается `analyze_company_impact()`; ответ и технический
   статус сохраняются в job. UI читает их через `GET /api/v1/jobs/{job_id}`.
5. Уточнение отправляется в
   `POST /api/v1/analysis/{analysis_id}/refinements` и передается в
   `adapt_company_analysis()` того же service.

Технические статусы job: `queued`, `running`, `partial`, `completed`, `failed`.
Они не заменяют бизнес-класс новости (`KEEP`, `REVIEW`, `DROP`) или направление
влияния (`positive`, `negative`, `mixed`, `unknown`). Ошибки отсутствующих ID
возвращаются сразу с HTTP 404; ошибки обработки отражаются в job. Частичный
ответ сохраняет ограничения и список недостающих данных.

## Конфигурация и запуск

Настройки читаются `ai_engine.config.Settings.from_env()`. LLM-таймаут и лимит
ответа по умолчанию — 180 секунд и 8000 токенов; переменные окружения имеют
приоритет над кодовыми значениями. При таймауте LLM-запрос не повторяется.
Скопированный ранее `.env` может содержать старые значения `LLM_TIMEOUT` и
`LLM_MAX_TOKENS`; обновите их до значений из `.env.example`, если они там заданы.

Запуск, сборка базы и сценарии проверки приведены в корневом
[README.md](../README.md). Для production следует перенести фоновые задачи из
процесса FastAPI в устойчивую очередь и использовать подходящую БД; текущая
реализация рассчитана на локальную демонстрацию.
