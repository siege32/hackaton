"""Справочник разделов ОКВЭД 2 (ОК 029-2014).

Код вида деятельности вида ``46.51`` -> класс ``46`` -> раздел ``G``.
Используется для человекочитаемой отрасли в профиле и для группировки
контрагентов по отраслям при расчёте экспозиции.
"""

from __future__ import annotations

# (первый класс, последний класс, буква раздела, название раздела)
_SECTIONS: tuple[tuple[int, int, str, str], ...] = (
    (1, 3, "A", "Сельское, лесное хозяйство, охота, рыболовство и рыбоводство"),
    (5, 9, "B", "Добыча полезных ископаемых"),
    (10, 33, "C", "Обрабатывающие производства"),
    (35, 35, "D", "Обеспечение электрической энергией, газом и паром; кондиционирование воздуха"),
    (36, 39, "E", "Водоснабжение; водоотведение, организация сбора и утилизации отходов"),
    (41, 43, "F", "Строительство"),
    (45, 47, "G", "Торговля оптовая и розничная; ремонт автотранспортных средств и мотоциклов"),
    (49, 53, "H", "Транспортировка и хранение"),
    (55, 56, "I", "Деятельность гостиниц и предприятий общественного питания"),
    (58, 63, "J", "Деятельность в области информации и связи"),
    (64, 66, "K", "Деятельность финансовая и страховая"),
    (68, 68, "L", "Деятельность по операциям с недвижимым имуществом"),
    (69, 75, "M", "Деятельность профессиональная, научная и техническая"),
    (77, 82, "N", "Деятельность административная и сопутствующие дополнительные услуги"),
    (84, 84, "O", "Государственное управление и обеспечение военной безопасности; социальное обеспечение"),
    (85, 85, "P", "Образование"),
    (86, 88, "Q", "Деятельность в области здравоохранения и социальных услуг"),
    (90, 93, "R", "Деятельность в области культуры, спорта, организации досуга и развлечений"),
    (94, 96, "S", "Предоставление прочих видов услуг"),
    (97, 98, "T", "Деятельность домашних хозяйств как работодателей"),
    (99, 99, "U", "Деятельность экстерриториальных организаций и органов"),
)

SECTION_NAMES: dict[str, str] = {letter: name for _, _, letter, name in _SECTIONS}


def normalize_code(code: str | None) -> str | None:
    """``' 1.11 '`` -> ``'01.11'``; мусор -> ``None``.

    Дописывать нули справа нельзя: ОКВЭД иерархичен по цифрам
    (``46`` -> ``46.7`` -> ``46.71``), и ``46.7`` — не то же, что ``46.70``.
    """
    if code is None:
        return None
    parts = [p for p in str(code).strip().split(".") if p != ""]
    if not parts or not all(p.isdigit() for p in parts):
        return None
    parts[0] = parts[0].zfill(2)
    return ".".join(parts)


def division(code: str | None) -> str | None:
    """Класс ОКВЭД (две цифры): ``'46.51'`` -> ``'46'``."""
    normalized = normalize_code(code)
    return normalized.split(".")[0] if normalized else None


def section(code: str | None) -> str | None:
    """Буква раздела ОКВЭД: ``'46.51'`` -> ``'G'``."""
    div = division(code)
    if div is None:
        return None
    number = int(div)
    for first, last, letter, _ in _SECTIONS:
        if first <= number <= last:
            return letter
    return None


def section_name(code: str | None) -> str | None:
    letter = section(code)
    return SECTION_NAMES.get(letter) if letter else None


def matches(code: str | None, prefixes: list[str] | tuple[str, ...]) -> bool:
    """Подходит ли код под фильтр события.

    Фильтр — буквы разделов (``'B'``) и/или префиксы кодов (``'46.71'``, ``'19'``).
    """
    normalized = normalize_code(code)
    if not normalized:
        return False
    letter = section(normalized)
    for raw in prefixes:
        prefix = str(raw).strip()
        if not prefix:
            continue
        if prefix.isalpha():
            if letter == prefix.upper():
                return True
            continue
        norm_prefix = normalize_code(prefix)
        # Цифровой префикс: '46.7' покрывает '46.71' и '46.73.3', но '46' не покрывает '4'.
        if norm_prefix and _digits(normalized).startswith(_digits(norm_prefix)):
            return True
    return False


def _digits(code: str) -> str:
    return code.replace(".", "")
