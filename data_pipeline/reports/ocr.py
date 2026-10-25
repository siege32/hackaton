"""Распознавание PDF-отчётов Альфа-Банка разрешёнными LLM.

PDF кейса — сканы без текстового слоя. Каждая страница рендерится в PNG и
распознаётся двумя независимыми моделями из разрешённого списка:

- основная (``PRIMARY_MODEL``) — её текст идёт в индекс;
- проверочная (``VERIFIER_MODEL``) — только для сверки.

Тексты сравниваются по словам: доля совпадения (``agreement``) и список
расхождений сохраняются в JSON страницы, по ним выставляется ``confidence``.
Совпадение двух разных моделей — сильный сигнал, что слово прочитано верно;
расхождения показывают, где текст стоит перепроверить.

Почему так (замеры на стр. 1 отчёта по ключевой ставке, 20 контрольных фраз):
- qwen3-vl-30b — систематически пропускает слова и целые фразы даже в узких
  полосах страницы (например, «более продолжительным [выбытием]»);
- gemma-4 — дёшево, но перефразирует («которую брал Минфин» -> «который сделал ЦБ РФ»);
- glm-5.3-flash и qwen3.6-35b — 20/20, совпадение между ними 99,45 %.

Результат кэшируется по страницам: повторный запуск не тратит токены.
"""

from __future__ import annotations

import base64
import difflib
import json
import logging
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .catalog import DOCUMENTS, ReportDocument, document_by_id

logger = logging.getLogger("data_pipeline.reports.ocr")

PRIMARY_MODEL = "z-ai/glm-5.3-flash"
VERIFIER_MODEL = "qwen/qwen3.6-35b-a3b"
PROMPT_VERSION = "bank-report-ocr.v1"
RENDER_DPI = 200
PAGE_SCHEMA = "bank_report_page.v1"

OCR_PROMPT = (
    "Перепиши дословно, буква в букву, весь текст с изображения страницы аналитического отчёта, "
    "сохраняя порядок и абзацы. Ничего не пропускай, не сокращай и не перефразируй; не исправляй "
    "опечатки оригинала. Жирные вводные фразы абзацев выдели **жирным**. Таблицы — Markdown-таблицами "
    "со всеми числами. Для графиков и диаграмм: строка '[График] ' + название, подписи осей, легенда "
    "и все явно подписанные значения. Сноски, источники и дисклеймер тоже перепиши. "
    "Выведи только текст страницы, без пояснений."
)

#: Пороги доли совпадения слов между моделями.
HIGH_AGREEMENT = 0.98
MEDIUM_AGREEMENT = 0.93


@dataclass
class PageResult:
    document_id: str
    page: int
    status: str  # ok | cached | error
    cost: float = 0.0
    agreement: float | None = None
    confidence: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Сравнение текстов
# ---------------------------------------------------------------------------
_MARKUP = re.compile(r"[#*|`_>]+")
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "«": '"', "»": '"', "“": '"', "”": '"', "ё": "е", "Ё": "Е"})


#: Строки свободного описания графика: модели законно описывают оси и точки
#: по-разному (а проверочная модель может «оценивать» координаты на глаз),
#: поэтому в сверку идут только текст, таблицы, заголовки рисунков и источники.
_CHART_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\[граф|ось\b|оси\b|легенда|подписи|подписанные|точки|значения|данные:|\(около|около\b)",
    re.IGNORECASE,
)


def strip_chart_descriptions(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not _CHART_LINE.match(line))


def comparable_words(text: str) -> list[str]:
    """Слова без описаний графиков, Markdown-разметки и типографских вариантов (тире, кавычки, ё)."""
    text = strip_chart_descriptions(unicodedata.normalize("NFC", text)).translate(_DASHES)
    text = _MARKUP.sub(" ", text)
    return [w for w in text.split() if w.strip("-:")]


def compare_transcriptions(primary: str, verifier: str, *, context: int = 3) -> tuple[float, list[dict[str, str]]]:
    """Доля слов основного текста, подтверждённых проверочной моделью, и список расхождений.

    Метрика асимметрична намеренно: в индекс идёт только основной текст, поэтому
    важно, подтверждено ли каждое его слово. Лишний текст проверочной модели
    (например, «оценки» точек графика на глаз) виден в расхождениях, но не
    снижает уверенность в основном тексте.
    """
    a, b = comparable_words(primary), comparable_words(verifier)
    if not a:
        return (1.0 if not b else 0.0), []
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    confirmed = sum(block.size for block in matcher.get_matching_blocks())
    diffs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        diffs.append({
            "primary": " ".join(a[i1:i2]),
            "verifier": " ".join(b[j1:j2]),
            "context": " ".join(a[max(0, i1 - context):i2 + context]),
        })
    return round(confirmed / len(a), 4), diffs


