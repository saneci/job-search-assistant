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
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Работа приложения завершена", out)

    events = read_events(tmp_path / "stamp.jsonl")
    assert events[0]["message"] == "Работа приложения завершена"


def test_info_plain_has_no_timestamp(tmp_path: Path, capsys) -> None:
    logger = RunLogger(tmp_path, run_id="report")
    report = "=================================\nОтчёт за 2026-10-21 15:19\n================================="

    logger.info_plain(report)
    logger.close()

    out = capsys.readouterr().out
    assert out == report + "\n"
    assert read_events(tmp_path / "report.jsonl")[0]["message"] == report


def test_empty_info_stays_empty(tmp_path: Path, capsys) -> None:
    logger = RunLogger(tmp_path, run_id="blank")

    logger.info("")
    logger.close()

    assert capsys.readouterr().out == "\n"
