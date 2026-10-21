"""Playwright: запуск Chromium, сохранение и восстановление сессии hh.ru."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    sync_playwright,
)

from .config import BrowserConfig
from .logging_setup import RunLogger

HH_HOME = "https://hh.ru/"
HH_RESUMES = "https://hh.ru/applicant/resumes"

_PROFILE_MARKERS = "[data-qa='mainmenu_applicantProfile'], [data-qa='mainmenu_avatar']"
_LOGIN_MARKERS = "a[href*='account/login'], [data-qa='login']"


class BrowserError(RuntimeError):
    """Ошибка работы с браузером или сессией hh.ru."""


class CaptchaError(BrowserError):
    """hh.ru показал капчу."""


class BrowserSession:
    """Открывает браузер и управляет сохранённой сессией."""

    def __init__(
        self,
        config: BrowserConfig,
        state_path: Path,
        logger: RunLogger,
    ) -> None:
        self.config = config
        self.state_path = state_path
        self.logger = logger
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._headless = config.headless

    def __enter__(self) -> "BrowserSession":
        self._playwright = sync_playwright().start()
        # Первый запуск (нет state.json) всегда с видимым браузером: нужен ручной вход.
        self._launch(headless=self.config.headless and self.state_path.exists())
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _launch(self, headless: bool) -> None:
        assert self._playwright is not None
        self._browser = self._playwright.chromium.launch(
            headless=headless,
            slow_mo=self.config.slow_mo_ms,
        )
        context_options: dict[str, Any] = {
            "locale": "ru-RU",
            "viewport": {"width": 1680, "height": 1000},
        }
        if self.state_path.exists():
            context_options["storage_state"] = str(self.state_path)
        self._context = self._browser.new_context(**context_options)
        self._headless = headless

    def _ensure_headed(self) -> None:
        if not self._headless:
            return
        self.logger.info("Перезапускаю браузер с окном: требуется ручной вход в hh.ru.")
        self._close_current()
        self._launch(headless=False)

    def _close_current(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def new_page(self) -> Page:
        if self._context is None:
            raise BrowserError("Браузер не запущен")
        return self._context.new_page()

    def save_state(self) -> None:
        if self._context is None:
            raise BrowserError("Браузер не запущен")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(self.state_path))
        self.logger.event("session_saved", path=str(self.state_path))
        self.logger.info(f"Сессия сохранена в {self.state_path}")

    def ensure_logged_in(self) -> None:
        """Проверяет сессию и при необходимости запускает ручной вход."""
        page = self.new_page()
        try:
            if self.state_path.exists() and is_authenticated(page):
                self.logger.info("Сессия hh.ru восстановлена, вход не требуется.")
                return
            if self.state_path.exists():
                self.logger.info("Сохранённая сессия hh.ru недействительна, нужен повторный вход.")
            else:
                self.logger.info("Сохранённая сессия не найдена, нужен первый вход в hh.ru.")
        finally:
            page.close()
        self.login_manually()

    def login_manually(self) -> None:
        """Открывает hh.ru и ждёт, пока пользователь войдёт в аккаунт вручную."""
        self._ensure_headed()
        page = self.new_page()
        try:
            page.goto(HH_HOME, wait_until="domcontentloaded")
            self.logger.info(
                "В открытом окне браузера войдите в аккаунт hh.ru. "
                "После входа вернитесь в консоль и нажмите Enter."
            )
            try:
                input("Нажмите Enter после входа в аккаунт hh.ru... ")
            except EOFError as exc:
                raise BrowserError(
                    "Не удалось дождаться подтверждения входа: консоль недоступна. "
                    "Запустите приложение из терминала."
                ) from exc

            if not is_authenticated(page):
                raise BrowserError(
                    "Вход в hh.ru не подтверждён. Запустите приложение снова и войдите в аккаунт."
                )
        finally:
            page.close()
        self.save_state()

    def close(self) -> None:
        try:
            self._close_current()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None


def is_authenticated(page: Page) -> bool:
    """Проверяет, что браузер авторизован на hh.ru."""
    try:
        page.goto(HH_RESUMES, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        if "login" in page.url.lower():
            return False
        if page.locator(_PROFILE_MARKERS).count() > 0:
            return True
        return page.locator(_LOGIN_MARKERS).count() == 0 and "резюме" in (page.title() or "").lower()
    except PlaywrightError:
        return False


def detect_captcha(page: Page) -> bool:
    """Определяет страницу с капчей."""
    try:
        if "captcha" in page.url.lower():
            return True
        for selector in ("[data-qa='captcha']", "#captcha", "iframe[src*='captcha']"):
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return True
    except PlaywrightError:
        return False
    return False


def save_screenshot(page: Page, path: Path) -> Path | None:
    """Сохраняет скриншот страницы для разбора ошибок."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
        return path
    except (PlaywrightError, OSError):
        return None
