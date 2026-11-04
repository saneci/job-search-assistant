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
    return SimpleNamespace(data=SimpleNamespace(paths=paths))


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

    web_server.ensure_web_server(_config(tmp_path), logger)
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

    web_server.ensure_web_server(_config(tmp_path), logger)
    logger.close()

    assert started
    assert started[0][-1] == str(web_server.WEB_PORT)
    assert any("запущен" in item for item in _messages(logger))
    if os.name == "nt":
        assert flags[0]["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert flags[0]["stdin"] == subprocess.DEVNULL


def test_stop_hint_points_to_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подсказка ведёт в scripts/ и называется по системе."""
    monkeypatch.setattr(web_server.os, "name", "nt")
    assert web_server.stop_hint() == "scripts\\stop-server.bat"
    monkeypatch.setattr(web_server.os, "name", "posix")
    assert web_server.stop_hint() == "scripts/stop-server.sh"
