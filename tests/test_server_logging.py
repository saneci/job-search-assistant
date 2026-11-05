"""Тесты журнала веб-сервера: метка времени, новый файл на дату и чистка."""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from web.server_logging import DailyFileHandler, server_formatter


def _logger(name: str, handler: DailyFileHandler) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_handler_writes_cli_timestamp(tmp_path: Path) -> None:
    """Строка журнала начинается с той же метки времени, что консоль агента."""
    handler = DailyFileHandler(tmp_path, retention_days=7, today=lambda: date(2026, 11, 5))
    handler.setFormatter(server_formatter())
    logger = _logger("test.server.timestamp", handler)

    logger.info("Сервер запущен")

    path = tmp_path / "2026-11-05.log"
    line = _lines(path)[0]
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Сервер запущен", line)
    logger.handlers.clear()
    handler.close()


def test_handler_switches_file_at_midnight(tmp_path: Path) -> None:
    """После смены даты событие уходит в новый файл, старый остаётся."""
    current = [date(2026, 11, 5)]
    handler = DailyFileHandler(tmp_path, retention_days=7, today=lambda: current[0])
    handler.setFormatter(server_formatter())
    logger = _logger("test.server.midnight", handler)

    logger.info("до полуночи")
    current[0] = date(2026, 11, 6)
    logger.info("после полуночи")

    first = tmp_path / "2026-11-05.log"
    second = tmp_path / "2026-11-06.log"
    assert first.exists() and second.exists()
    assert _lines(first)[0].endswith("до полуночи")
    assert _lines(second)[0].endswith("после полуночи")
    logger.handlers.clear()
    handler.close()


def test_handler_removes_logs_older_than_retention(tmp_path: Path) -> None:
    """При открытии файла удаляются журналы старше срока хранения."""
    old = tmp_path / "2026-10-01.log"
    recent = tmp_path / "2026-11-04.log"
    old.write_text("old", encoding="utf-8")
    recent.write_text("recent", encoding="utf-8")

    handler = DailyFileHandler(tmp_path, retention_days=7, today=lambda: date(2026, 11, 5))

    assert not old.exists()
    assert recent.exists()
    assert (tmp_path / "2026-11-05.log").exists()
    handler.close()
