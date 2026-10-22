"""Дубли и кластеры событий (шаг 3 ТЗ).

Уровни:
1. Тот же нормализованный URL — та же публикация (обновление, а не новая новость).
2. Тот же хеш очищенного текста — точный дубль (перепечатка).
3. Похожие заголовок и начало текста в окне ``CLUSTER_WINDOW_HOURS`` — одно событие
   в разных СМИ: публикации НЕ сливаются, а получают общий ``cluster_id``.

Публикации одного события не взаимозаменяемы: поздняя может содержать новые
цифры или опровержение. Поэтому для каждой считается, что она добавляет к
уже известным публикациям кластера (``novelty``): новые предложения и числа.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..reports.index import tokenize

CLUSTER_WINDOW_HOURS = 72
#: Порог похожести (Жаккар по основам слов заголовка+лида) для одного события.
CLUSTER_THRESHOLD = 0.35
#: Доля новых предложений, начиная с которой публикация считается несущей новые факты.
NOVELTY_THRESHOLD = 0.3
_SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=[А-ЯЁA-Z«\"0-9])")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?\s?%?")
_DENIAL = re.compile(r"опроверг|не соответству\w* действительност|фейк|недостоверн", re.I)


def signature(title: str, lead: str) -> set[str]:
    """Набор основ слов заголовка и начала текста — «отпечаток» события."""
    return set(tokenize(f"{title} {lead[:400]}"))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


@dataclass
class ClusterCandidate:
    news_id: str
    cluster_id: str
    published_at: str | None
    signature: set[str]


#: Ослабленный порог, если у публикаций есть общее конкретное число (70 БПЛА, ставка 14%).
#: Замер на 187 реальных новостях: разные СМИ по-разному называют одно событие
#: («ЦБ сохранил» / «Банк России оставил»), но число совпадает.
NUMBER_THRESHOLD = 0.22
NUMBER_OVERLAP = 0.6


def _numbers(sig: set[str]) -> set[str]:
    return {t for t in sig if t[0].isdigit() and len(t.replace(",", "").replace(".", "")) >= 2}


def similarity(a: set[str], b: set[str]) -> float:
    """Похожесть событий: Жаккар, а при общем числе — не ниже порога кластера, если перекрытие высокое."""
    score = jaccard(a, b)
    if score >= CLUSTER_THRESHOLD or score < NUMBER_THRESHOLD or not (_numbers(a) & _numbers(b)):
        return score
    overlap = len(a & b) / min(len(a), len(b))
    return max(score, CLUSTER_THRESHOLD) if overlap >= NUMBER_OVERLAP else score


def find_cluster(
    title: str,
    lead: str,
    published_at: str | None,
    candidates: list[ClusterCandidate],
) -> tuple[str | None, float]:
    """Кластер, к которому относится публикация, и мера похожести (или (None, лучшая похожесть))."""
    sig = signature(title, lead)
    moment = _parse(published_at)
    best_id, best = None, 0.0
    for cand in candidates:
        other = _parse(cand.published_at)
        if moment and other and abs(moment - other) > timedelta(hours=CLUSTER_WINDOW_HOURS):
            continue
        score = similarity(sig, cand.signature)
        if score > best:
            best_id, best = cand.cluster_id, score
    return (best_id if best >= CLUSTER_THRESHOLD else None), round(best, 4)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text.replace("\n\n", " ")) if len(s.strip()) > 20]


@dataclass
class Novelty:
    """Что публикация добавляет к уже известным текстам своего кластера."""

    new_sentence_share: float
    new_sentences: list[str] = field(default_factory=list)
    new_numbers: list[str] = field(default_factory=list)
    denial: bool = False

    @property
    def adds_facts(self) -> bool:
        return self.new_sentence_share >= NOVELTY_THRESHOLD or bool(self.new_numbers) or self.denial

    def to_dict(self) -> dict:
        return {
            "new_sentence_share": self.new_sentence_share,
            "new_sentences": self.new_sentences[:5],
            "new_numbers": self.new_numbers[:20],
            "denial": self.denial,
            "adds_facts": self.adds_facts,
        }


def novelty(text: str, known_texts: list[str]) -> Novelty:
    own = sentences(text)
    known_sigs = [set(tokenize(s)) for t in known_texts for s in sentences(t)]
    new = [s for s in own if all(jaccard(set(tokenize(s)), k) < 0.6 for k in known_sigs)]
    known_numbers = {n.strip() for t in known_texts for n in _NUMBER.findall(t)}
    new_numbers = sorted({n.strip() for n in _NUMBER.findall(text)} - known_numbers)
    share = round(len(new) / len(own), 4) if own else 0.0
    return Novelty(share, new, new_numbers, bool(_DENIAL.search(text)) and not any(_DENIAL.search(t) for t in known_texts))
