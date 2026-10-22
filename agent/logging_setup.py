"""Логирование запуска: JSON Lines, один файл на дату в каталоге logs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


class RunLogger:
    """Пишет события в logs/<дата>.jsonl и дублирует прогресс в консоль.

    Файл один на дату: перезапуски дописывают события в конец, поэтому в каждой
    записи есть ``run_id`` — по нему видно, к какому запуску относится событие.
    """

    def __init__(self, logs_dir: Path, run_id: str | None = None) -> None:
        now = datetime.now()
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or now.strftime("%Y-%m-%d-%H%M%S")
        self.day = now.strftime("%Y-%m-%d")
        self.events_path = self.logs_dir / f"{self.day}.jsonl"
        self._file: TextIO = self.events_path.open("a", encoding="utf-8")

    def event(self, name: str, **fields: Any) -> None:
        """Записывает одно событие в лог."""
        record: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "event": name,
        }
        record.update(fields)
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def info(self, message: str) -> None:
        """Печатает сообщение пользователю и дублирует его в лог.

        В консоль строка выводится с меткой времени в локальной зоне
        ([2026-10-21 16:02:37]); в JSONL время уже есть в поле ts.
        """
        self.event("info", message=message)
        print(self._stamped(message), flush=True)

    def info_plain(self, message: str) -> None:
        """Печатает многострочный блок (например, отчёт) без меток времени."""
        self.event("info", message=message)
        print(message, flush=True)

    @staticmethod
    def _stamped(message: str) -> str:
        if not message:
            return message
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"[{stamp}] {message}"

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
