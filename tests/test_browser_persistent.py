"""Тесты постоянного профиля: запуск без storage_state и перенос cookie."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.browser import BrowserSession
from agent.config import BrowserConfig
from agent.logging_setup import RunLogger


class FakeContext:
    """Контекст постоянного профиля: запоминает скрипт и cookie."""

    def __init__(self) -> None:
        self.cookies: list[dict[str, Any]] = []
        self.script = ""

    def add_init_script(self, script: str) -> None:
        self.script = script

    def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.cookies.extend(cookies)


class FakeChromium:
    def __init__(self) -> None:
        self.options: dict[str, Any] | None = None
        self.context = FakeContext()

    def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
        self.options = kwargs
        return self.context


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()


def test_persistent_launch_does_not_pass_storage_state(tmp_path: Path) -> None:
    """launch_persistent_context не понимает storage_state — cookie идут через add_cookies."""
    state = tmp_path / "habr-state.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [{"name": "hhtoken", "value": "x", "domain": ".habr.com", "path": "/"}],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    logger = RunLogger(tmp_path / "logs", run_id="persistent")
    session = BrowserSession(
        BrowserConfig(channel="chrome"),
        state,
        logger,
        tmp_path / "profile",
    )
    fake = FakePlaywright()
    session._playwright = fake  # type: ignore[assignment]

    session._launch(headless=True)

    assert fake.chromium.options is not None
    assert "storage_state" not in fake.chromium.options
    assert fake.chromium.options["user_data_dir"] == str(tmp_path / "profile")
    assert fake.chromium.options["chromium_sandbox"] is True
    assert fake.chromium.context.cookies[0]["name"] == "hhtoken"
    assert session.is_alive() is True
    logger.close()
