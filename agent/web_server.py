"""Управление веб-сервером отчётов: запуск, остановка и проверка состояния.

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

from .config import PROJECT_ROOT, AppConfig, WebServerConfig
from .logging_setup import (
    SERVER_LOG_DIR_ENV,
    SERVER_LOG_DIRNAME,
    SERVER_LOG_RETENTION_ENV,
    RunLogger,
)

WEB_HOST = "127.0.0.1"
WEB_PORT = 8000
# healthcheck и адрес для ПК всегда через loopback: 0.0.0.0 — адрес bind,
# а не адрес, по которому к серверу подключаются.
WEB_CHECK_HOST = "127.0.0.1"
WEB_URL = f"http://{WEB_CHECK_HOST}:{WEB_PORT}/"
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
        return "run.bat --stop"
    return "./run.sh --stop"


def stop_web_server(logger: RunLogger) -> int:
    """Останавливает сервер через системный скрипт, который умеет искать PID.

    Скрипт запускается отдельным процессом: он очищает PID-файл, завершает
    дерево процессов и проверяет порт. Вывод скрипта остаётся в консоли.
    """
    script = PROJECT_ROOT / "scripts" / (
        "stop-server.bat" if os.name == "nt" else "stop-server.sh"
    )
    command = ["cmd", "/c", str(script)] if os.name == "nt" else ["sh", str(script)]
    try:
        completed = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)
    except OSError as exc:
        logger.info(f"Не удалось остановить веб-сервер: {exc}")
        return 1
    logger.event("web_server_stop", returncode=completed.returncode)
    return 0 if completed.returncode == 0 else 1


def server_status(logger: RunLogger) -> int:
    """Показывает, отвечает ли веб-сервер: 0 — запущен, 1 — нет."""
    running = server_alive()
    if running:
        logger.info(f"Веб-сервер запущен: {WEB_URL}")
    else:
        logger.info("Веб-сервер не запущен.")
    logger.event("web_server_status", running=running)
    return 0 if running else 1


def _log_settings(config: AppConfig | WebServerConfig) -> tuple[Path, int]:
    """Каталог журналов и срок хранения из любой формы конфигурации.

    Запуск агента передаёт полную конфигурацию, а `--start` — только параметры
    веб-сервера: серверу больше ничего не нужно.
    """
    logs_dir = getattr(config, "logs_dir", None)
    if logs_dir is not None:
        return Path(logs_dir), int(config.logs_retention_days)
    return (
        Path(config.data.paths.logs_dir),
        int(config.data.logs_retention_days),
    )


def ensure_web_server(
    config: AppConfig | WebServerConfig, logger: RunLogger
) -> bool:
    """Запускает веб-сервер в фоне, если он ещё не запущен.

    Серверу нужны только каталог журналов и срок их хранения: отчёты он
    читает из каталога данных по умолчанию. Поэтому запуск не требует полной
    конфигурации агента. Повторный запуск не нужен: проверяем HTTP-ответ.
    Сбой запуска не должен останавливать агента: отчёт всё равно сохранится
    в файл. Возвращает True, если сервер уже работает или запущен, иначе False.
    """
    if server_alive():
        logger.info(f"Веб-сервер уже запущен: {WEB_URL}")
        return True
    logs_dir, retention_days = _log_settings(config)
    log_dir = logs_dir / SERVER_LOG_DIRNAME
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{date.today():%Y-%m-%d}.log"
    log_config = PROJECT_ROOT / "web" / "uvicorn-log.json"
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
        "--log-config",
        str(log_config),
        "--host",
        WEB_HOST,
        "--port",
        str(WEB_PORT),
    ]
    kwargs: dict[str, object] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": {
            **os.environ,
            SERVER_LOG_DIR_ENV: str(log_dir),
            SERVER_LOG_RETENTION_ENV: str(retention_days),
        },
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
        process = subprocess.Popen(command, **kwargs)  # noqa: S603 - команда своя
        pid_path.write_text(str(process.pid), encoding="utf-8")
    except OSError as exc:
        logger.info(f"Не удалось запустить веб-сервер: {exc}")
        return False
    for _ in range(_START_ATTEMPTS):
        if server_alive(0.2):
            logger.info(f"Веб-сервер запущен: {WEB_URL}")
            logger.info(f"Остановить веб-сервер: {stop_hint()}")
            return True
        time.sleep(_START_DELAY)
    if process.poll() is None:
        logger.info(f"Веб-сервер запускается, смотрите журнал: {log_path}")
        logger.info(f"Остановить веб-сервер: {stop_hint()}")
        return True
    try:
        pid_path.unlink()
    except OSError:
        pass
    logger.info(f"Веб-сервер не запустился, смотрите журнал: {log_path}")
    logger.info(f"Остановить веб-сервер: {stop_hint()}")
    return False
