"""Тесты постоянного профиля: запуск без storage_state и перенос cookie."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from playwright.sync_api import Error as PlaywrightError

from agent.browser import BrowserError, BrowserSession
from agent.config import BrowserConfig
from agent.logging_setup import RunLogger


class FakeInitialPage:
    """Служебная вкладка, которую создаёт постоянный профиль."""

    url = "about:blank"

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSitePage:
    """Рабочая страница, которую нельзя закрывать как служебную."""

    url = "https://career.habr.com/"

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeContext:
    """Контекст постоянного профиля: запоминает скрипт и cookie."""

    def __init__(self) -> None:
        self.cookies: list[dict[str, Any]] = []
        self.script = ""
        self.pages = [FakeInitialPage()]

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


class FakeClosePage:
    """Страница, которую можно закрыть: больше для проверки сессии не нужно."""

    def close(self) -> None:
        return


class FakeCaptchaPage:
    """Страница входа: запоминает ожидание капчи."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int | None]] = []

    def wait_for_selector(
        self,
        selector: str,
        *,
        state: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.calls.append((selector, state, timeout))


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
    assert (
        "--disable-blink-features=AutomationControlled"
        not in fake.chromium.options.get("args", [])
    )
    assert fake.chromium.context.cookies[0]["name"] == "hhtoken"
    assert fake.chromium.context.pages[0].closed is False
    site = FakeSitePage()
    fake.chromium.context.pages.append(site)
    session.close_blank_pages(site)  # type: ignore[arg-type]
    assert fake.chromium.context.pages[0].closed is True
    assert site.closed is False
    assert session.is_alive() is True
    logger.close()


def test_habr_login_waits_for_hcaptcha(tmp_path: Path) -> None:
    """Перед ручным входом ждём видимую hCaptcha, иначе Habr не пустит в аккаунт."""
    logger = RunLogger(tmp_path / "logs", run_id="captcha")
    session = BrowserSession(BrowserConfig(), tmp_path / "state.json", logger)
    page = FakeCaptchaPage()

    session._wait_for_login_captcha(page, "career.habr.com")  # type: ignore[arg-type]
    logger.close()

    assert page.calls == [("[data-captcha='hcaptcha'] iframe", "visible", 7000)]


def test_hh_login_does_not_wait_for_captcha(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path / "logs", run_id="captcha-hh")
    session = BrowserSession(BrowserConfig(), tmp_path / "state.json", logger)
    page = FakeCaptchaPage()

    session._wait_for_login_captcha(page, "hh.ru")  # type: ignore[arg-type]
    logger.close()

    assert page.calls == []


class RetryCaptchaPage:
    """Страница входа: первая загрузка капчи падает, после reload — успех."""

    def __init__(self) -> None:
        self.waits = 0
        self.reloads = 0

    def wait_for_selector(self, selector: str, **kwargs: Any) -> None:
        del selector, kwargs
        self.waits += 1
        if self.waits == 1:
            raise PlaywrightError("капча не загрузилась")

    def reload(self, **kwargs: Any) -> None:
        del kwargs
        self.reloads += 1


def test_habr_login_reloads_once_without_captcha(tmp_path: Path) -> None:
    """Если hCaptcha не загрузилась, один reload обычно возвращает её на страницу."""
    logger = RunLogger(tmp_path / "logs", run_id="captcha-reload")
    session = BrowserSession(BrowserConfig(), tmp_path / "state.json", logger)
    page = RetryCaptchaPage()

    session._wait_for_login_captcha(page, "career.habr.com")  # type: ignore[arg-type]
    logger.close()

    assert page.reloads == 1
    assert page.waits == 2


class LoginPage:
    """Страница входа: связка аккаунтов завершается после нажатия Enter."""

    def __init__(self) -> None:
        self.url = "https://account.habr.com/oauth/authorize/"
        self.visited: list[str] = []
        self.waits = 0

    def goto(self, url: str, **kwargs: Any) -> None:
        del kwargs
        self.url = url
        self.visited.append(url)

    def wait_for_timeout(self, milliseconds: int) -> None:
        del milliseconds
        self.waits += 1
        # Человек нажимает «Да», Habr возвращает его в Career.
        self.url = "https://career.habr.com/"

    def close(self) -> None:
        return


def test_login_waits_for_confirmation_without_reloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вход подтвердился не сразу: проверка повторяется без повторного запуска."""
    logger = RunLogger(tmp_path / "logs", run_id="login-wait")
    session = BrowserSession(BrowserConfig(), tmp_path / "state.json", logger)
    session._headless = False
    page = LoginPage()
    session.new_page = lambda: page  # type: ignore[assignment]
    saved: list[bool] = []
    session.save_state = lambda: saved.append(True)  # type: ignore[assignment]
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    calls: list[str] = []

    def check(current: Any) -> bool:
        calls.append(current.url)
        return current.url.startswith("https://career.habr.com")

    session.login_manually("https://account.habr.com/oauth/authorize/", check, "hh.ru")
    logger.close()

    assert len(calls) == 2
    # Со страницы входа не уходим: иначе недоделанный вход сбрасывается.
    assert page.visited == ["https://account.habr.com/oauth/authorize/"]
    assert saved == [True]


def test_login_fails_with_linking_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если вход так и не подтвердился, ошибка подсказывает про связку аккаунтов."""
    logger = RunLogger(tmp_path / "logs", run_id="login-timeout")
    session = BrowserSession(BrowserConfig(), tmp_path / "state.json", logger)
    session._headless = False
    page = LoginPage()
    page.wait_for_timeout = lambda milliseconds: None  # type: ignore[assignment]
    session.new_page = lambda: page  # type: ignore[assignment]
    session.save_state = lambda: None  # type: ignore[assignment]
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr("agent.browser.LOGIN_CONFIRM_WAIT_SECONDS", 0)

    with pytest.raises(BrowserError, match="связать аккаунты"):
        session.login_manually(
            "https://account.habr.com/oauth/authorize/", lambda current: False, "hh.ru"
        )
    logger.close()


def test_login_is_required_without_state_or_profile(tmp_path: Path) -> None:
    """Нет файла сессии и постоянного профиля — сразу ручной вход, без проверки сайта."""
    logger = RunLogger(tmp_path / "logs", run_id="no-session")
    session = BrowserSession(BrowserConfig(), tmp_path / "state.json", logger)
    calls: list[tuple[object, ...]] = []
    checked: list[bool] = []
    session.new_page = lambda: FakeClosePage()  # type: ignore[assignment]
    session.login_manually = lambda *args, **kwargs: calls.append(args)  # type: ignore[assignment]

    session.ensure_logged_in(
        check=lambda page: checked.append(True) or True,
        login_url="https://career.habr.com/",
        name="career.habr.com",
    )
    logger.close()

    assert calls
    assert checked == []


def test_profile_session_is_checked_even_without_state_file(tmp_path: Path) -> None:
    """У Habr сессия живёт в профиле браузера, файл cookie не обязателен."""
    logger = RunLogger(tmp_path / "logs", run_id="profile-session")
    session = BrowserSession(
        BrowserConfig(),
        tmp_path / "state.json",
        logger,
        tmp_path / "profile",
    )
    calls: list[tuple[object, ...]] = []
    checked: list[bool] = []
    session.new_page = lambda: FakeClosePage()  # type: ignore[assignment]
    session.login_manually = lambda *args, **kwargs: calls.append(args)  # type: ignore[assignment]

    session.ensure_logged_in(
        check=lambda page: checked.append(True) or True,
        login_url="https://career.habr.com/",
        name="career.habr.com",
    )
    logger.close()

    assert checked == [True]
    assert calls == []
