# Интеграция `ai_engine` с SXSVN

Документ описывает контракты между модулем и остальной системой. Модуль
намеренно не владеет таблицами новостей/компаний и не создаёт новую очередь,
API или UI.

## 1. Что модуль ожидает от системы

| Компонент | Владелец | Что нужно |
|---|---|---|
| Redis-очередь | владелец новостного пайплайна | существующие ключи и JSON-сообщения; модуль только читает/подтверждает |
| Таблица новостей | владелец новостного пайплайна | стабильный `news_id`, текст, `content_version` (или хеш модуля) |
| Профили компаний | владелец данных | стабильный `company_id`, деятельность, продукты, ресурсы, география, контрагенты |
| Связи с контрагентами | владелец данных | тип связи, `confirmed`, значимость, доказательства |
| Банковская аналитика | владелец аналитики | индекс, доступный через `AnalyticsIndex.search()` |
| Расчёты | владелец аналитики | `CalculationRepository.list_calculations()` |
| LLM | инфраструктура | OpenAI-совместимый endpoint и ключ в env |

Если чего-то нет, модуль обязан сообщить об этом в `limitations` /
`missing_data`, а не подставлять вымышленные данные.

## 2. Протоколы адаптеров

Все протоколы находятся в `ai_engine/storage/base.py` и проверяются
структурно (`runtime_checkable`), поэтому наследование не требуется.

```python
class NewsRepository(Protocol):
    def get_news(self, news_id: str) -> NewsRecord | None: ...

class CompanyRepository(Protocol):
    def get_company(self, company_id: str) -> CompanyProfile | None: ...

class RelationshipRepository(Protocol):
    def list_relationships(self, company_id: str, limit: int = 50) -> list[Relationship]: ...

class AnalyticsIndex(Protocol):
    def search(self, query: str, limit: int = 3) -> list[AnalyticsSnippet]: ...

class CalculationRepository(Protocol):
    def list_calculations(self, company_id: str) -> list[dict]: ...

class ExternalSearchTool(Protocol):
    def search(self, query: str, limit: int = 3) -> list[dict]: ...
```

`IntelStore` (в том же файле) — хранилище только собственных таблиц `intel_*`.
`NewsIntelligenceService` принимает его и адаптеры явно.

### 2.1. Новости

`NewsRecord`:

```python
NewsRecord(
    news_id="news-123",
    title="...",
    text="...",
    source_id="...",
    content_version="...",   # пусто => модуль считает хеш от title+text
    published_at=None,
    url=None,
    language="ru",
)
```

Требования:

- `news_id` уникален и стабилен;
- текст — полный текст, по которому можно проверить цитаты;
- если пайплайн обновляет текст, он **обязан** изменить
  `content_version`; иначе модуль может переиспользовать старый результат.
  При пустом `content_version` модуль считает собственный хеш, но при
  изменении текста хеш изменится только если адаптер отдаёт новый текст.

### 2.2. Компании

`CompanyProfile` — не только название и отрасль. Для содержательной оценки
нужны `activities`, `products_services`, `processes`, `key_resources`,
`cost_drivers`, `geography`, `suppliers`, `customers`, `financials`,
`known_risks`.

Важно: пустые поля означают «данных нет» (`missing_data`), а не «фактора нет».
Совпадение названия или отрасли не является доказательством связи.

`profile_version` можно не заполнять — модуль вычислит хеш профиля. Если
профиль обновляется, версия должна измениться (при автоматическом хеше это
произойдёт само).

### 2.3. Связи с контрагентами

`Relationship`:

```python
Relationship(
    relationship_id="rel-1",
    company_id="company-456",
    counterparty_id="company-789",
    counterparty_name="...",
    relation_type=RelationType.SUPPLIER,
    confirmed=True,
    significance=Significance.HIGH,
    evidence=["договор/раскрытие"],
    source="...",
)
```

Правила, которые нельзя обходить адаптером:

- `RelationType.PAYMENT_LINK` — это платёж, а не зависимость; движок не
  использует такую связь как доказательство и добавляет ограничение;
