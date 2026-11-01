"""Тесты расчёта метрик: дедупликация, все статусы, проценты и пустой отчёт."""

from __future__ import annotations

from typing import Any

import pytest

from web.metrics import build_metrics, dedupe_vacancies


def row(url: str, status: str, updated_at: str = "") -> dict[str, Any]:
    return {
        "url": f"https://hh.ru/vacancy/{url}",
        "title": f"Вакансия {url}",
        "company": "Компания",
        "status": status,
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def test_counts_all_statuses() -> None:
    """Все статусы из JSON считаются как есть; отклики = sent + rejected + offer."""
    rows = [
        row("1", "sent"),
        row("2", "sent"),
        row("3", "rejected"),
        row("4", "offer"),
        row("5", "skipped"),
        row("6", "excluded"),
        row("7", "queued"),
        row("8", "error"),
    ]

    metrics = build_metrics(
        rows, reports_count=2, updated_at="2026-10-24T12:00:00+03:00"
    )

    assert metrics["total"] == 8
    assert metrics["sent"] == 2
    assert metrics["rejected"] == 1
    assert metrics["offers"] == 1
    assert metrics["responses"] == 4
    assert metrics["others"] == 4
    assert metrics["rates"] == {
        "response": 50.0,
        "invite": 0.0,
        "offer": 25.0,
        "rejected": 25.0,
        "ignore": 50.0,
    }
    assert metrics["rate_text"]["response"] == "50.0%"
    assert metrics["summary_text"] == "В отчётах: 8 вакансий · 4 отклика · 1 оффер"
    # Дата — в том же виде, что колонка «Дата» на странице отчёта.
    assert metrics["updated_at_text"] == "2026-10-24 12:00:00"
    assert metrics["updated_text"].startswith("Обновлено: 2026-10-24 12:00:00")
    # Пирог: отклики без офферов и приглашений, приглашения, офферы, остальные.
    assert [item["count"] for item in metrics["pie_legend"]] == [3, 0, 1, 4]
    assert metrics["pie_css"].startswith("conic-gradient(")
    assert [item["count"] for item in metrics["fate"]] == [2, 0, 1, 1]
    # Подпись «N откликов из M вакансий» требует родительного падежа.
    assert metrics["words"]["vacancies_genitive"] == "вакансий"


def test_same_vacancy_with_different_profiles_counts_twice() -> None:
    """Одна вакансия с двумя резюме — две записи в метриках."""
    rows = [row("1", "sent"), row("1", "queued")]
    rows[0]["profile"] = "Java Developer"
    rows[1]["profile"] = "Team Lead"

    metrics = build_metrics(rows)

    assert metrics["total"] == 2
    assert metrics["counts"]["sent"] == 1
    assert metrics["counts"]["queued"] == 1


@pytest.mark.parametrize(
    ("total", "expected"),
    [(1, "вакансии"), (3, "вакансий"), (11, "вакансий"), (21, "вакансии")],
)
def test_vacancies_genitive_after_from(total: int, expected: str) -> None:
    rows = [row(str(index), "skipped") for index in range(total)]

    assert build_metrics(rows)["words"]["vacancies_genitive"] == expected


def test_sent_status_and_responses_are_labelled_differently() -> None:
    """Одно слово не должно означать два числа: статус и все отклики.

    «Отправлено» — статус без ответа, «Отклики» — все отправленные отклики,
    включая уже закрытые отказом или оффером.
    """
    rows = [
        row("1", "sent"),
        row("2", "sent"),
        row("3", "rejected"),
        row("4", "offer"),
    ]

    metrics = build_metrics(rows)

    labels = {item["key"]: item["label"] for item in metrics["distribution"]}
    assert labels["sent"] == "Отправлено"
    assert metrics["counts"]["sent"] == 2
    assert metrics["responses"] == 4
    assert [item["count"] for item in metrics["pie_legend"]] == [3, 0, 1, 0]
    assert metrics["pie_legend"][0]["label"] == "Отклики"


def test_invite_is_a_response_and_survives_status_change() -> None:
    """Приглашение — исход отклика; факт из `invited_at` остаётся после смены статуса."""
    rows = [
        row("1", "sent"),
        row("2", "invite"),
        row("3", "offer"),
    ]
    rows[2]["invited_at"] = "2026-10-25T10:00:00+03:00"

    metrics = build_metrics(rows)

    assert metrics["invites"] == 1
    assert metrics["responses"] == 3
    # Оффер пришёл после приглашения: текущий статус уже не invite, но факт виден.
    assert metrics["invited_total"] == 1
    assert metrics["rates"]["invite"] == 33.3
    labels = {item["key"]: item["label"] for item in metrics["pie_legend"]}
    assert labels["invite"] == "Приглашения"
    assert [item["count"] for item in metrics["pie_legend"]] == [1, 1, 1, 0]
    # «Судьба откликов»: без ответа, приглашения, отказы, офферы.
    assert [item["key"] for item in metrics["fate"]] == ["sent", "invite", "rejected", "offer"]


def test_offer_is_a_response() -> None:
    metrics = build_metrics([row("1", "offer")])

    assert metrics["responses"] == 1
    assert metrics["rates"]["offer"] == 100.0
    assert metrics["rates"]["ignore"] == 0.0


def test_dedupe_keeps_latest_record() -> None:
    """Повтор вакансии переносится между файлами: берём самую свежую запись."""
    rows = [
        row("1", "sent", "2026-10-23T10:00:00+03:00"),
        row("1", "rejected", "2026-10-24T11:00:00+03:00"),
    ]

    unique = dedupe_vacancies(rows)

    assert len(unique) == 1
    assert unique[0]["status"] == "rejected"


def test_dedupe_uses_updated_at() -> None:
    """Самая свежая запись определяется по updated_at, а не по старому полю."""
    rows = [
        {
            "url": "https://hh.ru/vacancy/1",
            "status": "sent",
            "created_at": "2026-09-30T10:00:00+03:00",
            "updated_at": "2026-09-30T10:00:00+03:00",
        },
        {
            "url": "https://hh.ru/vacancy/1",
            "status": "offer",
            "created_at": "2026-09-30T10:00:00+03:00",
            "updated_at": "2026-10-24T11:00:00+03:00",
        },
    ]

    unique = dedupe_vacancies(rows)

    assert len(unique) == 1
    assert unique[0]["status"] == "offer"


def test_empty_report_has_no_fake_percent() -> None:
    metrics = build_metrics([])

    assert metrics["empty"] is True
    assert metrics["total"] == 0
    assert metrics["rates"]["response"] is None
    assert metrics["rate_text"]["response"] == "—"
    assert metrics["pie_css"] == "var(--surface-2)"


def test_unknown_status_is_counted_separately() -> None:
    metrics = build_metrics([row("1", "mystery")])

    assert metrics["total"] == 1
    assert metrics["counts"]["unknown"] == 1
    assert metrics["distribution"][-1]["key"] == "unknown"
    assert metrics["others"] == 1


def test_pie_css_stops_are_portable() -> None:
    """Стопы градиента — по одной позиции: двойные понимают не все браузеры."""
    metrics = build_metrics(
        [row("1", "sent"), row("2", "offer"), row("3", "skipped")]
    )

    css = metrics["pie_css"]
    assert css.startswith("conic-gradient(")
    stops = css.removeprefix("conic-gradient(").removesuffix(")").split(", ")
    assert stops
    for stop in stops:
        # «цвет 0.00%» — одна позиция, «цвет 0.00% 9.34%» — две, несовместимые.
        assert stop.count("%") == 1


def test_zero_count_in_legend_is_a_number() -> None:
    """Ноль в легенде — число: график показывает реальный счётчик, а не прочерк."""
    metrics = build_metrics([row("1", "sent")])

    fate = {item["key"]: item for item in metrics["fate"]}
    assert fate["invite"]["count"] == 0
    assert fate["sent"]["count"] == 1
    # Прочерк остался только в карточках, где делить не на что.
    assert metrics["rate_text"]["invite"] == "0.0%"
    assert build_metrics([])["rate_text"]["invite"] == "—"


def test_invited_at_marks_record_as_invited_after_status_rollback() -> None:
    """Дата приглашения важнее откатанного статуса.

    `invited_at` ставится один раз и не сбрасывается: если статус вернули в
    `sent` или `skipped`, запись всё равно считается приглашённой. Оффер и
    отказ остаются финальными исходами.
    """
    rows = [
        row("1", "sent"),
        row("2", "skipped"),
        row("3", "invite"),
        row("4", "rejected"),
        row("5", "offer"),
    ]
    for index in (0, 1, 2, 4):
        rows[index]["invited_at"] = "2026-10-25T10:00:00+03:00"

    metrics = build_metrics(rows)

    # Приглашения: sent, skipped и invite с датой приглашения; оффер — в офферах.
    assert metrics["counts"]["invite"] == 3
    assert metrics["counts"]["sent"] == 0
    assert metrics["counts"]["skipped"] == 0
    assert metrics["invites"] == 3
    assert metrics["offers"] == 1
    assert metrics["rejected"] == 1
    fate = {item["key"]: item["count"] for item in metrics["fate"]}
    assert fate == {"sent": 0, "invite": 3, "rejected": 1, "offer": 1}


def test_cards_show_percents_when_there_are_no_responses() -> None:
    """Все четыре карточки единообразны: нет откликов — везде проценты, а не прочерк."""
    metrics = build_metrics([row("1", "skipped"), row("2", "queued")])

    assert metrics["responses"] == 0
    assert metrics["rate_text"] == {
        "response": "0.0%",
        "invite": "0.0%",
        "offer": "0.0%",
        "rejected": "0.0%",
        "ignore": "0.0%",
    }

