"""Запуск обычного контекста: sandbox Chromium управляется конфигом."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.browser import BrowserSession
from agent.config import BrowserConfig
from agent.logging_setup import RunLogger


class FakeContext:
    """Контекст: запоминает параметры создания."""

    def __init__(self) -> None:
        self.options: dict[str, Any] = {}

    def close(self) -> None:
        pass


class FakeBrowser:
    """Браузер: запоминает параметры контекста."""

    def __init__(self) -> None:
        self.context = FakeContext()

    def new_context(self, **kwargs: Any) -> FakeContext:
        self.context.options = kwargs
        return self.context

    def close(self) -> None:
        pass


class FakeChromium:
    def __init__(self) -> None:
        self.options: dict[str, Any] | None = None
        self.browser = FakeBrowser()

    def launch(self, **kwargs: Any) -> FakeBrowser:
        self.options = kwargs
        return self.browser


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()


def start_session(tmp_path: Path, config: BrowserConfig) -> tuple[BrowserSession, FakePlaywright]:
    """Поднимает сессию на фейковом Playwright и возвращает её с перехватчиком."""
    logger = RunLogger(tmp_path / "logs", run_id="sandbox")
    session = BrowserSession(config, tmp_path / "state.json", logger)
    fake = FakePlaywright()
    session._playwright = fake  # type: ignore[assignment]
    session._launch(headless=True)
    logger.close()
    return session, fake


def test_launch_enables_chromium_sandbox(tmp_path: Path) -> None:
    """По умолчанию sandbox включён: браузер не предупреждает про --no-sandbox."""
    _session, fake = start_session(tmp_path, BrowserConfig())

    assert fake.chromium.options is not None
    assert fake.chromium.options["chromium_sandbox"] is True


def test_chromium_sandbox_can_be_disabled(tmp_path: Path) -> None:
    """В окружении без sandbox его выключают конфигом, не правкой кода."""
    _session, fake = start_session(tmp_path, BrowserConfig(chromium_sandbox=False))

    assert fake.chromium.options is not None
    assert fake.chromium.options["chromium_sandbox"] is False
