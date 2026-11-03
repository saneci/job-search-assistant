"""Межпроцессный доступ к файлам дневных отчётов: блокировка каталога и атомарная запись."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

LOCK_FILE = ".report.lock"
LOCK_WAIT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.05
LOCK_STALE_SECONDS = 120.0


class ReportLockError(RuntimeError):
    """Не удалось получить блокировку каталога отчётов."""


@contextmanager
def report_lock(directory: Path, timeout: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
    """Держит межпроцессную блокировку каталога отчётов.

    Файл блокировки создаётся с `O_EXCL`, поэтому одновременно её получает один
    писатель, остальные ждут. Имя файла начинается с точки, поэтому отчётом он
    не считается.
    """
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / LOCK_FILE
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            _drop_stale_lock(lock_path)
            if time.monotonic() >= deadline:
                raise ReportLockError(
                    f"не удалось получить блокировку отчётов: {lock_path}"
                ) from None
            time.sleep(LOCK_POLL_SECONDS)
            continue
        except OSError as exc:
            raise ReportLockError(f"не удалось получить блокировку отчётов: {exc}") from exc
        try:
            os.write(descriptor, f"{os.getpid()} {time.time():.3f}".encode("ascii"))
        finally:
            os.close(descriptor)
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
        return


def _drop_stale_lock(lock_path: Path) -> None:
    """Удаляет блокировку, которую оставил упавший процесс, если она старше порога."""
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return
    if age < LOCK_STALE_SECONDS:
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


def write_report_file(path: Path, payload: dict[str, Any]) -> None:
    """Пишет отчёт атомарно: через временный файл и замену.

    Имя временного файла уникально для процесса и вызова, поэтому два писателя
    не мешают друг другу: читатель видит либо старый файл целиком, либо новый.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_path.replace(path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
