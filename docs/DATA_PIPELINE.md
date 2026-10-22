# Модуль данных `data_pipeline` (владелец — Данил)

Готовит данные кейса о компаниях для `ai_engine` и API: профили, связи,
расчёты платёжной экспозиции. Всё — обычный Python + SQLite (stdlib), без LLM.

## Быстрый старт

```powershell
pip install -e ".[dev]"
# положить CSV кейса в data/raw/ (папка в .gitignore, данные не коммитим):
#   df_Uid_data_hack.csv, df_Uid_payment_hack.csv, df_Fid_Uid_link_hack.csv
python -m data_pipeline build          # ~30 с -> data/processed/companies.sqlite
python -m data_pipeline report         # отчёт о качестве данных
python -m data_pipeline companies      # 115 компаний крупного бизнеса (выручка > 15 млрд)
python -m data_pipeline profile U_26030
python -m data_pipeline exposure U_26030 --okved B,19
python -m data_pipeline relationships U_26030 --limit 10
python -m pytest tests/test_data_pipeline.py
```

Пути: `DATA_RAW_DIR` (по умолчанию `data/raw`), `DATA_DB_PATH`
(по умолчанию `data/processed/companies.sqlite`).

## Подключение к ai_engine / API (для Владислава и Александра)

```python
from data_pipeline import (
    connect, SqliteCompanyRepository, PaymentRelationshipRepository, PaymentCalculationRepository,
)
from ai_engine.service import build_service

conn = connect("data/processed/companies.sqlite")      # только чтение
service = build_service(
    settings,
    store=store, news=news_repo,
    companies=SqliteCompanyRepository(conn),             # CompanyRepository
    relationships=PaymentRelationshipRepository(conn),   # RelationshipRepository
    calculations=PaymentCalculationRepository(conn),     # CalculationRepository
)
```

Функции-инструменты агента (шаг 14 ТЗ), возвращают `{"tool", "status", "data", ...}`,
`status` ∈ `success | partial | empty | error`:

| Функция | Что делает |
|---|---|
| `profiles.get_company_profile(conn, company_id)` | профиль + `missing_data` |
| `exposure.get_counterparty_exposure(conn, company_id, okved_filter=None)` | без фильтра — структура платежей по отраслям; с фильтром — экспозиция к затронутым отраслям в обе стороны |
| `exposure.calculate_sector_exposure(conn, company_id, okved_filter, direction)` | одно число E с формулой, входами и ограничениями |
| `SqliteCompanyRepository(conn).list_companies(min_revenue=15e9, query=None)` | список для выбора компании в UI |

`okved_filter` — буквы разделов ОКВЭД (`"B"` — добыча) и/или префиксы кодов
(`"19"` — нефтепереработка, `"46.71"` — опт топливом). Перевод «событие → отрасли»
делает агент/LLM.

## Что в профиле (`ai_engine.models.CompanyProfile`)

| Поле | Откуда |
|---|---|
| `industry` / `subindustry` / `activities` | раздел ОКВЭД 2 по коду / `MAINOKVED_NAME` |
| `key_resources` | продукты банка: кредиты, лизинг, факторинг, гарантии |
| `cost_drivers` | правило: есть финансирование → зависимость от ставки; есть ФТС → курс/пошлины/санкции |
| `geography` | город и регион регистрации + страны ВЭД из ФТС (`ВЭД: Китай`) |
| `suppliers` / `customers` | топ-10 получателей / плательщиков по платежам (роль не подтверждена) |
| `financials` | выручка, сегмент, сотрудники, возраст, стадия, собственность, тип клиента, продукты, группа, сводка платежей |
| `data_sources` | происхождение каждого поля: файл, колонки, строка CSV или правило |
| `raw.missing_data` | чего нет в данных (продукты/услуги, рыночная доля, название и т.д.) |

Текст полей подобран под словарь тем `ai_engine/classification/context.py`
(основы «ставк», «кредит», «валют», «пошлин», «санкц»): именно по ним новость
сопоставляется с профилем.

## Связи (`Relationship`), все `confirmed=False`

- `payment_link` — платёж; `direction` = `incoming`/`outgoing`, `share` — доля в платежах направления.
- `other` + `metadata.kind="shared_person"` — общее связанное физлицо.
- `other` + `metadata.kind="same_group"` — один `GROUP_ID` (до 10 крупнейших).

Платёж — наблюдение, а не доказательство зависимости; ai_engine так и трактует.

## Экспозиция

`E = платежи контрагентов из затронутых отраслей / все валидные платежи направления`.
Контрагенты, которых нет в справочнике (их отрасль неизвестна), не считаются
незатронутыми: возвращаются `value` (нижняя граница), `value_upper_bound`
(если все неизвестные затронуты) и `coverage`. Знаменатель 0 → `empty`, `value=None`.

## Качество данных (сборка от 2026-10-22)

- Компании: 85 525 строк → 85 513 загружено. ID `U_428697` встречается 12 раз
  с разными атрибутами — это заглушка, профиль не строится, платежи с ним = неидентифицированный контрагент.
- Крупный бизнес (> 15 млрд): **115** компаний.
- Платежи: 1 380 585 строк; 27 406 самоплатежей исключено (45,7 трлн — в 4,5 раза больше
  всех остальных платежей!); 9 повторяющихся пар суммированы; итого 1 353 162 пары.
- Обе стороны платежа есть в справочнике только для **28,8 %** суммы → покрытие
  отраслей контрагентов ограничено, это видно в `coverage`.
- Физлица: 12 514 связей, только 4 629 указывают на компании из справочника; 969 физлиц связаны с несколькими компаниями.
- Единицы выручки и платежей не указаны — трактуются как рубли; период платежей не указан и не придумывается.
- `AMOUNT_WORKERS_SPARK_GRADATION` пусто в 99,8 % — не используется.

## Дальше

- Индекс аналитики Альфа-Банка (`AnalyticsIndex`): PDF — сканы без текста, нужен OCR.
- Парсер новостей (`NewsRepository` + постановка задач в очередь).
