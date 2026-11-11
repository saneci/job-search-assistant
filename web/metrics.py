"""Расчёт метрик по дневным отчётам (data/report).

Модуль не зависит от FastAPI: на вход идут записи `vacancies` из файлов дня,
на выходе — готовые числа для шаблона. Одна запись — это вакансия и резюме:
разными резюме можно отвечать на одну вакансию, поэтому такие записи считаются
отдельно. При повторах запись переносится между файлами, поэтому перед
подсчётом выбирается самая свежая запись по `updated_at` (или названию и компании).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable, Mapping

STATUS_ORDER = ("skipped", "excluded", "queued", "error", "sent", "invite", "rejected", "offer")
STATUS_LABELS = {
    "skipped": "Не подходит",
    "excluded": "Исключено",
    "queued": "В очереди",
    "error": "Ошибки",
    # «Отправлено» — статус вакансии: отклик ушёл, ответа пока нет.
    # Все отклики вместе с приглашениями, отказами и офферами считаются
    # отдельно (responses).
    "sent": "Отправлено",
    "invite": "Приглашения",
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
    "invite": "var(--color-invite)",
    "rejected": "var(--color-rejected)",
    "offer": "var(--color-offer)",
    "unknown": "var(--color-base)",
}
# Статусы, которые означают отправленный отклик и его исход: ожидание ответа,
# приглашение, отказ и оффер.
RESPONSE_STATUSES = ("sent", "invite", "rejected", "offer")
# Финальные исходы: запись, дошедшая до оффера или отказа, показывается в своём
# исходе даже если по ней было приглашение.
FINAL_STATUSES = ("rejected", "offer")

# Каналы поиска: у записей старых отчётов поля нет — они идут как unknown.
CHANNEL_LABELS = {
    "hh": "hh.ru",
    "habr": "Habr Career",
    "external": "Сайт компании",
    "unknown": "Без канала",
}
CHANNEL_COLORS = {
    "hh": "var(--color-sent)",
    "habr": "var(--color-queued)",
    "external": "var(--color-skipped)",
    "unknown": "var(--color-base)",
}


def profile_value(value: Any) -> str:
    """Нормализованное имя резюме: регистр, «ё» и лишние пробелы не важны."""
    return " ".join(str(value or "").lower().replace("ё", "е").split())


def profile_key(record: Mapping[str, Any]) -> str:
    """Нормализованное имя резюме записи."""
    return profile_value(record.get("profile", ""))


def _invited_at(record: Mapping[str, Any]) -> str:
    """Дата приглашения из записи или пустая строка.

    `null` в JSON считается отсутствием даты: `str(None)` дал бы строку «None»,
    и запись без приглашения попала бы в счётчик.
    """
    return str(record.get("invited_at") or "").strip()


def effective_status(record: Mapping[str, Any]) -> str:
    """Статус записи для графиков с учётом факта приглашения.

    `invited_at` ставится один раз и не сбрасывается. Поэтому запись, у которой
    статус позже откатили в `sent` или `skipped`, всё равно считается дошедшей
    до приглашения. Оффер и отказ — финальные исходы, их не перекрываем.
    Статус `invite` без даты приглашения считается отправленным откликом: дата —
    единственный признак, что приглашение было на самом деле.
    """
    status = str(record.get("status", "") or "unknown")
    if status in FINAL_STATUSES:
        return status
    if _invited_at(record):
        return "invite"
    if status == "invite":
        return "sent"
    return status


def _vacancy_key(
    record: Mapping[str, Any], index: int, legacy_profiles: Mapping[str, str]
) -> tuple[str, ...]:
    """Ключ записи: вакансия и профиль. У Habr профиль теперь тоже хранится.

    Старые записи Habr шли без профиля: их профиль неизвестен, поэтому копия
    относится к профилю, которым вакансию обрабатывали последний раз
    (`legacy_profiles`). Так старая копия не висит отдельной строкой, а записи
    разных профилей одной вакансии остаются разными записями.
    """
    profile = profile_key(record)
    url = str(record.get("url", "")).strip()
    if url and not profile:
        profile = legacy_profiles.get(url, "")
    if url:
        return ("url", url) if not profile else ("url", url, "profile", profile)
    title = str(record.get("title", "")).strip()
    company = str(record.get("company", "")).strip()
    if title or company:
        return (
            ("title", title, company)
            if not profile
            else ("title", title, company, "profile", profile)
        )
    return "row", str(index)


def _legacy_profiles(records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Профиль для старых записей Habr без профиля: самый свежий известный.

    Habr хранит профиль в записи не с самого начала. Копия без профиля —
    это та же вакансия, которую последний раз обрабатывали профилем из
    `# PROFILE:`, поэтому берём профиль самой свежей записи с профилем.
    """
    latest: dict[str, tuple[tuple[float, str], str]] = {}
    for record in records:
        if str(record.get("channel", "")).strip() != "habr":
            continue
        url = str(record.get("url", "")).strip()
        profile = profile_key(record)
        if not url or not profile:
            continue
        moment = _updated_key(record)
        known = latest.get(url)
        if known is None or moment >= known[0]:
            latest[url] = (moment, profile)
    return {url: profile for url, (_, profile) in latest.items()}


def _updated_key(record: Mapping[str, Any]) -> tuple[float, str]:
    """Время последнего изменения для выбора самой свежей записи о вакансии."""
    for field in ("updated_at", "created_at"):
        value = str(record.get(field, "")).strip()
        if not value:
            continue
        try:
            return datetime.fromisoformat(value).timestamp(), value
        except ValueError:
            continue
    return 0.0, ""


