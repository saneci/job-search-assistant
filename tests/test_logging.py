"""Тесты журнала запуска: префикс вакансии, метки времени в консоли, чистка логов."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from agent.logging_setup import RunLogger, log_timestamp, new_run_id, prune_logs


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


def test_event_time_has_no_separator(tmp_path: Path) -> None:
    """Время события журнала пишется без разделителя `T`: «2026-11-05 19:23:23+03:00»."""
    logger = RunLogger(tmp_path, run_id="stamp")

    logger.event("info", message="проверка")
    logger.close()

    stamp = read_events(logger.events_path)[0]["ts"]
    assert "T" not in stamp
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", stamp)
    assert datetime.fromisoformat(stamp).tzinfo is not None


def test_log_timestamp_keeps_zone_offset() -> None:
    """Метка времени журнала сохраняет смещение зоны и локальное время."""
    moment = datetime(2026, 11, 5, 19, 23, 23, tzinfo=timezone(timedelta(hours=3)))

    assert log_timestamp(moment) == "2026-11-05 19:23:23+03:00"


def test_console_prints_without_event(tmp_path: Path, capsys) -> None:
    """Подробность видна в консоли, но в JSONL не дублируется."""
    logger = RunLogger(tmp_path, run_id="stamp")

    logger.console("LLM-вердикт: подходит")
    logger.close()

    out = capsys.readouterr().out.strip()
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] LLM-вердикт: подходит", out
    )
    assert read_events(logger.events_path) == []


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


def test_prune_logs_removes_old_files_only(tmp_path: Path) -> None:
    """Журналы и скриншоты старше срока удаляются, свежие остаются."""
    old_log = tmp_path / "2026-09-01.jsonl"
    old_shot = tmp_path / "2026-09-01-101500-captcha.png"
    fresh_log = tmp_path / "2026-10-20.jsonl"
    for path in (old_log, old_shot, fresh_log):
        path.write_text("x", encoding="utf-8")

    removed = prune_logs(tmp_path, days=7, today=date(2026, 10, 21))

    assert removed == ["2026-09-01.jsonl", "2026-09-01-101500-captcha.png"]
    assert fresh_log.exists()


def test_prune_logs_removes_old_server_logs(tmp_path: Path) -> None:
    """Логи веб-сервера чистятся по тому же сроку, что журналы агента."""
    server_dir = tmp_path / "uvicorn"
    server_dir.mkdir()
    old = server_dir / "2026-09-01.log"
    fresh = server_dir / "2026-10-20.log"
    old.write_text("x", encoding="utf-8")
    fresh.write_text("x", encoding="utf-8")

    removed = prune_logs(tmp_path, days=7, today=date(2026, 10, 21))

    assert removed == ["2026-09-01.log"]
    assert fresh.exists()


def test_prune_logs_keeps_current_file(tmp_path: Path) -> None:
    """Журнал текущего запуска открыт на запись — его не трогаем."""
    current = tmp_path / "2026-09-01.jsonl"
    current.write_text("x", encoding="utf-8")

    removed = prune_logs(tmp_path, days=7, today=date(2026, 10, 21), keep=[current])

    assert removed == []
    assert current.exists()


def test_prune_logs_uses_mtime_without_date_in_name(tmp_path: Path) -> None:
    """Файл без даты в имени оцениваем по времени изменения."""
    path = tmp_path / "old-run.jsonl"
    path.write_text("x", encoding="utf-8")
    stamp = datetime(2026, 1, 1, 10, 0).timestamp()
    os.utime(path, (stamp, stamp))

    assert prune_logs(tmp_path, days=7, today=date(2026, 10, 21)) == ["old-run.jsonl"]


def test_prune_logs_without_directory_is_noop(tmp_path: Path) -> None:
    assert prune_logs(tmp_path / "missing", days=7, today=date(2026, 10, 21)) == []


def test_run_id_has_single_format() -> None:
    """Идентификатор запуска — дата и время без разделителей, один формат."""
    assert re.fullmatch(r"\d{14}", new_run_id(datetime(2026, 11, 3, 9, 21, 14)))
    assert new_run_id(datetime(2026, 11, 3, 9, 21, 14)) == "20261103092114"
