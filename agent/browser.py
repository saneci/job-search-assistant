"""Playwright: запуск браузера, сохранение и восстановление сессии hh.ru и Habr."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

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

# Постоянный профиль Chrome подменяет признак автоматизации: без этого hCaptcha
# на входе в Habr не отрисовывается и ручной вход невозможен.
HIDE_WEBDRIVER = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"

# Таймаут загрузки страницы hh.ru: страницы с тяжёлой версткой грузятся дольше обычных.
NAVIGATION_TIMEOUT_MS = 45000

# Скриншот не должен подвешивать запуск: hh.ru иногда не даёт стабильный кадр.
SCREENSHOT_TIMEOUT_MS = 15000

_PROFILE_MARKERS = "[data-qa='mainmenu_applicantProfile'], [data-qa='mainmenu_avatar']"
_LOGIN_MARKERS = "a[href*='account/login'], [data-qa='login']"

# Черновики сопроводительных писем hh.ru в localStorage: в файле сессии не нужны.
LETTER_DRAFT_PREFIX = "vacancy_response_letter_"

# Текст капчи hh.ru: отдельная страница и модалка «Пройдите капчу» в форме отклика.
CAPTCHA_TEXT_RE = re.compile(
    r"пройдите капчу|введите текст с картинки|подтвердите, что вы не робот",
    re.IGNORECASE,
)


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
        profile_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.state_path = state_path
        self.logger = logger
        # Постоянный профиль нужен каналу Habr: в одноразовом контексте hCaptcha
        # не отрисовывается. Для hh.ru профиль не используется (профиль = None).
        self.profile_dir = profile_dir
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
            # Без этой опции Playwright добавляет --no-sandbox и браузер
            # предупреждает, что стабильность и безопасность нарушены.
            "chromium_sandbox": self.config.chromium_sandbox,
        }
        if self.config.channel:
            launch_options["channel"] = self.config.channel
        if self.profile_dir is not None:
            self._launch_persistent(launch_options)
            self._headless = headless
            return
        self._browser = self._playwright.chromium.launch(**launch_options)
        context_options: dict[str, Any] = {
            "locale": "ru-RU",
            "viewport": {"width": 1680, "height": 1000},
        }
        if self.state_path.exists():
            context_options["storage_state"] = str(self.state_path)
        self._context = self._browser.new_context(**context_options)
        self._headless = headless

    def _launch_persistent(self, launch_options: dict[str, Any]) -> None:
        """Запускает постоянный профиль Chrome без признаков автоматизации."""
        assert self._playwright is not None and self.profile_dir is not None
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        context_options: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "ignore_default_args": ["--enable-automation"],
            "args": ["--disable-blink-features=AutomationControlled"],
            "locale": "ru-RU",
            "viewport": {"width": 1680, "height": 1000},
        }
        self._context = self._playwright.chromium.launch_persistent_context(
            **launch_options, **context_options
        )
        self._context.add_init_script(HIDE_WEBDRIVER)
        self._browser = None
        self._restore_cookies()

    def _restore_cookies(self) -> None:
        """Переносит cookie из файла сессии в постоянный профиль.

        `launch_persistent_context` не принимает `storage_state`, а cookie
        из сохранённого файла нужны, чтобы не входить заново после смены профиля.
        """
        if self._context is None or not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies") or []
            if cookies:
                self._context.add_cookies(cookies)
        except (OSError, ValueError, PlaywrightError) as exc:
            self.logger.event("session_restore_error", error=str(exc))

    def _ensure_headed(self) -> None:
        if not self._headless:
            return
        self.logger.info("Перезапускаю браузер с окном: требуется ручной вход.")
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
        if self._context is None:
            return False
        return self._browser is not None or self.profile_dir is not None

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

    def ensure_logged_in(
        self,
        check: Callable[[Page], bool] | None = None,
        login_url: str | None = None,
        name: str = "hh.ru",
    ) -> None:
        """Проверяет сессию сайта и при необходимости запускает ручной вход.

        Без `check` поведение прежнее (hh.ru): проверка и тексты про hh.ru.
        `check` позволяет проверить сессию другого сайта (Habr).
        """
        authenticate = check or is_authenticated
        state_exists = self.state_path.exists()
        # Для постоянного профиля файл сессии не обязателен: cookie живут в профиле.
        ready = state_exists or check is not None
        self.logger.info(f"Проверяю сессию {name}...")
        page = self.new_page()
        try:
            if ready and authenticate(page):
                self.logger.info(f"Сессия {name} восстановлена, вход не требуется.")
                return
            if state_exists:
                self.logger.info(
                    f"Сохранённая сессия {name} недействительна, нужен повторный вход."
                )
            else:
                self.logger.info(f"Сохранённая сессия не найдена, нужен первый вход в {name}.")
        finally:
            page.close()
        self.login_manually(login_url, check, name)

    def login_manually(
        self,
        url: str | None = None,
        check: Callable[[Page], bool] | None = None,
        name: str = "hh.ru",
    ) -> None:
        """Открывает сайт и ждёт, пока пользователь войдёт в аккаунт вручную."""
        authenticate = check or is_authenticated
        target = url or HH_HOME
        self._ensure_headed()
        page = self.new_page()
        authenticated = False
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            self.logger.info(
                f"В открытом окне браузера войдите в аккаунт {name}. "
                "После входа вернитесь в консоль и нажмите Enter."
            )
            try:
                input(f"Нажмите Enter после входа в аккаунт {name}... ")
            except EOFError as exc:
                raise BrowserError(
                    "Не удалось дождаться подтверждения входа: консоль недоступна. "
                    "Запустите приложение из терминала."
                ) from exc

            self.logger.info(f"Проверяю вход в {name}...")
            authenticated = authenticate(page)
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
    """Убирает из storage_state кэш шрифтов и черновики писем hh.ru.

    hh.ru складывает в localStorage шрифты страниц (``magritteFontsNewFont``,
    ``xhhFontsFont``, ``chatikFontsFont``) — это больше мегабайта payload,
    который для входа не нужен: при загрузке страницы браузер скачивает шрифты
    заново. Ещё выкидываем черновики сопроводительных писем
    (``vacancy_response_letter_<id>``) — текст письма в файле сессии не храним.
    Cookies и остальные ключи остаются без изменений.
    """
    trimmed = dict(state)
    origins: list[dict[str, Any]] = []
    for origin in state.get("origins", []):
        if not isinstance(origin, dict):
            continue
        copy = dict(origin)
        copy["localStorage"] = [
            item
            for item in origin.get("localStorage", [])
            if not _is_font_entry(item) and not _is_letter_draft(item)
        ]
        origins.append(copy)
    trimmed["origins"] = origins
    return trimmed


def _is_font_entry(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    return str(item.get("name", "")).endswith("Font")


def _is_letter_draft(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    return str(item.get("name", "")).startswith(LETTER_DRAFT_PREFIX)


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
    """Определяет капчу: отдельную страницу или диалог поверх формы отклика.

    hh.ru показывает капчу двумя способами: страница с `captcha` в адресе и
    модалка «Пройдите капчу» внутри формы отклика (её видно только по тексту).
    """
    try:
        if "captcha" in page.url.lower():
            return True
        for selector in (
            "[data-qa='captcha']",
            "#captcha",
            "iframe[src*='captcha']",
            "iframe[src*='hcaptcha']",
            "[id^='h-captcha']",
            "textarea[name='h-captcha-response']",
            "[data-qa*='captcha']",
            "input[name*='captcha']",
            "img[alt*='капч']",
        ):
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return True
        for selector in ("[role='dialog']", "[data-qa='modal-content']"):
            dialog = page.locator(selector).first
            if dialog.count() > 0 and dialog.is_visible():
                if CAPTCHA_TEXT_RE.search(dialog.inner_text() or ""):
                    return True
    except PlaywrightError:
        return False
    return False


def wait_for_captcha_solved(page: Page, seconds: int) -> bool:
    """Ждёт, пока человек решит капчу в открытом окне браузера.

    Возвращает True, если капча исчезла (человек ввёл текст и отправил форму).
    Обход капчи не выполняется: приложение только ждёт ручного действия.
    """
    if seconds <= 0:
        return False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        if not detect_captcha(page):
            return True
    return False


def save_screenshot(page: Page, path: Path, timeout: int = SCREENSHOT_TIMEOUT_MS) -> Path | None:
    """Сохраняет скриншот страницы для разбора ошибок."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), timeout=timeout)
        return path
    except (PlaywrightError, OSError):
        return None
