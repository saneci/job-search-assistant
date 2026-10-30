"""Тесты распределения вакансий по каналам в метриках."""

from __future__ import annotations

from typing import Any

from web.metrics import build_metrics


def vacancy(url: str, status: str, channel: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {
        "url": url,
        "title": "Java",
        "company": "Ozon",
        "status": status,
        "created_at": "2026-10-24T10:00:00+03:00",
        "updated_at": "2026-10-24T10:00:00+03:00",
    }
    if channel:
        item["channel"] = channel
    return item


def test_channels_are_counted() -> None:
    metrics = build_metrics(
        [
            vacancy("https://hh.ru/vacancy/1", "sent", "hh"),
            vacancy("https://career.habr.com/vacancies/2", "sent", "habr"),
            vacancy("https://career.habr.com/vacancies/3", "skipped", "habr"),
        ]
    )

    by_key = {item["key"]: item for item in metrics["channels"]}
    assert by_key["hh"]["count"] == 1
    assert by_key["habr"]["count"] == 2
    assert by_key["habr"]["percent"] == 66.67
    assert by_key["habr"]["label"] == "Habr Career"


def test_old_records_go_to_unknown_channel() -> None:
    metrics = build_metrics([vacancy("https://hh.ru/vacancy/4", "sent")])

    by_key = {item["key"]: item for item in metrics["channels"]}
    assert by_key["unknown"]["count"] == 1


def test_empty_metrics_have_no_channels() -> None:
    metrics = build_metrics([])

    assert metrics["channels"] == []