- `confirmed=False` остаётся гипотезой (`confirmed=false` в evidence);
- `SUPPLIER`/`CUSTOMER` без высокой/средней значимости — гипотеза;
- отсутствие связей не является отрицательным фактом.

### 2.4. Банковская аналитика

`AnalyticsIndex.search()` возвращает `AnalyticsSnippet`:

```python
AnalyticsSnippet(
    snippet_id="...",
    title="...",
    text="...",
    source="...",
    score=0.8,
    url=None,
)
```

Движок цитирует только фактически полученные сниппеты. Нельзя возвращать
«типовые» отраслевые тексты без источника: они попадут в доказательства как
реальные. Если индекс ещё не готов, используйте
`UnavailableAnalyticsIndex` (по умолчанию в `build_service`).

`CalculationRepository.list_calculations()` возвращает словари с
`calculation_id`/`id`/`name` и содержимым расчёта. Движок включает их в
`sources_used` и передаёт в LLM-синтез, но не пересчитывает.

`ExternalSearchTool` опционален. Его результаты всегда
`confirmed=False`, помечают событие как гипотезу и требуют проверки.

## 3. Подключение к SQL

### 3.1. SQLite (локально, тесты)

```python
from ai_engine import Settings
from ai_engine.service import build_service

settings = Settings.from_env()
service = build_service(settings)   # читает INTEL_SQLITE_PATH, создаёт intel_* таблицы
```

`SqlStore.sqlite()` создаёт файл и применяет `storage/schema.sql`. Для
SQLite используйте отдельное соединение на процесс/поток; не делите одно
соединение между потоками.

### 3.2. PostgreSQL (production)

```python
import psycopg

from ai_engine import Settings
from ai_engine.service import NewsIntelligenceService
from ai_engine.storage.sql import (
    SqlCompanyRepository,
    SqlNewsRepository,
    SqlRelationshipRepository,
    SqlStore,
)

settings = Settings.from_env()
conn = psycopg.connect("postgresql://...")

store = SqlStore(conn)          # placeholder "%s" определяется автоматически
store.ensure_schema()           # CREATE TABLE IF NOT EXISTS intel_*

service = NewsIntelligenceService(
    settings=settings,
    store=store,
    news=SqlNewsRepository(conn, table="news", columns={"news_id": "id"}),
    companies=SqlCompanyRepository(conn, table="companies", columns={"company_id": "id"}),
    relationships=SqlRelationshipRepository(conn, table="company_relationships"),
    analytics=my_analytics_index,       # реализация AnalyticsIndex
    calculations=my_calculations,       # реализация CalculationRepository
    external_search=None,               # опционально
)
```

`SqlStore` поддерживает DB-API 2.0. Для драйвера с другим стилем
placeholder передайте `SqlStore(conn, placeholder="%s")`. Идентификаторы
таблиц/колонок валидируются, значения передаются параметрами.

### 3.3. Маппинг колонок

По умолчанию `SqlNewsRepository` ожидает:

| Логическое поле | Колонка |
|---|---|
| `news_id` | `id` |
| `title` | `title` |
| `text` | `text` |
| `source_id` | `source_id` |
| `content_version` | `content_version` |
| `published_at` | `published_at` |
| `url` | `url` |
| `language` | `language` |

`SqlCompanyRepository` ожидает `id`, `name`, `industry`, `subindustry`,
`activities`, `products_services`, `processes`, `key_resources`,
`cost_drivers`, `geography`, `suppliers`, `customers`, `financials`,
`known_risks`, `data_sources`, `updated_at`, `profile_version`.
JSON-поля могут быть JSON-текстом или массивами.

Если схема другая, передайте `columns={...}` или реализуйте протокол вручную.
Модуль не выполняет `ALTER TABLE` чужих таблиц.

### 3.4. Схема `intel_*`

Применение:

```powershell
python -m ai_engine init-db --path .\intel.db
# PostgreSQL:
psql "$DATABASE_URL" -f ai_engine/storage/schema.sql
```

