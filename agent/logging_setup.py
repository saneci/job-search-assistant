"""Логирование запуска: JSON Lines, один файл на дату в каталоге logs."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, TextIO

# Что чистим по сроку хранения: журналы событий и скриншоты проблемных состояний.
LOG_FILE_PATTERNS = ("*.jsonl", "*.json", "*.png")

# Дата в имени файла: 2026-10-21.jsonl, 2026-10-21-170519-captcha.png.
_FILE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def prune_logs(
    logs_dir: Path,
    days: int,
    today: date | None = None,
    keep: Iterable[Path] = (),
) -> list[str]:
    """Удаляет журналы и скриншоты старше days дней. Возвращает имена файлов.

    Дату файла берём из имени (``2026-10-21.jsonl``), а если её там нет —
    из времени изменения файла. `keep` — файлы, которые трогать нельзя:
    например, журнал текущего запуска (он ещё открыт на запись).
    """
    if days <= 0 or not logs_dir.is_dir():
        return []
    limit = (today or date.today()) - timedelta(days=days)
    protected = {path.resolve() for path in keep}
    removed: list[str] = []
    for pattern in LOG_FILE_PATTERNS:
        for path in sorted(logs_dir.glob(pattern)):
            if not path.is_file() or path.resolve() in protected:
                continue
            file_day = _log_file_date(path)
            if file_day is None or file_day >= limit:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path.name)
    return removed


def _log_file_date(path: Path) -> date | None:
    """Дата журнала из имени файла, иначе — из времени изменения."""
    match = _FILE_DATE_RE.match(path.name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None


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

    def info(self, message: str, *, console_message: str | None = None, **fields: Any) -> None:
        """Печатает сообщение пользователю и дублирует его в лог.

        В консоль строка выводится с меткой времени в локальной зоне
        ([2026-10-21 16:02:37]); в JSONL время уже есть в поле ts.
        `console_message` позволяет показать человеку более полную строку
        (например, с номером вакансии), не дублируя её в поле `message`.
        Дополнительные поля (например, `vacancy_id`) попадают в запись события.
        """
        self.event("info", message=message, **fields)
        print(self._stamped(console_message or message), flush=True)

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

    def switch_day(self, day: str) -> bool:
        """Переключает журнал на файл другой даты.

        Запуск может идти через полночь, а файл журнала — файл даты: после
        полуночи события должны попасть в новый файл, а не в сегодняшний.
        Возвращает True, если файл действительно сменился.
        """
        if day == self.day:
            return False
        self.close()
        self.day = day
        self.events_path = self.logs_dir / f"{self.day}.jsonl"
        self._file = self.events_path.open("a", encoding="utf-8")
        return True

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
