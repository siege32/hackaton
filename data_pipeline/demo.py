"""Синтетический демо-набор CSV в формате кейса (ТЗ: «синтетические тестовые данные»).

Данные кейса в репозиторий не кладём, поэтому без них проект нельзя прогнать
целиком. Этот модуль детерминированно (фиксированный seed) генерирует три CSV
с теми же колонками, что и у кейса, но с выдуманными компаниями:

- 12 крупных компаний (выручка >= 15 млрд ₽ — их показывает интерфейс) в
  отраслях, которые чаще всего задевают новости: топливо, металлургия,
  удобрения, зерно, стройка, ритейл, импорт оборудования, IT, энергетика,
  перевозки;
- ~70 контрагентов поменьше — поставщики и покупатели крупных, чтобы у
  платёжной экспозиции и связей было что показывать;
- платежи (в том числе дубль пары, самоплатёж и платёж «вне справочника» —
  те же особенности, что в настоящих данных) и связанные физлица.

Идентификаторы с префиксом ``DEMO`` (``U_DEMO_001``), чтобы их нельзя было
перепутать с компаниями кейса. Готовые файлы лежат в ``data/demo/``;
перегенерировать: ``python -m data_pipeline demo-data``.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Any

from .config import COMPANIES_CSV, PAYMENTS_CSV, PERSON_LINKS_CSV

DEMO_DIR = Path("data/demo")
SEED = 20261026

_COMPANY_HEADER = (
    "SYNTHETIC_U;ORG_TYPE;OKFS_NAME;COMPANY_AGE_YEARS_GRADATION;ECO_MARK_LABEL;MAINOKVED_CODE;"
    "MAINOKVED_NAME;NEGATIVEMARK;AMOUNT_WORKERS_FNS_GRADATION;AMOUNT_WORKERS_SPARK_GRADATION;CL_TYPE;"
    "CL_PRODUCT_RKO_MARK;CL_PRODUCT_GAR_MARK;CL_PRODUCT_LOA_MARK;CL_PRODUCT_LEA_MARK;CL_PRODUCT_DEP_MARK;"
    "CL_PRODUCT_ZPP_MARK;FTS_FLAG;FTS_COUNTRY_NAME;FACTORING_MARK;REVENUE_COMPANY;GROUP_ID;CITY;REGION"
).split(";")

# Крупные компании: (ОКВЭД, название, выручка млрд ₽, город, регион, страны ВЭД, кредит, лизинг)
_LARGE = (
    ("46.71", "Торговля оптовая твердым, жидким и газообразным топливом и подобными продуктами",
     118.4, "г. Москва", "Москва", "", 1, 0),
    ("24.10", "Производство чугуна, стали и ферросплавов",
     86.2, "г. Магнитогорск", "Челябинская область", "КИТАЙ, ТУРЦИЯ", 1, 1),
    ("20.15", "Производство удобрений и азотных соединений",
     71.5, "г. Пермь", "Пермский край", "ИНДИЯ, БРАЗИЛИЯ, КИТАЙ", 1, 0),
    ("47.11", "Торговля розничная преимущественно пищевыми продуктами, включая напитки, и табачными "
     "изделиями в неспециализированных магазинах", 64.9, "г. Москва", "Москва", "", 1, 1),
    ("35.11", "Производство электроэнергии",
     49.7, "г. Екатеринбург", "Свердловская область", "", 1, 0),
    ("41.20", "Строительство жилых и нежилых зданий",
     38.3, "г. Санкт-Петербург", "Санкт-Петербург", "", 1, 0),
    ("46.69", "Торговля оптовая прочими машинами и оборудованием",
     31.6, "г. Москва", "Москва", "КИТАЙ", 0, 1),
    ("10.11", "Переработка и консервирование мяса",
     24.8, "г. Белгород", "Белгородская область", "", 1, 1),
    ("01.11", "Выращивание зерновых (кроме риса), зернобобовых культур и семян масличных культур",
     21.9, "г. Краснодар", "Краснодарский край", "ТУРЦИЯ, ЕГИПЕТ", 1, 1),
    ("49.41", "Деятельность автомобильного грузового транспорта",
     18.2, "г. Новосибирск", "Новосибирская область", "", 0, 1),
    ("52.29", "Деятельность вспомогательная прочая, связанная с перевозками",
     17.1, "г. Санкт-Петербург", "Санкт-Петербург", "КИТАЙ, ОАЭ", 0, 0),
    ("62.01", "Разработка компьютерного программного обеспечения",
     16.0, "г. Москва", "Москва", "", 0, 0),
)

# Контрагенты: (ОКВЭД, название, выручка млн ₽ от-до)
_COUNTERPARTY_KINDS = (
    ("06.10", "Добыча нефти и нефтяного (попутного) газа", (3_000, 9_000)),
    ("19.20", "Производство нефтепродуктов", (2_000, 12_000)),
    ("05.10", "Добыча и обогащение угля и антрацита", (800, 5_000)),
    ("07.10", "Добыча и обогащение железных руд", (1_500, 8_000)),
    ("46.21", "Торговля оптовая зерном, необработанным табаком, семенами и кормами для сельскохозяйственных животных",
     (300, 3_000)),
    ("10.61", "Производство продуктов мукомольной и крупяной промышленности", (200, 2_000)),
    ("28.30", "Производство машин и оборудования для сельского и лесного хозяйства", (400, 2_500)),
    ("46.75", "Торговля оптовая химическими продуктами", (300, 4_000)),
    ("23.61", "Производство изделий из бетона для использования в строительстве", (150, 1_500)),
    ("43.21", "Производство электромонтажных работ", (80, 900)),
    ("43.99", "Работы строительные специализированные прочие, не включенные в другие группировки", (60, 700)),
    ("49.20", "Деятельность железнодорожного транспорта: грузовые перевозки", (1_000, 6_000)),
    ("50.20", "Деятельность морского грузового транспорта", (500, 4_000)),
    ("45.20", "Техническое обслуживание и ремонт автотранспортных средств", (40, 400)),
    ("33.12", "Ремонт машин и оборудования", (50, 600)),
    ("68.20", "Аренда и управление собственным или арендованным недвижимым имуществом", (50, 1_200)),
    ("69.20", "Деятельность по оказанию услуг в области бухгалтерского учета", (10, 120)),
    ("73.11", "Деятельность рекламных агентств", (20, 300)),
    ("10.51", "Производство молока (кроме сырого) и молочной продукции", (300, 3_000)),
    ("46.34", "Торговля оптовая напитками", (200, 2_500)),
    ("17.21", "Производство гофрированной бумаги и картона, бумажной и картонной тары", (150, 1_500)),
    ("58.29", "Издание прочего программного обеспечения", (30, 500)),
    ("35.12", "Передача электроэнергии и технологическое присоединение к распределительным электросетям",
     (2_000, 9_000)),
    ("80.10", "Деятельность частных охранных служб", (20, 200)),
)

# Кому из крупных какие контрагенты по ОКВЭД (поставщики → крупная компания платит им;
# покупатели → они платят крупной компании).
_SUPPLIERS = {
    "46.71": ("19.20", "06.10", "49.20", "50.20"),
    "24.10": ("07.10", "05.10", "49.20", "35.12", "33.12"),
    "20.15": ("06.10", "46.75", "49.20", "50.20", "35.12"),
    "47.11": ("10.51", "46.34", "10.11", "68.20", "73.11", "80.10"),
    "35.11": ("05.10", "06.10", "33.12", "43.21"),
    "41.20": ("23.61", "43.21", "43.99", "24.10", "68.20"),
    "46.69": ("50.20", "52.29", "28.30", "68.20"),
    "10.11": ("46.21", "17.21", "45.20", "49.41"),
    "01.11": ("28.30", "46.75", "19.20", "45.20"),
    "49.41": ("19.20", "45.20", "68.20"),
    "52.29": ("50.20", "49.20", "68.20", "80.10"),
    "62.01": ("58.29", "68.20", "69.20", "73.11"),
}
_BUYERS = {
    "46.71": ("49.41", "01.11", "45.20"),
    "24.10": ("41.20", "28.30", "23.61"),
    "20.15": ("01.11", "46.21", "46.75"),
    "47.11": (),
    "35.11": ("35.12",),
    "41.20": ("68.20",),
    "46.69": ("01.11", "33.12"),
    "10.11": ("47.11", "46.34"),
    "01.11": ("46.21", "10.61", "10.11"),
    "49.41": ("47.11", "10.11", "46.71"),
    "52.29": ("46.69", "24.10", "20.15"),
    "62.01": ("47.11", "35.11", "46.69"),
}

_CITIES = (
    ("г. Москва", "Москва", "Москва", "МОСКОВСКИЙ"),
    ("г. Санкт-Петербург", "Санкт-Петербург", "Санкт-Петербург", "СЕВЕРО-ЗАПАДНЫЙ"),
    ("г. Новосибирск", "Новосибирская область", "Новосибирск", "СИБИРСКИЙ"),
    ("г. Екатеринбург", "Свердловская область", "Екатеринбург", "УРАЛЬСКИЙ"),
    ("г. Казань", "Республика Татарстан", "Казань", "ПОВОЛЖСКИЙ"),
    ("г. Краснодар", "Краснодарский край", "Краснодар", "ЮЖНЫЙ"),
    ("г. Челябинск", "Челябинская область", "Челябинск", "УРАЛЬСКИЙ"),
    ("г. Самара", "Самарская область", "Самара", "ПОВОЛЖСКИЙ"),
)
_REGION_TO_RC = {region: (city, rc) for _, region, city, rc in _CITIES}
_REGION_TO_RC.update({
    "Челябинская область": ("Магнитогорск", "УРАЛЬСКИЙ"),
    "Пермский край": ("Пермь", "УРАЛЬСКИЙ"),
    "Белгородская область": ("Белгород", "ЦЕНТРАЛЬНЫЙ"),
})


def _flag(value: bool) -> str:
    return "1.0" if value else "0.0"


def _company_row(uid: str, okved: str, name: str, revenue_rub: float, city: str, region: str, *,
                 group: str, workers: str, client_type: str, fts: str = "", loan: int = 0,
                 leasing: int = 0, rng: random.Random) -> dict[str, Any]:
    return {
        "SYNTHETIC_U": uid,
        "ORG_TYPE": "КОММ_ОРГ",
        "OKFS_NAME": rng.choice(("ЧАСТНАЯ СОБСТВЕННОСТЬ",) * 8 + ("СОВМЕСТНАЯ ЧАСТНАЯ И ИНОСТРАННАЯ СОБСТВЕННОСТЬ",)),
        "COMPANY_AGE_YEARS_GRADATION": rng.choice(("8-15", "16-25", "4-7", "0-3")),
        "ECO_MARK_LABEL": rng.choice(("Оптимизация", "Оптимизация", "Бизнес в новой реальности", "Стремительный рост")),
        "MAINOKVED_CODE": okved,
        "MAINOKVED_NAME": name,
        "NEGATIVEMARK": _flag(rng.random() < 0.1),
        "AMOUNT_WORKERS_FNS_GRADATION": workers,
        "AMOUNT_WORKERS_SPARK_GRADATION": "",
        "CL_TYPE": client_type,
        "CL_PRODUCT_RKO_MARK": _flag(client_type != "PROSPECT"),
        "CL_PRODUCT_GAR_MARK": _flag(client_type != "PROSPECT" and rng.random() < 0.3),
        "CL_PRODUCT_LOA_MARK": _flag(loan),
        "CL_PRODUCT_LEA_MARK": _flag(leasing),
        "CL_PRODUCT_DEP_MARK": _flag(client_type != "PROSPECT" and rng.random() < 0.5),
        "CL_PRODUCT_ZPP_MARK": _flag(client_type != "PROSPECT" and rng.random() < 0.6),
        "FTS_FLAG": _flag(bool(fts)),
        "FTS_COUNTRY_NAME": fts,
        "FACTORING_MARK": _flag(rng.random() < 0.05),
        "REVENUE_COMPANY": f"{round(revenue_rub, -3):.1f}",
        "GROUP_ID": group,
        "CITY": city,
        "REGION": region,
    }


def generate_demo(out_dir: str | Path = DEMO_DIR, seed: int = SEED) -> dict[str, Any]:
    """Записать три CSV демо-набора в ``out_dir``; вернуть счётчики."""
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    companies: list[dict[str, Any]] = []
    large_ids: dict[str, str] = {}
    for i, (okved, name, bln, city, region, fts, loan, leasing) in enumerate(_LARGE, start=1):
        uid = f"U_DEMO_{i:03d}"
        large_ids[okved] = uid
        companies.append(_company_row(
            uid, okved, name, bln * 1e9 * rng.uniform(0.98, 1.02), city, region,
            group=f"group_demo_{i:02d}", workers="Более 251",
            client_type=rng.choice(("CLIENT", "CLOSE_CLIENT")), fts=fts, loan=loan, leasing=leasing, rng=rng,
        ))

    # По 2–4 контрагента каждого вида.
    by_okved: dict[str, list[str]] = {}
    n = len(companies)
    for okved, name, (lo, hi) in _COUNTERPARTY_KINDS:
        for _ in range(rng.randint(2, 4)):
            n += 1
            uid = f"U_DEMO_{n:03d}"
            city, region, _, _ = rng.choice(_CITIES)
            revenue = rng.uniform(lo, hi) * 1e6
            workers = "16-100" if revenue < 1e9 else rng.choice(("101-250", "Более 251"))
            companies.append(_company_row(
                uid, okved, name, revenue, city, region, group=f"group_demo_{n:02d}", workers=workers,
                client_type=rng.choice(("CLIENT", "PROSPECT", "PROSPECT")),
                fts=rng.choice(("",) * 6 + ("КИТАЙ", "ТУРЦИЯ", "КАЗАХСТАН")),
                loan=int(rng.random() < 0.3), leasing=int(rng.random() < 0.2), rng=rng,
            ))
            by_okved.setdefault(okved, []).append(uid)
    # Крупные компании тоже бывают контрагентами друг друга (сталь → стройка и т.п.).
    for okved, uid in large_ids.items():
        by_okved.setdefault(okved, []).insert(0, uid)

    # Группы: крупная компания и 1–2 её поставщика под одним GROUP_ID.
    rows_by_id = {row["SYNTHETIC_U"]: row for row in companies}
    for okved, uid in large_ids.items():
        suppliers = [s for kind in _SUPPLIERS[okved] for s in by_okved.get(kind, []) if s not in large_ids.values()]
        for member in rng.sample(suppliers, k=min(len(suppliers), rng.randint(1, 2))):
            rows_by_id[member]["GROUP_ID"] = rows_by_id[uid]["GROUP_ID"]

    # Платежи: суммы — доля выручки плательщика.
    payments: list[tuple[str, str, float, int]] = []
    revenue = {row["SYNTHETIC_U"]: float(row["REVENUE_COMPANY"]) for row in companies}
    for okved, uid in large_ids.items():
        for kind in _SUPPLIERS[okved]:
            for supplier in by_okved.get(kind, [])[:3]:
                if supplier == uid:
                    continue
                amount = revenue[uid] * rng.uniform(0.004, 0.05)
                payments.append((supplier, uid, amount, rng.randint(12, 900)))
        for kind in _BUYERS[okved]:
            for buyer in by_okved.get(kind, [])[:2]:
                if buyer == uid:
                    continue
                amount = min(revenue[buyer] * rng.uniform(0.02, 0.15), revenue[uid] * 0.08)
                payments.append((uid, buyer, amount, rng.randint(6, 400)))
    # Особенности настоящих данных: дубль пары (сливается при сборке), самоплатёж
    # (исключается) и контрагент вне справочника (не идентифицирован).
    first = payments[0]
    payments.append((first[0], first[1], first[2] * 0.1, 3))
    payments.append((large_ids["46.71"], large_ids["46.71"], 5_000_000.0, 1))
    payments.append(("U_DEMO_UNKNOWN", large_ids["24.10"], revenue[large_ids["24.10"]] * 0.01, 40))

    # Связанные физлица: у части крупных общий человек с поставщиком или с другой крупной.
    links: list[tuple[str, str, str, str]] = []
    person = 0
    ids = [row["SYNTHETIC_U"] for row in companies]
    for okved, uid in large_ids.items():
        city, rc = _REGION_TO_RC.get(rows_by_id[uid]["REGION"], ("Москва", "МОСКОВСКИЙ"))
        for _ in range(rng.randint(1, 3)):
            person += 1
            fid = f"F_DEMO_{person:03d}"
            links.append((fid, city, rc, uid))
            if rng.random() < 0.6:
                links.append((fid, city, rc, rng.choice([i for i in ids if i != uid])))
    for _ in range(25):
        person += 1
        city, _, cname, rc = rng.choice(_CITIES)
        links.append((f"F_DEMO_{person:03d}", cname, rc, rng.choice(ids[len(_LARGE):])))

    with (out / COMPANIES_CSV).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COMPANY_HEADER, delimiter=";", lineterminator="\n")
        writer.writeheader()
        writer.writerows(companies)
    with (out / PAYMENTS_CSV).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", lineterminator="\n")
        writer.writerow(("BENEFICIARY", "PAYER", "PAY_AMT", "PAY_CNT"))
        writer.writerows((b, p, f"{round(a, -3):.1f}", c) for b, p, a, c in payments)
    with (out / PERSON_LINKS_CSV).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", lineterminator="\n")
        writer.writerow(("SYNTHETIC_F", "CITY_NAME", "RC_NAME", "SYNTHETIC_U"))
        writer.writerows(links)

    return {
        "out_dir": str(out),
        "companies": len(companies),
        "large_companies": len(_LARGE),
        "payment_rows": len(payments),
        "person_links": len(links),
    }