Таблицы:

- `intel_news_processing` — текущий технический результат по новости;
- `intel_news_facts` — факты, отдельно от оценок компаний;
- `intel_company_assessments` — текущая оценка `(news_id, company_id)`;
- `intel_company_assessment_history` — история изменений оценки;
- `intel_insight_analyses` — версии анализа;
- `intel_refinements` — уточнения пользователя.

Схема идемпотентна (`IF NOT EXISTS`) и не меняет чужие таблицы.

## 4. Redis-очередь

### 4.1. Ключи

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `NEWS_QUEUE_KEY` | `sxsvn:news:queue` | LIST ожидающих задач |
| `NEWS_PROCESSING_KEY` | `sxsvn:news:processing` | LIST зарезервированных |
| `NEWS_JOB_META_KEY` | `sxsvn:news:job_meta` | HASH token → метаданные |
| `NEWS_RETRY_DELAY_KEY` | `sxsvn:news:delayed` | ZSET отложенных retry |
| `NEWS_DEAD_LETTER_KEY` | `sxsvn:news:dead_letter` | LIST окончательно упавших |

Продюсер делает `LPUSH` в `NEWS_QUEUE_KEY`; worker забирает справа
(`BLMOVE ... RIGHT LEFT`), то есть очередь работает как FIFO. Не создавайте
параллельную очередь: используйте эти ключи или передайте совместимый
`JobQueue`.

### 4.2. Сообщения

```json
{
  "job_type": "news.process",
  "news_id": "news-123",
  "content_version": "sha256:...",
  "job_id": "job-...",
  "attempts": 0
}
```

Типы:

- `news.process` — извлечь факты (обязателен `news_id`);
- `company.assess` — оценить новость для компании (нужен `company_id`);
- `company.analyze` — построить insight (нужен `company_id`,
  `payload.event_ids` опционально; по умолчанию `[news_id]`).

`job_id` и `enqueued_at` могут быть опущены — будут сгенерированы.
`attempts` увеличивается при retry. Неизвестный `job_type`, отсутствие
`news_id` или `company_id` для company-задач → dead-letter без бесконечных
попыток.

### 4.3. Гарантии

1. Worker резервирует задачу (перемещает в processing list).
2. Выполняет обработку и **сохраняет** результат.
3. Только после этого вызывает `ack`.
4. При падении процесса задача остаётся в processing и возвращается в очередь
   после `WORKER_VISIBILITY_TIMEOUT`; после `WORKER_MAX_ATTEMPTS` уходит в
   dead-letter.
5. При ошибке сохранения ack не выполняется.
6. Ошибки LLM/сети/хранилища — retryable; `company_not_found`,
   `config_error`, `processing_not_ready` — сразу dead-letter.
7. `news_not_found` считается retryable (продюсер мог опередить появление
   новости), но ограничен числом попыток.

Повторная доставка безопасна: результат идемпотентен по версиям, повторный
`process_news` вернёт сохранённый результат, если версии совпадают.

### 4.4. Проверка очереди

```powershell
python -m ai_engine check-config
python -m ai_engine worker --max-jobs 1
```

Логи содержат `job reserved`, `job acked`, `job failed`, `job dead-lettered`,
но не содержат API-ключ.

## 5. LLM

- Endpoint: `LLM_BASE_URL` (по умолчанию
  `https://changellenge.up.railway.app/v1`).
- Модель по умолчанию: `deepseek/deepseek-v4.1-flash`. Это **предварительный**
  выбор; сравнительных замеров качества/стоимости в модуле нет.
- Разрешённые модели перечислены в `ai_engine/config.py` (`KNOWN_MODELS`).
  `LLM_ALLOW_ANY_MODEL=1` — только для локального mock-провайдера.
- `CLASSIFIER_MODE`:
  - `llm` — только LLM (без ключа будет `ConfigError`);
  - `rules` — детерминированные правила без сети;
  - `auto` — LLM при наличии ключа, иначе правила.
