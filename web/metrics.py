"""Расчёт метрик по дневным отчётам (data/report).

Модуль не зависит от FastAPI: на вход идут записи `vacancies` из файлов дня,
на выходе — готовые числа для шаблона. Одна вакансия учитывается один раз:
при повторах запись переносится между файлами, поэтому перед подсчётом
выбирается самая свежая запись по ссылке (или названию и компании).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable, Mapping

STATUS_ORDER = ("skipped", "excluded", "queued", "error", "sent", "rejected", "offer")
STATUS_LABELS = {
    "skipped": "Не подходит",
    "excluded": "Исключено",
    "queued": "В очереди",
    "error": "Ошибки",
    # «Отправлено» — статус вакансии: отклик ушёл, ответа пока нет.
    # Все отклики вместе с отказами и офферами считаются отдельно (responses).
    "sent": "Отправлено",
    "rejected": "Отказы",
    "offer": "Офферы",
    "unknown": "Неизвестно",
}
STATUS_COLORS = {
    "skipped": "var(--color-skipped)",
    "excluded": "var(--color-excluded)",
    "queued": "var(--color-queued)",
    "error": "var(--color-error)",
    "sent": "var(--color-sent)",
    "rejected": "var(--color-rejected)",
    "offer": "var(--color-offer)",
    "unknown": "var(--color-base)",
}
# Статусы, которые означают отправленный отклик: ожидание ответа, отказ и оффер.
RESPONSE_STATUSES = ("sent", "rejected", "offer")


def _vacancy_key(record: Mapping[str, Any], index: int) -> tuple[str, ...]:
    """Ключ вакансии: ссылка, иначе название и компания."""
    url = str(record.get("url", "")).strip()
    if url:
        return ("url", url)
    title = str(record.get("title", "")).strip()
    company = str(record.get("company", "")).strip()
    if title or company:
        return ("title", title, company)
    return ("row", str(index))


def _processed_key(record: Mapping[str, Any]) -> tuple[float, str]:
    """Время обработки для выбора самой свежей записи о вакансии."""
    value = str(record.get("processed_at", "")).strip()
    if not value:
        return 0.0, ""
    try:
        return datetime.fromisoformat(value).timestamp(), value
    except ValueError:
        return 0.0, value


def dedupe_vacancies(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Оставляет по одной — самой свежей — записи на каждую вакансию."""
    best: dict[tuple[str, ...], dict[str, Any]] = {}
    for index, record in enumerate(records):
        item = dict(record)
        key = _vacancy_key(item, index)
        previous = best.get(key)
        if previous is None or _processed_key(item) >= _processed_key(previous):
            best[key] = item
    return list(best.values())


def _rate(part: int, whole: int) -> float | None:
    """Процент от целого; None, когда делить не на что."""
    if whole <= 0:
        return None
    return round(part * 100 / whole, 1)


def _percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round(part * 100 / whole, 2)


def _rate_text(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русская форма слова для числа: 1 вакансия, 2 вакансии, 5 вакансий."""
    tail = abs(count) % 100
    if 10 <= tail <= 20:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _format_updated(value: str) -> str:
    """Дата и время в том же виде, что колонка «Дата» на странице отчёта."""
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _pie_css(segments: list[tuple[int, str]], total: int) -> str:
    """CSS conic-gradient по реальным долям; пустой отчёт — нейтральный круг."""
    if total <= 0:
        return "var(--surface-2)"
    stops: list[str] = []
    cumulative = 0.0
    for count, color in segments:
        start = cumulative
        cumulative += count * 100 / total
        stops.append(f"{color} {start:.2f}% {cumulative:.2f}%")
    return "conic-gradient(" + ", ".join(stops) + ")"


def _item(key: str, label: str, count: int, whole: int, color: str) -> dict[str, Any]:
    percent = _percent(count, whole)
    return {
        "key": key,
        "label": label,
        "count": count,
        "percent": percent,
        "percent_text": f"{percent:.1f}%",
        "color": color,
    }


def build_metrics(
    vacancies: Iterable[Mapping[str, Any]],
    *,
    reports_count: int = 0,
    updated_at: str = "",
) -> dict[str, Any]:
    """Считает метрики по записям отчётов.

    Все статусы считаются как есть: `sent` — отправленные отклики (включая
    отклики, отправленные вручную), `rejected` и `offer` — их исход.
    """
    rows = dedupe_vacancies(vacancies)
    total = len(rows)
    raw_counts = Counter(str(row.get("status", "") or "unknown") for row in rows)
    counts = {status: raw_counts.get(status, 0) for status in STATUS_ORDER}
    unknown = total - sum(counts.values())
    if unknown:
        counts["unknown"] = unknown

    sent = counts["sent"]
    rejected = counts["rejected"]
    offers = counts["offer"]
    responses = sum(counts[status] for status in RESPONSE_STATUSES)
    others = total - responses

    pie_segments = [
        (responses - offers, "var(--color-sent)"),
        (offers, "var(--color-offer)"),
        (others, "var(--color-base)"),
    ]
    pie_legend = [
        _item("responses", "Отклики", responses - offers, total, "var(--color-sent)"),
        _item("offer", "Офферы", offers, total, "var(--color-offer)"),
        _item("others", "Остальные", others, total, "var(--color-base)"),
    ]

    distribution = [
        _item(status, STATUS_LABELS[status], counts[status], total, STATUS_COLORS[status])
        for status in STATUS_ORDER
    ]
    if unknown:
        distribution.append(
            _item("unknown", STATUS_LABELS["unknown"], unknown, total, STATUS_COLORS["unknown"])
        )

    fate = [
        _item("sent", "Без ответа", sent, responses, "var(--color-base)"),
        _item("rejected", "Отказы", rejected, responses, "var(--color-rejected)"),
        _item("offer", "Офферы", offers, responses, "var(--color-offer)"),
    ]

    rates = {
        "response": _rate(responses, total),
        "offer": _rate(offers, responses),
        "rejected": _rate(rejected, responses),
        "ignore": _rate(sent, responses),
    }
    rate_text = {key: _rate_text(value) for key, value in rates.items()}
    updated_text = _format_updated(updated_at)
    words = {
        "vacancies": _plural(total, "вакансия", "вакансии", "вакансий"),
        # Для оборота «из N …» нужен родительный падеж: «из 1 вакансии», «из 553 вакансий».
        "vacancies_genitive": _plural(total, "вакансии", "вакансий", "вакансий"),
        "responses": _plural(responses, "отклик", "отклика", "откликов"),
        "offers": _plural(offers, "оффер", "оффера", "офферов"),
        "rejected": _plural(rejected, "отказ", "отказа", "отказов"),
        "sent": _plural(sent, "отклик", "отклика", "откликов"),
    }

    return {
        "total": total,
        "reports_count": reports_count,
        "updated_at": updated_at,
        "updated_at_text": updated_text,
        "updated_text": (
            f"Обновлено: {updated_text} · Всего обработано {total} {words['vacancies']}"
        ),
        "summary_text": (
            f"В отчётах: {total} {words['vacancies']} · "
            f"{responses} {words['responses']} · {offers} {words['offers']}"
        ),
        "words": words,
        "counts": counts,
        "sent": sent,
        "rejected": rejected,
        "offers": offers,
        "responses": responses,
        "others": others,
        "rates": rates,
        "rate_text": rate_text,
        "pie_css": _pie_css(pie_segments, total),
        "pie_legend": pie_legend,
        "distribution": distribution,
        "fate": fate,
        "empty": total == 0,
        "has_responses": responses > 0,
    }
