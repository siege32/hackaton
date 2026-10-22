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

## Аналитика Альфа-Банка (`data_pipeline/reports/`)

PDF кейса — сканы без текстового слоя. Текст распознан **один раз** разрешёнными
LLM и лежит в git: `data/bank_reports/ocr/<document_id>/page_NNN.json`.
Поиск работает по этим файлам, ключ LLM для него не нужен.

```python
from data_pipeline.reports import BankReportsIndex, search_bank_reports

index = BankReportsIndex("data/bank_reports/ocr")       # AnalyticsIndex для ai_engine
index.search("ключевая ставка прогноз", limit=3)         # -> list[AnalyticsSnippet]
search_bank_reports(index, "ключевая ставка", company_context="кредиты")  # инструмент агента со статусом
```

`AnalyticsSnippet.metadata`: `document_id`, `page`, `report_date` (дата отчёта, а не загрузки),
`period`, `stale` (отчёт старше года — например, «Российская сталь» 2021 г.),
`ocr_confidence`, `origin="bank_analytics"`. `source` — готовая подпись:
«Альфа-Банк, «<название>», <дата>, стр. N».

Поиск — BM25 по фрагментам внутри страницы (страница = единица цитирования),
упрощённый русский стемминг, служебный дисклеймер не индексируется.
Каталог отчётов и дат — `data_pipeline/reports/catalog.py`.

### Как распознавали (и почему так)

```powershell
python -m data_pipeline ocr-reports --doc alfa_policy_rate_2026_09_11 --max-cost 5  # платно
python -m data_pipeline ocr-reports --rescore      # пересчитать сверку без LLM
python -m data_pipeline search-reports "прогноз ключевой ставки"
```

Каждая страница рендерится в PNG (200 dpi) и распознаётся двумя моделями:
**`z-ai/glm-5.3-flash`** (основной текст) и **`qwen/qwen3.6-35b-a3b`** (проверка).
Тексты сравниваются по словам; `agreement` — доля слов основного текста,
подтверждённых второй моделью (описания графиков в сверку не входят).
`confidence`: high ≥ 0,98, medium ≥ 0,93, иначе low. Расхождения сохранены в `disagreements`.

Замер на стр. 1 отчёта по ставке (20 контрольных фраз из оригинала):

| Модель | Результат |
|---|---|
| `qwen3-vl-30b` (предложена кейсом для изображений) | пропускает слова и фразы даже в узких полосах страницы |
| `gemma-4-26b` | дёшево, но перефразирует («которую брал Минфин» → «который сделал ЦБ РФ») |
| `glm-5.3-flash`, `qwen3.6-35b` | 20/20, совпадение между собой 99,45 % |

Обе модели по умолчанию «рассуждают» перед ответом: у GLM на плотной странице это ~5 800 скрытых
токенов из 7 100 — ответ упирается в лимит и стоит в 4 раза дороже. Рассуждения отключены
(`extra_body={"reasoning": {"enabled": False}}`): на эталонной странице те же 20/20 и 99,86 %
совпадения с «думающим» вариантом.

Итог: 64 страницы, ~33 ₽ разово (часть страниц распознана ещё в режиме с рассуждениями).
Уверенность: 38 high, 16 medium, 10 low. Low — почти везде страницы с графиками и таблицами,
где модели по-разному оформляют подписи; реальные ошибки — «ОфЗ» вместо «ОФЗ» (FI, стр. 16)
и латиница/кириллица в тикерах облигаций (FI, стр. 26). Повторный запуск берёт кэш.

Порядок выдачи поиска: отбор по релевантности (BM25 + пары слов, мягкая поправка на свежесть),
затем **от новых отчётов к старым** — банк меняет позицию (прогноз ставки на конец 2026 г.:
13,5 % в отчёте от 31.07 → 14,0 % от 11.09). При разных датах в выдаче инструмент
`search_bank_reports` добавляет предупреждение.

## Новости (`data_pipeline/news/`) — шаги 1–5 ТЗ

Каталог событий с первоисточниками (не готовые инсайты). Хранится в
`data/processed/news.sqlite` (в git нет — пересобирается за ~20 с).

