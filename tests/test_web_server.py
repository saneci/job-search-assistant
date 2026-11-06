"""Тесты автозапуска веб-сервера: проверка живости и запуск в фоне."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent import web_server
from agent.logging_setup import RunLogger


def _config(tmp_path: Path) -> SimpleNamespace:
    paths = SimpleNamespace(logs_dir=tmp_path / "logs")
    return SimpleNamespace(data=SimpleNamespace(paths=paths, logs_retention_days=7))


def _messages(logger: RunLogger) -> list[str]:
    return [
        json.loads(line)["message"]
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "info"
    ]


def test_server_alive_false_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет соединения — сервер считается не запущенным."""

    def fail(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("нет соединения")

    monkeypatch.setattr(web_server.httpx, "get", fail)

    assert web_server.server_alive() is False


def test_ensure_web_server_skips_when_already_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Работающий сервер повторно не запускаем."""
    monkeypatch.setattr(web_server, "server_alive", lambda timeout=0.5: True)
    logger = RunLogger(tmp_path / "logs")

    assert web_server.ensure_web_server(_config(tmp_path), logger) is True
    logger.close()

    assert any("уже запущен" in item for item in _messages(logger))


def test_ensure_web_server_starts_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Если сервер не отвечает, запускаем uvicorn и пишем адрес."""
    calls = {"alive": 0}

    def alive(timeout: float = 0.5) -> bool:
        calls["alive"] += 1
        return calls["alive"] > 1

    started: list[list[str]] = []
    flags: list[dict[str, object]] = []

    def fake_popen(command: list[str], **kwargs: object) -> SimpleNamespace:
        started.append(command)
        flags.append(kwargs)
        return SimpleNamespace(pid=1234)

    monkeypatch.setattr(web_server, "server_alive", alive)
    monkeypatch.setattr(web_server.time, "sleep", lambda _: None)
    monkeypatch.setattr(web_server.subprocess, "Popen", fake_popen)
    logger = RunLogger(tmp_path / "logs")

    assert web_server.ensure_web_server(_config(tmp_path), logger) is True
    logger.close()

    assert started
    assert started[0][-1] == str(web_server.WEB_PORT)
    assert "--log-config" in started[0]
    config_index = started[0].index("--log-config")
    assert started[0][config_index + 1].endswith("uvicorn-log.json")
    assert flags[0]["stdout"] == subprocess.DEVNULL
    env = flags[0]["env"]
    assert isinstance(env, dict)
    assert env[web_server.SERVER_LOG_RETENTION_ENV] == "7"
    assert any("запущен" in item for item in _messages(logger))
    if os.name == "nt":
        assert flags[0]["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert flags[0]["stdin"] == subprocess.DEVNULL


def test_stop_hint_points_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подсказка ведёт на CLI-команду остановки."""
    monkeypatch.setattr(web_server.os, "name", "nt")
    assert web_server.stop_hint() == "run.bat --stop"
    monkeypatch.setattr(web_server.os, "name", "posix")
    assert web_server.stop_hint() == "./run.sh --stop"


def _events(logger: RunLogger) -> list[dict]:
    return [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]


def test_stop_web_server_runs_platform_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Остановка запускает системный скрипт и пишет результат в журнал."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(web_server.subprocess, "run", fake_run)
    logger = RunLogger(tmp_path / "logs")

    assert web_server.stop_web_server(logger) == 0
    logger.close()

    assert calls
    script = "stop-server.bat" if os.name == "nt" else "stop-server.sh"
    assert calls[0][0][-1].endswith(script)
    assert _events(logger)[-1]["event"] == "web_server_stop"
    assert _events(logger)[-1]["returncode"] == 0


def test_stop_web_server_returns_error_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Скрипт остановки завершился с ошибкой — CLI получает код 1."""
    monkeypatch.setattr(
        web_server.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2),
    )
    logger = RunLogger(tmp_path / "logs")

    assert web_server.stop_web_server(logger) == 1
    logger.close()


def test_server_status_reports_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Запущенный сервер — код 0 и адрес в консоли."""
    monkeypatch.setattr(web_server, "server_alive", lambda timeout=0.5: True)
    logger = RunLogger(tmp_path / "logs")

    assert web_server.server_status(logger) == 0
    logger.close()

    assert any("запущен" in item for item in _messages(logger))
    assert _events(logger)[-1]["running"] is True


def test_server_status_reports_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Остановленный сервер — код 1 и понятное сообщение."""
    monkeypatch.setattr(web_server, "server_alive", lambda timeout=0.5: False)
    logger = RunLogger(tmp_path / "logs")

    assert web_server.server_status(logger) == 1
    logger.close()

    assert any("не запущен" in item for item in _messages(logger))
    assert _events(logger)[-1]["running"] is False
