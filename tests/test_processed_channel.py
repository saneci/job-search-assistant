"""Тесты поля channel в дневном отчёте."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from agent.processed import ProcessedStore


def read_day(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["vacancies"]


def test_record_writes_channel(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    store.record(
        url="https://career.habr.com/vacancies/1",
        title="Java",
        company="Ozon",
        status="sent",
        channel="habr",
    )

    rows = read_day(store.path)
    assert rows[0]["channel"] == "habr"


def test_update_keeps_and_replaces_channel(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url="https://career.habr.com/vacancies/1",
        title="Java",
        company="Ozon",
        status="queued",
        channel="habr",
    )

    store.record(
        url="https://career.habr.com/vacancies/1",
        title="Java",
        company="Ozon",
        status="sent",
        channel="habr",
    )

    rows = read_day(store.path)
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["channel"] == "habr"
