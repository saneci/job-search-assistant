"""Логирование запуска: JSON Lines в каталоге .logs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


class RunLogger:
    """Пишет события запуска в .logs/<run_id>.jsonl и дублирует прогресс в консоль."""

    def __init__(self, logs_dir: Path, run_id: str | None = None) -> None:
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.events_path = self.logs_dir / f"{self.run_id}.jsonl"
        self._file: TextIO = self.events_path.open("a", encoding="utf-8")

    def event(self, name: str, **fields: Any) -> None:
        """Записывает одно событие в лог."""
        record: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": name,
        }
        record.update(fields)
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def info(self, message: str) -> None:
        """Печатает сообщение пользователю."""
        print(message, flush=True)

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
