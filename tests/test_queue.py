"""Тесты очереди ручных действий."""

from __future__ import annotations

from pathlib import Path

import pytest

from hh_agent.queue import QueueError, QueueStore


def test_add_and_reload(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "queue.json")

    record = store.add(
        url="https://hh.ru/vacancy/1",
        title="Java-разработчик",
        company="Ozon",
        reason="test",
    )

    assert record is not None
    assert record["reason"] == "test"
    assert record["added_at"]

    reloaded = QueueStore(tmp_path / "queue.json").load()
    assert len(reloaded) == 1
    assert reloaded[0]["url"] == "https://hh.ru/vacancy/1"
    assert reloaded[0]["title"] == "Java-разработчик"


def test_duplicate_is_skipped(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "queue.json")
    store.add(url="https://hh.ru/vacancy/1", title="A", company="B", reason="test")

    duplicate = store.add(url="https://hh.ru/vacancy/1", title="A", company="B", reason="questionnaire")

    assert duplicate is None
    assert len(store.load()) == 1


def test_unknown_reason_rejected(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "queue.json")

    with pytest.raises(QueueError):
        store.add(url="https://hh.ru/vacancy/1", title="A", company="B", reason="not_a_reason")


def test_load_missing_file_returns_empty_list(tmp_path: Path) -> None:
    assert QueueStore(tmp_path / "queue.json").load() == []


def test_load_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    path.write_text("не json", encoding="utf-8")

    with pytest.raises(QueueError):
        QueueStore(path).load()
