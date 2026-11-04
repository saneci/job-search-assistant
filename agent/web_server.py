"""Автоматический запуск веб-сервера отчётов в фоне.

Сервер нужен только для просмотра отчётов, поэтому его сбой не должен
останавливать агента. Повторный запуск не делаем: проверяем HTTP-ответ.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import httpx

from .config import PROJECT_ROOT, AppConfig
from .logging_setup import RunLogger

WEB_HOST = "127.0.0.1"
WEB_PORT = 8000
WEB_URL = f"http://{WEB_HOST}:{WEB_PORT}/"
_START_ATTEMPTS = 50
_START_DELAY = 0.1


def server_alive(timeout: float = 0.5) -> bool:
    """Отвечает ли веб-сервер отчётов по ожидаемому адресу."""
    try:
        response = httpx.get(WEB_URL, timeout=timeout)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


def stop_hint() -> str:
    """Как человеку остановить сервер в его системе."""
    if os.name == "nt":
        return "scripts\\stop-server.bat"
    return "scripts/stop-server.sh"


def ensure_web_server(config: AppConfig, logger: RunLogger) -> None:
    """Запускает веб-сервер в фоне, если он ещё не запущен.

    Повторный запуск не нужен: проверяем HTTP-ответ. Сбой запуска не должен
    останавливать агента: отчёт всё равно сохранится в файл.
    """
    if server_alive():
        logger.info(f"Веб-сервер уже запущен: {WEB_URL}")
        return
    log_dir = config.data.paths.logs_dir / "uvicorn"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{date.today():%Y-%m-%d}.log"
    executable = sys.executable
    if os.name == "nt":
        # pythonw.exe запускает сервер без консольного окна.
        pythonw = Path(executable).with_name("pythonw.exe")
        if pythonw.exists():
            executable = str(pythonw)
    command = [
        executable,
        "-m",
        "uvicorn",
        "web.main:app",
        "--host",
        WEB_HOST,
        "--port",
        str(WEB_PORT),
    ]
    kwargs: dict[str, object] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    pid_path = log_dir / "server.pid"
    try:
        with open(log_path, "ab") as log_file:
            kwargs["stdout"] = log_file
            process = subprocess.Popen(command, **kwargs)  # noqa: S603 - команда своя
        pid_path.write_text(str(process.pid), encoding="utf-8")
    except OSError as exc:
        logger.info(f"Не удалось запустить веб-сервер: {exc}")
        return
    for _ in range(_START_ATTEMPTS):
        if server_alive(0.2):
            logger.info(f"Веб-сервер запущен: {WEB_URL}")
            logger.info(f"Остановить веб-сервер: {stop_hint()}")
            return
        time.sleep(_START_DELAY)
    if process.poll() is None:
        logger.info(f"Веб-сервер запускается, смотрите журнал: {log_path}")
        logger.info(f"Остановить веб-сервер: {stop_hint()}")
        return
    try:
        pid_path.unlink()
    except OSError:
        pass
    logger.info(f"Веб-сервер не запустился, смотрите журнал: {log_path}")
    logger.info(f"Остановить веб-сервер: {stop_hint()}")
