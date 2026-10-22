"""Каталог аналитических материалов Альфа-Банка из кейса (Приложение 2).

Дата отчёта берётся из текста первой страницы отчёта (``date_source``),
а не из даты загрузки файла или метаданных PDF (там дата сканирования,
28.09.2026, — она одинакова для всех файлов и ничего не значит).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReportDocument:
    document_id: str
    file: str
    title: str
    #: ISO-дата публикации отчёта или None, если в отчёте её нет.
    report_date: str | None
    #: Период, к которому относятся данные отчёта (для людей и LLM).
    period: str
    series: str
    topics: tuple[str, ...]
    date_source: str
    notes: tuple[str, ...] = field(default_factory=tuple)


DOCUMENTS: tuple[ReportDocument, ...] = (
    ReportDocument(
        document_id="alfa_policy_rate_2026_09_11",
        file="ALFA-Macro_Insights-Policy_Rate_11Sep.pdf",
        title="Ключевая ставка ЦБ РФ: ставка без изменения на 14,0%, риторика стала жестче",
        report_date="2026-09-11",
        period="решение ЦБ РФ 11 сентября 2026",
        series="Macro Insights",
        topics=("ключевая ставка", "денежно-кредитная политика", "инфляция", "инфляционные ожидания",
                "кредитование", "бюджет"),
        date_source="страница 1: «11 сентября 2026 г.»",
    ),
    ReportDocument(
        document_id="alfa_macro_7m26",
        file="ALFA-7M26.pdf",
        title="Макростатистика 7М26: данные по рынку труда не подтверждают охлаждения экономики",
        report_date="2026-09-07",
        period="январь–июль 2026",
        series="Макростатистика",
        topics=("рынок труда", "зарплаты", "розничная торговля", "промышленность", "финансовый результат компаний",
                "кредитование"),
        date_source="страница 1: «7 сентября 2026 г.»",
    ),
    ReportDocument(
        document_id="alfa_macro_1h26",
        file="ALFA-macro_stat1H26.pdf",
        title="Макростатистика 1П26: слабый рост 1П26 смещает прогноз роста ВВП на 0,1% за год",
        report_date="2026-07-31",
        period="первое полугодие 2026",
        series="Макростатистика",
        topics=("ВВП", "экономический рост", "макростатистика"),
        date_source="страница 1: «31 июля 2026 г.»",
    ),
    ReportDocument(
        document_id="alfa_consumption",
        file="ALFA-consumption.pdf",
        title="Потребление на пике",
        report_date="2026-08-05",
        period="данные по июнь–июль 2026, прогноз на 4К26",
        series="Macro Insights",
        topics=("потребление", "потребительский спрос", "розничная торговля", "доходы населения"),
        date_source="страница 1: «5 августа 2026 г.»",
    ),
    ReportDocument(
        document_id="alfa_inflation_tracker_aug_25_31",
        file="Inflation_tracker_02.pdf",
        title="Инфляция 25–31 августа: основные выводы",
        report_date="2026-09-02",
        period="неделя 25–31 августа 2026",
        series="Инфляционный трекер",
        topics=("инфляция", "цены", "потребительские цены", "бензин", "топливо", "плодоовощи"),
        date_source="страница 1: «2 сентября 2026 года»",
    ),
    ReportDocument(
        document_id="alfa_bond_brief_2026_09_14",
        file="Бонд_Бриф_14092026.pdf",
        title="Бонд Бриф: итоги недели",
        report_date="2026-09-14",
        period="неделя до 14 сентября 2026",
        series="Бонд Бриф",
        topics=("облигации", "ОФЗ", "доходности", "долговой рынок", "корпоративные облигации"),
        date_source="имя файла (14092026); на страницах даты нет, текст описывает заседание ЦБ «в пятницу 11 сентября»",
    ),
    ReportDocument(
        document_id="alfa_gold_digest_2026_08",
        file="Gold_digest_August26.pdf",
        title="Обзор рынка золота, август 2026",
        report_date=None,
        period="август 2026",
        series="Gold digest",
        topics=("золото", "драгоценные металлы", "сырьевые рынки", "ФРС"),
        date_source="обложка: «Август 2026»; точный день публикации не указан",
    ),
    ReportDocument(
        document_id="alfa_fi_strategy_3q26",
        file="FI_Strategy_3Q26_22072026.pdf",
        title="Долговой рынок. Тактический взгляд на 3-й квартал 2026 г.: время собирать камни",
        report_date="2026-07-22",
        period="3-й квартал 2026",
        series="Стратегия долгового рынка",
        topics=("облигации", "ОФЗ", "долговой рынок", "ключевая ставка", "кривая доходности", "кредитный риск"),
        date_source="страница 1 (титульный слайд): «22 июля 2026 г.»",
    ),
    ReportDocument(
        document_id="alfa_russian_steels_1q21",
        file="ALFA_Russian_Steels_1К21PreviewUpdate_15Apr2021.pdf",
        title="Российская сталь: ралли цен продолжается; прогноз результатов за 1К21",
        report_date="2021-04-15",
        period="1-й квартал 2021",
        series="Рынок акций: металлы и горная добыча",
        topics=("сталь", "металлургия", "цены на сталь", "Северсталь", "НЛМК", "ММК", "экспорт"),
        date_source="страница 1: «15 апреля 2021»",
        notes=("Отчёт 2021 года: устарел относительно текущей даты кейса (2026). "
               "Можно использовать только как исторический контекст, не как текущую позицию банка.",),
    ),
)

_BY_FILE = {d.file: d for d in DOCUMENTS}
_BY_ID = {d.document_id: d for d in DOCUMENTS}


def document_by_file(file: str) -> ReportDocument | None:
    return _BY_FILE.get(file)


def document_by_id(document_id: str) -> ReportDocument | None:
    return _BY_ID.get(document_id)