def prefer_record(candidate: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Какая копия записи важнее: архивная терминальнее, иначе побеждает свежая.

    Архив закрывает вакансию целиком. Если одна копия в архиве, а другая
    осталась в очереди, показывается архивная: иначе закрытая вакансия снова
    выглядит рабочей.
    """
    if bool(candidate.get("archived")) != bool(current.get("archived")):
        return bool(candidate.get("archived"))
    return _updated_key(candidate) >= _updated_key(current)


def dedupe_vacancies(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Оставляет по одной — архивной или самой свежей — копии на каждую запись.

    Запись — это вакансия и профиль: у Habr профиль хранится в записи, как
    и у hh. Копии без профиля относятся к профилю, которым вакансию
    обрабатывали последний раз.
    """
    items = [dict(record) for record in records]
    legacy_profiles = _legacy_profiles(items)
    best: dict[tuple[str, ...], dict[str, Any]] = {}
    for index, item in enumerate(items):
        key = _vacancy_key(item, index, legacy_profiles)
        previous = best.get(key)
        if previous is None or prefer_record(item, previous):
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


def _rate_text(value: float | None, *, has_data: bool = True) -> str:
    """Проценты для карточек конверсий: прочерк только в пустом отчёте.

    Пока в отчётах есть вакансии, все четыре карточки показывают проценты:
    при нулевом знаменателе это «0.0%», а не прочерк. Иначе в одной строке
    соседствуют «0.0%» и «—», и непонятно, где ошибка данных, а где ноль.
    """
    if value is None:
        return "0.0%" if has_data else "—"
    return f"{value:.1f}%"


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
    """CSS conic-gradient по реальным долям; пустой отчёт — нейтральный круг.

    У каждого стопа одна позиция: запись «цвет начало%» понимают все браузеры.
    Двойные позиции «цвет начало% конец%» поддерживают не все, и тогда браузер
    отбрасывает весь градиент — круг остаётся без заливки.
    """
    if total <= 0:
        return "var(--surface-2)"
    stops: list[str] = []
    cumulative = 0.0
    for count, color in segments:
        if count <= 0:
            # Пустой сегмент не рисуется, иначе появляются стопы нулевой ширины.
            continue
        start = cumulative
        cumulative += count * 100 / total
        stops.append(f"{color} {start:.2f}%")
        stops.append(f"{color} {cumulative:.2f}%")
    if not stops:
        return "var(--surface-2)"
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
    Факт приглашения берётся из `invited_at`: запись с датой приглашения
    считается приглашённой, даже если статус позже откатили в `sent` или
    `skipped`. Оффер и отказ перекрывают этот факт как финальные исходы.
    """
    rows = dedupe_vacancies(vacancies)
    total = len(rows)
    channel_counts = Counter(str(row.get("channel", "") or "unknown") for row in rows)
    channels = [
        _item(name, CHANNEL_LABELS[name], channel_counts.get(name, 0), total, CHANNEL_COLORS[name])
        for name in ("hh", "habr", "external", "unknown")
        if channel_counts.get(name)
    ]
    raw_counts = Counter(effective_status(row) for row in rows)
    counts = {status: raw_counts.get(status, 0) for status in STATUS_ORDER}
    unknown = total - sum(counts.values())
    if unknown:
        counts["unknown"] = unknown

    sent = counts["sent"]
    invites = counts["invite"]
    rejected = counts["rejected"]
    offers = counts["offer"]
    responses = sum(counts[status] for status in RESPONSE_STATUSES)
    others = total - responses
    # «Дошло до приглашения»: факт хранится в `invited_at`, поэтому счётчик не
    # уменьшается, когда запись переходит из `invite` в `offer` или `rejected`.
    invited_total = sum(1 for row in rows if _invited_at(row))

    pie_segments = [
        (responses - offers - invites, "var(--color-sent)"),
        (invites, "var(--color-invite)"),
        (offers, "var(--color-offer)"),
        (others, "var(--color-base)"),
    ]
    pie_legend = [
        _item("responses", "Отклики", responses - offers - invites, total, "var(--color-sent)"),
        _item("invite", "Приглашения", invites, total, "var(--color-invite)"),
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
        _item("invite", "Приглашения", invites, responses, "var(--color-invite)"),
        _item("rejected", "Отказы", rejected, responses, "var(--color-rejected)"),
        _item("offer", "Офферы", offers, responses, "var(--color-offer)"),
    ]

    rates = {
        "response": _rate(responses, total),
        "invite": _rate(invites, responses),
        "offer": _rate(offers, responses),
        "rejected": _rate(rejected, responses),
        "ignore": _rate(sent, responses),
    }
    rate_text = {
        key: _rate_text(value, has_data=total > 0) for key, value in rates.items()
    }
    updated_text = _format_updated(updated_at)
    words = {
        "vacancies": _plural(total, "вакансия", "вакансии", "вакансий"),
        # Для оборота «из N …» нужен родительный падеж: «из 1 вакансии», «из 553 вакансий».
        "vacancies_genitive": _plural(total, "вакансии", "вакансий", "вакансий"),
        "responses": _plural(responses, "отклик", "отклика", "откликов"),
        "offers": _plural(offers, "оффер", "оффера", "офферов"),
        "invites": _plural(invites, "приглашение", "приглашения", "приглашений"),
        "invited_total": _plural(
            invited_total, "приглашение", "приглашения", "приглашений"
        ),
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
        "invites": invites,
        "invited_total": invited_total,
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
        "channels": channels,
        "empty": total == 0,
        "has_responses": responses > 0,
    }