```powershell
pip install -e ".[news]"
python -m data_pipeline news-fetch --max-items 30        # все включённые RSS-источники
python -m data_pipeline news-list --limit 20             # лента: одна строка на событие
python -m data_pipeline news-show news_xxx               # публикация + все источники события
python -m data_pipeline news-search "ключевая ставка" --days 7
python -m data_pipeline news-add --url https://...       # новость по ссылке (например, от жюри)
python -m data_pipeline news-add --title "..." --text-file news.txt
python -m data_pipeline news-enqueue                     # задачи news.process в Redis (REDIS_URL)
```

Подключение к ai_engine:

```python
from data_pipeline.news import open_news_db, SqliteNewsRepository, search_news, enqueue_pending

news_conn = open_news_db("data/processed/news.sqlite")
service = build_service(settings, ..., news=SqliteNewsRepository(news_conn))   # NewsRepository
enqueue_pending(news_conn, queue)            # любой JobQueue ai_engine (RedisQueue / InMemoryQueue)
search_news(news_conn, "пошлины удобрения", period=(since, None))  # инструмент агента
```

`content_version` считается той же функцией, что и в `NewsRecord`, поэтому кэш
ai_engine совпадает с каталогом: изменился текст — изменилась версия.

### Источники (`news/sources.py`, проверено 22.10.2026)

| Источник | Полный текст |
|---|---|
| РБК | прямо в RSS (тег `full-text`) |
| Интерфакс, Коммерсантъ, Ведомости, Лента, Банки.ру | со страницы статьи (trafilatura) |
| ТАСС | страницы отдают 403 — только заголовок и анонс (`text_quality=summary_only`) |
| Банк России, Прайм | отключены: российский корневой SSL-сертификат не входит в стандартный набор |

### Обработка

1. **Сбор:** URL, источник, дата публикации (в UTC) и получения, полный текст, статус.
   Ошибка источника (`error`) отличается от пустой ленты (`empty`); недоступная страница → `partial`, остаётся анонс.
2. **Очистка:** заголовок, лид и текст отдельно; служебные строки («Новость дополняется», подписки,
   выходные данные газеты) убираются, числа и даты остаются.
3. **Дубли:** тот же URL → обновление (`updated`, старая версия в `news_history`, новая `content_version`);
   тот же хеш текста → `duplicate` (в обработку не идёт); похожие публикации в окне 72 ч →
   общий `cluster_id`. Похожесть — Жаккар по основам слов заголовка и лида (порог 0,35; 0,22 при общем
   числе вроде «70 БПЛА»), проверено на 187 реальных новостях без ложных склеек.
   Для каждой публикации кластера `novelty`: новые предложения, новые числа, опровержение.
   В обработку LLM идёт первая публикация события и те, что **добавляют факты**.
4. **Предотбор правилами** (`news/prefilter.py`): KEEP — тема в заголовке, ≥3 упоминаний тем словаря
   ai_engine или критичный маркер (ставка, санкции, пошлины, налог...); REVIEW — слабые признаки,
   инфраструктура (суда, порты, НПЗ, ЛЭП) или только анонс; DROP — нет деловой темы, спорт/культура,
   старше 14 дней. Темы ищутся **с начала слова**: в ai_engine подстроки дают ложные темы
   («курс» в «ресурс», «ставк» в «поставка», «налог» в «аналогичный»).
5. **Повторный запуск идемпотентен:** второй прогон тех же лент — 187 из 187 `unchanged`.

Прогон 22.10.2026: 187 публикаций → KEEP 116, REVIEW 31, DROP 40, в обработку LLM — 147.

### Проверено сквозным прогоном с ai_engine (LLM)

Новость «Российский бизнес перестал ждать снижения ключевой ставки» (Лента, 21.10) →
`process_news` извлёк 6 фактов с цитатами → `classify_news_for_company`:
`U_26030` (кредиты и лизинг) — KEEP, negative/indirect, обоснование — драйвер затрат из профиля;
`U_408263` (без кредитов) — REVIEW.
