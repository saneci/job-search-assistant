"""Playwright: запуск Chromium, сохранение и восстановление сессии hh.ru."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)

from .config import BrowserConfig
from .logging_setup import RunLogger

HH_HOME = "https://hh.ru/"
HH_RESUMES = "https://hh.ru/applicant/resumes"

# Таймаут загрузки страницы hh.ru: страницы с тяжёлой версткой грузятся дольше обычных.
NAVIGATION_TIMEOUT_MS = 45000

_PROFILE_MARKERS = "[data-qa='mainmenu_applicantProfile'], [data-qa='mainmenu_avatar']"
_LOGIN_MARKERS = "a[href*='account/login'], [data-qa='login']"


class BrowserError(RuntimeError):
    """Ошибка работы с браузером или сессией hh.ru."""


class CaptchaError(BrowserError):
    """hh.ru показал капчу."""


class PageLoadError(RuntimeError):
    """Страница hh.ru не загрузилась за отведённое время."""


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
        if self.config.channel:
            self.logger.info(f"Используется установленный браузер: {self.config.channel}.")
        # Первый запуск (нет state.json) всегда с видимым браузером: нужен ручной вход.
        self._launch(headless=self.config.headless and self.state_path.exists())
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _launch(self, headless: bool) -> None:
        assert self._playwright is not None
        launch_options: dict[str, Any] = {
            "headless": headless,
            "slow_mo": self.config.slow_mo_ms,
        }
        if self.config.channel:
            launch_options["channel"] = self.config.channel
        self._browser = self._playwright.chromium.launch(**launch_options)
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
        """Закрывает контекст и браузер, не падая, если они уже закрыты."""
        context, browser = self._context, self._browser
        self._context = None
        self._browser = None
        for resource in (context, browser):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                # Окно могли закрыть вручную, браузер уже завершился или
                # соединение с драйвером оборвалось (например, по Ctrl+C).
                pass

    def is_alive(self) -> bool:
        return self._context is not None and self._browser is not None

    def new_page(self) -> Page:
        if self._context is None:
            raise BrowserError("Браузер не запущен")
        return self._context.new_page()

    def save_state(self) -> None:
        if self._context is None:
            raise BrowserError("Браузер не запущен")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = trim_storage_state(self._context.storage_state())
            self.state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            self.logger.event("session_save_error", error=str(exc))
            self.logger.info(f"Не удалось сохранить сессию: {exc}")
            return
        self.logger.event(
            "session_saved", path=str(self.state_path), size=self.state_path.stat().st_size
        )
        self.logger.info(f"Сессия сохранена в {self.state_path}")

    def ensure_logged_in(self) -> None:
        """Проверяет сессию и при необходимости запускает ручной вход."""
        self.logger.info("Проверяю сессию hh.ru...")
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
        authenticated = False
        try:
            page.goto(HH_HOME, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
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

            self.logger.info("Проверяю вход в hh.ru...")
            authenticated = is_authenticated(page)
            if authenticated:
                self.logger.info("Вход подтверждён.")
            else:
                self.logger.info(
                    "Не удалось подтвердить вход: похоже, вход в аккаунт не завершён."
                )
        finally:
            page.close()

        # Сессию сохраняем в любом случае: при повторном запуске она будет перепроверена.
        self.save_state()
        if not authenticated:
            raise BrowserError(
                "Вход в hh.ru не подтверждён. Войдите в аккаунт до нажатия Enter "
                "и запустите приложение снова."
            )

    def close(self) -> None:
        try:
            self._close_current()
        finally:
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None


def trim_storage_state(state: dict[str, Any]) -> dict[str, Any]:
    """Убирает из storage_state кэш шрифтов hh.ru.

    hh.ru складывает в localStorage шрифты страниц (``magritteFontsNewFont``,
    ``xhhFontsFont``, ``chatikFontsFont``) — это больше мегабайта payload,
    который для входа не нужен: при загрузке страницы браузер скачивает шрифты
    заново. Cookies и остальные ключи остаются без изменений.
    """
    trimmed = dict(state)
    origins: list[dict[str, Any]] = []
    for origin in state.get("origins", []):
        if not isinstance(origin, dict):
            continue
        copy = dict(origin)
        copy["localStorage"] = [
            item for item in origin.get("localStorage", []) if not _is_font_entry(item)
        ]
        origins.append(copy)
    trimmed["origins"] = origins
    return trimmed


def _is_font_entry(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    return str(item.get("name", "")).endswith("Font")


def is_authenticated(page: Page) -> bool:
    """Проверяет авторизацию hh.ru, не уводя пользователя с текущей страницы."""
    try:
        if "hh.ru" not in page.url:
            page.goto(HH_RESUMES, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            page.wait_for_timeout(1500)

        url = page.url.lower()
        if "login" in url:
            return False
        # Разделы /applicant/ доступны только авторизованным пользователям:
        # без сессии hh.ru перенаправляет на /account/login.
        if "/applicant/" in url:
            return True
        if _is_visible(page.locator(_PROFILE_MARKERS).first):
            return True
        # Признак неавторизованного состояния — видимая ссылка «Войти».
        return not _is_visible(page.locator(_LOGIN_MARKERS).first)
    except PlaywrightError:
        return False


def _is_visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.is_visible()
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
