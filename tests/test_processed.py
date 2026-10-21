"""Тесты памяти об обработанных вакансиях (data/processed/<дата>.json)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from agent.processed import ProcessedStore


def test_add_and_find_in_same_run(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.add(
        url="https://hh.ru/vacancy/1",
        title="Java-разработчик",
        company="Ozon",
        status="sent",
    )

    assert store.find("Java-разработчик", "Ozon", "https://hh.ru/vacancy/1") == "2026-10-21"


def test_file_named_by_date_with_queue_like_format(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.add(
        url="https://hh.ru/vacancy/1",
        title="Java-разработчик",
        company="Ozon",
        status="sent",
    )

    path = tmp_path / "2026-10-21.json"
    records = json.loads(path.read_text(encoding="utf-8"))

    assert isinstance(records, list)
    assert records[0]["title"] == "Java-разработчик"
    assert records[0]["status"] == "sent"
    assert "processed_at" in records[0]


def test_new_run_skips_previous_vacancy(tmp_path: Path) -> None:
    ProcessedStore(tmp_path, today=date(2026, 10, 21)).add(
        url="https://hh.ru/vacancy/1",
        title="Java-разработчик",
        company="Ozon",
        status="skipped",
    )

    next_day = ProcessedStore(tmp_path, today=date(2026, 10, 22))
    next_day.load()

    assert next_day.find("Java-разработчик", "Ozon", "https://hh.ru/vacancy/2") == "2026-10-21"


def test_history_reads_all_date_files(tmp_path: Path) -> None:
    ProcessedStore(tmp_path, today=date(2026, 10, 20)).add(
        url="https://hh.ru/vacancy/1",
        title="Первая",
        company="Ozon",
        status="sent",
    )
    ProcessedStore(tmp_path, today=date(2026, 10, 21)).add(
        url="https://hh.ru/vacancy/2",
        title="Вторая",
        company="Ozon",
        status="queued",
    )

    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.load()

    assert store.find("Первая", "Ozon", "") == "2026-10-20"
    assert store.find("Вторая", "Ozon", "") == "2026-10-21"


def test_duplicate_vacancy_is_not_written_twice(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.add(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="sent")
    assert not store.add(
        url="https://hh.ru/vacancy/2", title="Java", company="Ozon", status="sent"
    )

    records = json.loads((tmp_path / "2026-10-21.json").read_text(encoding="utf-8"))
    assert len(records) == 1


def test_empty_vacancy_is_not_stored(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert not store.add(url="", title="", company="", status="sent")
    assert store.find("", "", "") is None


def test_broken_file_does_not_break_load(tmp_path: Path) -> None:
    (tmp_path / "2026-10-20.json").write_text("{не json", encoding="utf-8")

    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.load() == 0



def test_prune_removes_old_files_only(tmp_path: Path) -> None:
    old = tmp_path / "2026-10-01.json"
    fresh = tmp_path / "2026-10-20.json"
    old.write_text("[]", encoding="utf-8")
    fresh.write_text("[]", encoding="utf-8")

    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.prune(days=7) == ["2026-10-01.json"]
    assert not old.exists()
    assert fresh.exists()


def test_prune_keeps_files_within_retention(tmp_path: Path) -> None:
    (tmp_path / "2026-10-15.json").write_text("[]", encoding="utf-8")
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.prune(days=7) == []


def test_prune_ignores_foreign_files(tmp_path: Path) -> None:
    (tmp_path / "notes.json").write_text("[]", encoding="utf-8")
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.prune(days=7) == []
    assert (tmp_path / "notes.json").exists()


def test_prune_without_directory_is_noop(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path / "missing", today=date(2026, 10, 21))

    assert store.prune(days=7) == []

