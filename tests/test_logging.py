"""Тесты журнала запуска: префикс вакансии, метки времени в консоли."""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent.logging_setup import RunLogger


def read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_info_adds_timestamp(tmp_path: Path, capsys) -> None:
    logger = RunLogger(tmp_path, run_id="stamp")

    logger.info("Работа приложения завершена")
    logger.close()

    out = capsys.readouterr().out.strip()
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Работа приложения завершена", out
    )

    events = read_events(logger.events_path)
    assert events[0]["message"] == "Работа приложения завершена"
    assert events[0]["run_id"] == "stamp"


def test_info_plain_has_no_timestamp(tmp_path: Path, capsys) -> None:
    logger = RunLogger(tmp_path, run_id="report")
    report = "=================================\nОтчёт за 2026-10-21 15:19\n================================="

    logger.info_plain(report)
    logger.close()

    out = capsys.readouterr().out
    assert out == report + "\n"
    assert read_events(logger.events_path)[0]["message"] == report


def test_log_file_named_by_date(tmp_path: Path) -> None:
    """Файл лога — один на дату, а не на запуск."""
    logger = RunLogger(tmp_path, run_id="first")

    assert logger.events_path == tmp_path / f"{logger.day}.jsonl"
    logger.close()


def test_restart_appends_to_same_day_file(tmp_path: Path) -> None:
    """Перезапуск дописывает события в конец файла дня, а не затирает его."""
    first = RunLogger(tmp_path, run_id="first")
    first.info("первый запуск")
    first.close()

    second = RunLogger(tmp_path, run_id="second")
    second.info("второй запуск")
    second.close()

    events = read_events(second.events_path)
    assert [item["message"] for item in events] == ["первый запуск", "второй запуск"]
    assert [item["run_id"] for item in events] == ["first", "second"]


def test_switch_day_opens_new_file(tmp_path: Path) -> None:
    """Через полночь события уходят в файл новой даты, а не вчерашний."""
    logger = RunLogger(tmp_path, run_id="long-run")
    first_path = logger.events_path
    logger.info("до полуночи")

    assert logger.switch_day("2030-01-01") is True
    logger.info("после полуночи")
    logger.close()

    assert logger.events_path != first_path
    assert [item["message"] for item in read_events(first_path)] == ["до полуночи"]
    assert [item["message"] for item in read_events(logger.events_path)] == [
        "после полуночи"
    ]
    assert read_events(logger.events_path)[0]["run_id"] == "long-run"


def test_switch_day_keeps_current_file(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path, run_id="same-day")

    assert logger.switch_day(logger.day) is False
    logger.close()


def test_empty_info_stays_empty(tmp_path: Path, capsys) -> None:
    logger = RunLogger(tmp_path, run_id="blank")

    logger.info("")
    logger.close()

    assert capsys.readouterr().out == "\n"
