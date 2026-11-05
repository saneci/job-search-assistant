"""Журнал веб-сервера: файл на дату, метка времени как в консоли агента.

Uvicorn пишет через logging. Обработчик сам открывает
``<logs_dir>/uvicorn/<YYYY-MM-DD>.log``, переключает файл в полночь и удаляет
файлы старше срока хранения. Срок берётся из ``data.logs_retention_days``,
который агент передаёт через переменную окружения.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import TextIO

from agent.config import DEFAULT_LOGS_DIR
from agent.logging_setup import (
    CONSOLE_TIMESTAMP_FORMAT,
    SERVER_LOG_DIR_ENV,
    SERVER_LOG_DIRNAME,
    SERVER_LOG_RETENTION_ENV,
)

DEFAULT_SERVER_LOG_RETENTION_DAYS = 7
SERVER_LOG_SUFFIX = ".log"
SERVER_LOG_FORMAT = "[%(asctime)s] %(message)s"
_SERVER_LOG_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def server_log_dir() -> Path:
    """Каталог журнала сервера: из окружения, иначе каталог данных агента."""
    raw = os.environ.get(SERVER_LOG_DIR_ENV)
    if raw:
        return Path(raw)
    return DEFAULT_LOGS_DIR / SERVER_LOG_DIRNAME


def server_log_retention_days() -> int:
    """Срок хранения журналов сервера: из окружения, иначе 7 дней."""
    raw = os.environ.get(SERVER_LOG_RETENTION_ENV, "")
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_SERVER_LOG_RETENTION_DAYS
    return days if days > 0 else DEFAULT_SERVER_LOG_RETENTION_DAYS


def _file_date(path: Path) -> date | None:
    """Дата из имени журнала вида ``YYYY-MM-DD.log``."""
    match = _SERVER_LOG_DATE_RE.match(path.name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


class DailyFileHandler(logging.Handler):
    """Пишет строки в файл текущей даты и чистит старые журналы."""

    def __init__(
        self,
        log_dir: Path,
        retention_days: int = DEFAULT_SERVER_LOG_RETENTION_DAYS,
        today: Callable[[], date] = date.today,
    ) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.retention_days = retention_days
        self._today = today
        self._day: date | None = None
        self._stream: TextIO | None = None
        self._open()

    def _open(self) -> None:
        """Открывает файл текущей даты и удаляет журналы старше срока."""
        self._close_stream()
        day = self._today()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._day = day
        self._stream = (self.log_dir / f"{day:%Y-%m-%d}{SERVER_LOG_SUFFIX}").open(
            "a", encoding="utf-8"
        )
        self._prune(day)

    def _prune(self, day: date) -> None:
        limit = day - timedelta(days=self.retention_days)
        for path in self.log_dir.glob(f"*{SERVER_LOG_SUFFIX}"):
            file_day = _file_date(path)
            if file_day is None or file_day >= limit:
                continue
            try:
                path.unlink()
            except OSError:
                continue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._today() != self._day:
                self._open()
            stream = self._stream
            if stream is None:
                return
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.close()
        except OSError:
            pass
        self._stream = None

    def close(self) -> None:
        self._close_stream()
        super().close()


def server_formatter() -> logging.Formatter:
    """Формат строки: та же метка времени, что у консоли агента."""
    return logging.Formatter(SERVER_LOG_FORMAT, datefmt=CONSOLE_TIMESTAMP_FORMAT)


def daily_handler(**_: object) -> DailyFileHandler:
    """Фабрика обработчика для конфигурации логирования uvicorn."""
    handler = DailyFileHandler(server_log_dir(), server_log_retention_days())
    handler.setFormatter(server_formatter())
    return handler