def confidence_level(agreement: float | None) -> str:
    if agreement is None:
        return "unverified"
    if agreement >= HIGH_AGREEMENT:
        return "high"
    if agreement >= MEDIUM_AGREEMENT:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
Transcriber = Callable[[str, bytes], tuple[str, float]]


class TranscriptionError(RuntimeError):
    """Неудачное распознавание; ``cost`` — сколько всё равно потрачено на вызов."""

    def __init__(self, message: str, cost: float = 0.0) -> None:
        super().__init__(message)
        self.cost = cost


#: Обе модели по умолчанию «рассуждают» перед ответом: у GLM на плотной странице
#: это ~5 800 скрытых токенов из 7 100, ответ упирается в лимит и стоит в 4 раза
#: дороже. Замер на эталонной странице: без рассуждений те же 20/20 контрольных
#: фраз и 99,86 % совпадения с «думающим» вариантом.
REASONING = {"reasoning": {"enabled": False}}


def openai_transcriber(api_key: str, base_url: str, *, timeout: float = 240, max_tokens: int = 16000) -> Transcriber:
    """Функция (model, png) -> (текст, стоимость) поверх OpenAI-совместимого API кейса."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=2)

    def transcribe(model: str, png: bytes) -> tuple[str, float]:
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
            extra_body=REASONING,
        )
        cost = float(getattr(response.usage, "cost", 0) or 0)
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        if choice.finish_reason == "length":
            raise TranscriptionError(f"{model}: ответ обрезан по max_tokens", cost)
        if not text:
            raise TranscriptionError(f"{model}: пустой ответ", cost)
        return text, cost

    return transcribe


def transcriber_from_env() -> Transcriber:
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("LLM_API_KEY не задан (см. .env)")
    base = os.environ.get("LLM_BASE_URL", "https://changellenge.up.railway.app/v1").strip()
    return openai_transcriber(key, base)


# ---------------------------------------------------------------------------
# Страницы
# ---------------------------------------------------------------------------
def page_path(ocr_dir: Path, document_id: str, page: int) -> Path:
    return ocr_dir / document_id / f"page_{page:03d}.json"


def render_page(pdf_path: Path, page_index: int, dpi: int = RENDER_DPI) -> bytes:
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        return doc[page_index].get_pixmap(dpi=dpi).tobytes("png")


def page_count(pdf_path: Path) -> int:
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        return doc.page_count


def _is_current(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("prompt_version") == PROMPT_VERSION and bool(data.get("primary", {}).get("text"))


def ocr_page(
    doc: ReportDocument,
    pdf_path: Path,
    page: int,
    pages_total: int,
    ocr_dir: Path,
    transcribe: Transcriber,
    *,
    force: bool = False,
) -> PageResult:
    """Распознать одну страницу (нумерация с 1) и сохранить JSON."""
    out = page_path(ocr_dir, doc.document_id, page)
    if not force and _is_current(out):
        return PageResult(doc.document_id, page, "cached")

    png = render_page(pdf_path, page - 1)
    started = time.monotonic()
    with ThreadPoolExecutor(2) as pool:
        primary_f = pool.submit(transcribe, PRIMARY_MODEL, png)
        verifier_f = pool.submit(transcribe, VERIFIER_MODEL, png)
        try:
            verifier_text, verifier_cost = verifier_f.result()
            verifier_error = None
        except Exception as exc:
            verifier_text, verifier_cost, verifier_error = None, getattr(exc, "cost", 0.0), str(exc)
        try:
            primary_text, primary_cost = primary_f.result()
        except Exception as exc:  # без основного текста страницу не сохраняем, но траты учитываем
            spent = getattr(exc, "cost", 0.0) + verifier_cost
            return PageResult(doc.document_id, page, "error", cost=spent, error=f"{PRIMARY_MODEL}: {exc}")

    if verifier_text is not None:
        agreement, disagreements = compare_transcriptions(primary_text, verifier_text)
    else:
        agreement, disagreements = None, []
    confidence = confidence_level(agreement)

    record = {
        "schema": PAGE_SCHEMA,
        "document_id": doc.document_id,
        "file": doc.file,
        "title": doc.title,
        "report_date": doc.report_date,
        "page": page,
        "pages_total": pages_total,
        "text": primary_text,
        "primary": {"model": PRIMARY_MODEL, "text": primary_text, "cost": primary_cost},
        "verifier": {"model": VERIFIER_MODEL, "text": verifier_text, "cost": verifier_cost, "error": verifier_error},
        "agreement": agreement,
        "confidence": confidence,
        "disagreements": disagreements,
        "prompt_version": PROMPT_VERSION,
        "render_dpi": RENDER_DPI,
        "seconds": round(time.monotonic() - started, 1),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    return PageResult(doc.document_id, page, "ok", primary_cost + verifier_cost, agreement, confidence)


def _find_pdf(reports_dir: Path, file: str) -> Path | None:
    """Имя файла может отличаться нормализацией Unicode (кириллическая «К» и т.п.).

    PDF ищутся в ``reports_dir`` (по умолчанию ``data/raw/bank_reports``), а затем
    в родительской папке: часто их кладут прямо в ``data/raw/`` рядом с CSV.
    """
    target = unicodedata.normalize("NFC", file)
    for folder in (reports_dir, reports_dir.parent):
        direct = folder / file
        if direct.exists():
            return direct
        for candidate in folder.glob("*.pdf"):
            if unicodedata.normalize("NFC", candidate.name) == target:
                return candidate
    return None


def ocr_reports(
    reports_dir: Path,
    ocr_dir: Path,
    *,
    transcribe: Transcriber | None = None,
    document_ids: list[str] | None = None,
    max_pages: int | None = None,
    max_cost: float = 60.0,
    workers: int = 4,
    force: bool = False,
    on_page: Callable[[PageResult], None] | None = None,
) -> dict[str, Any]:
    """Распознать отчёты каталога. Останавливается, если потрачено больше ``max_cost``."""
    transcribe = transcribe or transcriber_from_env()
    jobs = []
    missing_files = []
    for doc in DOCUMENTS:
        if document_ids and doc.document_id not in document_ids:
            continue
        pdf = _find_pdf(reports_dir, doc.file)
        if pdf is None:
            missing_files.append(doc.file)
            continue
        total = page_count(pdf)
        pages = range(1, total + 1) if max_pages is None else range(1, min(total, max_pages) + 1)
        jobs += [(doc, pdf, p, total) for p in pages]

    results: list[PageResult] = []
    spent = 0.0
    with ThreadPoolExecutor(max(1, workers)) as pool:
        futures = []
        for job in jobs:
            futures.append(pool.submit(ocr_page, *job, ocr_dir, transcribe, force=force))
        for future in futures:
            result = future.result()
            results.append(result)
            spent += result.cost
            if on_page:
                on_page(result)
            if spent > max_cost:
                for f in futures:
                    f.cancel()
                logger.warning("Остановлено: потрачено %.2f > лимита %.2f", spent, max_cost)
                break

    return {
        "pages_total": len(jobs),
        "pages_processed": sum(r.status == "ok" for r in results),
        "pages_cached": sum(r.status == "cached" for r in results),
        "pages_failed": [{"document_id": r.document_id, "page": r.page, "error": r.error}
                         for r in results if r.status == "error"],
        "cost": round(spent, 4),
        "low_confidence_pages": [{"document_id": r.document_id, "page": r.page, "agreement": r.agreement}
                                 for r in results if r.confidence in ("low", "unverified")],
        "missing_files": missing_files,
    }


def rescore_pages(ocr_dir: Path) -> int:
    """Пересчитать agreement/confidence и метаданные каталога по сохранённым страницам (без LLM)."""
    updated = 0
    for path in sorted(Path(ocr_dir).glob("*/page_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        verifier_text = (record.get("verifier") or {}).get("text")
        if verifier_text is None:
            agreement, disagreements = None, []
        else:
            agreement, disagreements = compare_transcriptions(record["primary"]["text"], verifier_text)
        record.update(agreement=agreement, confidence=confidence_level(agreement), disagreements=disagreements)
        doc = document_by_id(record["document_id"])
        if doc:  # метаданные берём из каталога: даты уточняются после распознавания
            record.update(title=doc.title, report_date=doc.report_date)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        updated += 1
    return updated
