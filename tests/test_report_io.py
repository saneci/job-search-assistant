"""Тесты безопасного конкурентного доступа к файлам отчётов."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from agent.processed import ProcessedStore
from agent.report_io import LOCK_FILE, LOCK_STALE_SECONDS, report_lock


def test_report_lock_is_exclusive(tmp_path: Path) -> None:
    """Пока блокировку держит один, второй писатель ждёт."""
    order: list[str] = []
    started = threading.Event()

    def holder() -> None:
        with report_lock(tmp_path):
            started.set()
            time.sleep(0.3)
            order.append("first")

    thread = threading.Thread(target=holder)
    thread.start()
    assert started.wait(1.0)
    with report_lock(tmp_path):
        order.append("second")
    thread.join()

    assert order == ["first", "second"]


def test_stale_lock_is_dropped(tmp_path: Path) -> None:
    """Блокировку упавшего процесса снимаем по сроку, а не ждём вечно."""
    lock_path = tmp_path / LOCK_FILE
    lock_path.write_text("99999 0", encoding="ascii")
    old = time.time() - LOCK_STALE_SECONDS - 1
    os.utime(lock_path, (old, old))

    with report_lock(tmp_path, timeout=0.5):
        pass

    assert not lock_path.exists()


def test_concurrent_records_are_not_lost(tmp_path: Path) -> None:
    """Два процесса пишут отчёт одновременно: записи не перетирают друг друга."""
    def write(index: int) -> None:
        store = ProcessedStore(tmp_path, today=date(2026, 11, 3))
        for number in range(10):
            vacancy_id = index * 100 + number
            store.record(
                url=f"https://hh.ru/vacancy/{vacancy_id}",
                title=f"Вакансия {vacancy_id}",
                company="Компания",
                status="sent",
                profile="Java Developer",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, range(2)))

    payload = json.loads((tmp_path / "2026-11-03.json").read_text(encoding="utf-8"))
    assert len(payload["vacancies"]) == 20
    assert payload["summary"]["sent"] == 20