- Извлечение фактов и insight-синтез требуют LLM. Без ключа
  `process_news` вернёт понятную ошибку конфигурации, а не фиктивные факты.
- Структурированный вывод: `LLM_RESPONSE_FORMAT=json_object`; ответ
  валидируется на стороне клиента, поэтому провайдер не обязан поддерживать
  `json_schema`.
- Версии промптов: `news-facts.v1`, `company-relevance.v1`,
  `company-insight.v1`, `company-adaptation.v3`. При изменении семантики
  промпта версию нужно поднять, иначе старые результаты будут считаться
  совместимыми.

## 6. Правила, которые модуль соблюдает

- Технический статус (`processed`/`failed`) отделён от бизнес-класса
  (`KEEP`/`DROP`/`REVIEW`).
- Факты компании-независимы; оценки компаний хранятся отдельно.
- `KEEP` без пригодного доказательства понижается валидатором.
- Отсутствие данных о компании не трактуется как отрицательный факт.
- Совпадение отрасли/названия недостаточно для `KEEP`.
- Платёжные ссылки не являются доказательством зависимости.
- Банковская аналитика цитируется только если реально получена.
- В insight явно различаются `fact`, `inference`, `hypothesis`,
  `user_statement`; внешние результаты неподтверждены.
- Не более двух дополнительных поисков на анализ, лимит в коде.
- Уточнение пользователя — `user_statement`, `confirmed=False`; оно меняет
  зависимые выводы и добавляет ограничения, но не становится внешним фактом.
- Секреты не попадают в код, логи и `public_dict()`.

## 7. Чек-лист интеграции

### Со стороны новостного пайплайна (Даниил)

- [ ] Подтвердить имена ключей Redis или передать их через `NEWS_*`.
- [ ] Подтвердить JSON-формат сообщений и порядок `LPUSH`/`BRPOPLPUSH`.
- [ ] Дать доступ к таблице новостей и маппинг колонок.
- [ ] Гарантировать стабильный `news_id` и обновление `content_version` при
      изменении текста.
- [ ] Не запускать второй consumer той же очереди без согласования: задачи
      идемпотентны, но нагрузка/порядок могут измениться.

### Со стороны данных/аналитики (Владислав)

- [ ] Реализовать `CompanyRepository` (или дать схему и маппинг).
- [ ] Реализовать `RelationshipRepository`; заполнить `confirmed` и
      `significance`; не помечать платёжные ссылки как зависимости.
- [ ] Реализовать `AnalyticsIndex` поверх подготовленного индекса; вернуть
      `AnalyticsSnippet` с реальными `source`/`text`.
- [ ] Реализовать `CalculationRepository`, если готовые расчёты должны
      использоваться.
- [ ] Подтвердить актуальность и версионирование профилей компаний.

### Со стороны инфраструктуры

- [ ] Задать `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` в секретах деплоя.
- [ ] Установить `redis` (`pip install -e ".[redis]"`) и проверить `PING`.
- [ ] Применить `storage/schema.sql` к рабочей БД.
- [ ] Запустить `python -m ai_engine check-config` и `worker --max-jobs 1`.
- [ ] Проверить один реальный вызов LLM и один реальный Redis-job в staging.
- [ ] Утвердить модель по результатам собственного сравнения (текущий выбор —
      предварительный).

## 8. Известные ограничения

- Живой вызов LLM не проверялся: нет ключа/подтверждённого доступа.
- Redis проверен только на in-memory fake; `redis` не установлен в среде
  разработки.
- Нет реальных таблиц новостей/компаний и подготовленного индекса аналитики,
  поэтому production end-to-end не подтверждён.
- SQLite-хранилище не рассчитано на многопоточную запись; для production
  используйте PostgreSQL и пул соединений.
- Insight-синтез опционален: без LLM движок строит детерминированный результат
  с ограничениями; это не заменяет полноценный аналитический вывод.
- Модуль не реализует HTTP API/UI и не выполняет миграции чужих таблиц.
