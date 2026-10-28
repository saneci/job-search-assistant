"""Страница вакансии career.habr.com: разбор данных, проверки и отклик.

Отклик на Habr устроен иначе, чем на hh.ru: кнопка «Откликнуться» сразу создаёт
отклик, а сопроводительное письмо добавляется к уже созданному отклику кнопкой
«Дополнить отклик». Поэтому письмо обязано быть готово до клика — это
обеспечивает pipeline: Decision → Generation → respond(letter).
"""

from __future__ import annotations

import re
import time
from typing import Callable, Sequence

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .browser import NAVIGATION_TIMEOUT_MS, CaptchaError, PageLoadError, detect_captcha
from .habr_search import SUITABLE_URL
from .hh_vacancy import (
    ResponseResult,
    STATUS_ALREADY_RESPONDED,
    STATUS_CAPTCHA,
    STATUS_NO_FORM,
    STATUS_SENT,
    STATUS_UNKNOWN,
    VacancyParseError,
)
from .models import VacancyCard, VacancyDetails

HABR_BASE_URL = "https://career.habr.com"
# Ручной вход открываем на главной: кнопка «Войти» ведёт на account.habr.com.
LOGIN_URL = f"{HABR_BASE_URL}/"

TITLE_SELECTORS = ("h1.page-title__title", "h1")
DESCRIPTION_SELECTORS = (".vacancy-description__text", ".style-ugc")
COMPANY_SELECTORS = (".vacancy-company__title a.link-comp", ".vacancy-company__title a")
SALARY_SELECTORS = (".vacancy-header__salary",)
RESPOND_BUTTON_SELECTORS = (".vacancy-header__footer button",)
LETTER_SELECTORS = ("textarea[name='body']",)
SUBMIT_SELECTORS = ("button[type='submit']",)
EXTERNAL_RESPONSE_SELECTOR = "a[href$='/external_response']"
RESPONSE_SECTION_SELECTOR = ".vacancy-response-section"
RESPONSE_MESSAGE_SELECTOR = ".vacancy-response__message"

RESPOND_RE = re.compile(r"^\s*откликнуться\s*$", re.IGNORECASE)
SUBMIT_RE = re.compile(r"дополнить\s+отклик", re.IGNORECASE)
LOGIN_LINK = "Войти"

# Отрисовка после загрузки и ожидание формы отклика.
RENDER_WAIT_MS = 1500
WAIT_FORM_MS = 15000
WAIT_RESULT_SECONDS = 20.0
POLL_MS = 500


def open_vacancy(page: Page, card: VacancyCard) -> VacancyDetails:
    """Открывает страницу вакансии и извлекает данные."""
    try:
        page.goto(card.url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise PageLoadError(
            f"страница вакансии не загрузилась за {NAVIGATION_TIMEOUT_MS // 1000} с: {card.url}"
        ) from exc
    page.wait_for_timeout(RENDER_WAIT_MS)
    if detect_captcha(page):
        raise CaptchaError(f"career.habr.com показал капчу на странице вакансии: {card.url}")

    try:
        page.wait_for_selector(TITLE_SELECTORS[0], timeout=WAIT_FORM_MS)
    except PlaywrightTimeoutError:
        pass

    title = _first_text(page, TITLE_SELECTORS) or card.title
    description = _first_text(page, DESCRIPTION_SELECTORS)
    if not description:
        raise VacancyParseError(f"не удалось прочитать описание вакансии: {card.url}")
    return VacancyDetails(
        url=card.url,
        title=title,
        company=_first_text(page, COMPANY_SELECTORS) or card.company,
        salary=_first_text(page, SALARY_SELECTORS) or card.salary,
        description=description,
        already_responded=_has_response(page),
        blocker=detect_page_blocker(page),
    )


def detect_page_blocker(page: Page) -> str | None:
    """Отклик уходит на сайте компании: такой канал пока не поддерживается."""
    if _has_selector(page, EXTERNAL_RESPONSE_SELECTOR):
        return "external_form"
    return None


def respond(
    page: Page,
    letter: str,
    captcha_waiter: Callable[[], bool],
) -> ResponseResult:
    """Отправляет отклик и добавляет к нему готовое письмо.

    Письмо передаётся готовым: клик «Откликнуться» создаёт отклик сразу,
    поэтому LLM вызывается до этой функции, а не после клика.
    """
    if detect_captcha(page) and not captcha_waiter():
        return _result(STATUS_CAPTCHA)
    if _has_response(page):
        return _result(STATUS_ALREADY_RESPONDED)

    button = _find_action(page, RESPOND_BUTTON_SELECTORS, RESPOND_RE)
    if button is None:
        return _result(STATUS_NO_FORM, detect_page_blocker(page) or "")

    try:
        button.click()
    except PlaywrightError:
        return _result(STATUS_UNKNOWN)

    # Клик мог создать отклик: дальше любые сбои не отменяют факт отправки.
    box = _wait_for_element(page, LETTER_SELECTORS, captcha_waiter)
    if box is None:
        if detect_captcha(page):
            return _result(STATUS_CAPTCHA)
        if _has_response(page):
            return _result(STATUS_SENT, "отклик создан, но форма письма не открылась")
        return _result(STATUS_UNKNOWN)

    try:
        box.fill(letter)
    except PlaywrightError:
        return _result(STATUS_SENT, "отклик создан, но письмо заполнить не удалось")

    submit = _find_action(page, SUBMIT_SELECTORS, SUBMIT_RE)
    if submit is None:
        return _result(STATUS_SENT, "отклик создан, но кнопка «Дополнить отклик» не найдена")
    try:
        submit.click()
    except PlaywrightError:
        return _result(STATUS_SENT, "отклик создан, но письмо не отправлено")

    if _wait_for_result(page, captcha_waiter):
        return _result(STATUS_SENT)
    return _result(STATUS_SENT, "отклик создан, подтверждение добавления письма не получено")


def response_state(page: Page) -> dict[str, object]:
    """Диагностика формы отклика для журнала: без текста письма и данных профиля."""
    state: dict[str, object] = {"page_url": page.url}
    box = _first_visible(page, LETTER_SELECTORS)
    if box is not None:
        try:
            state["letter_length"] = len(box.input_value() or "")
        except PlaywrightError:
            pass
    return state


def is_authenticated(page: Page) -> bool:
    """Проверяет вход на Habr, не уводя пользователя с текущей страницы."""
    try:
        if "career.habr.com" not in page.url:
            page.goto(SUITABLE_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            page.wait_for_timeout(1000)
        if "account.habr.com" in page.url.lower():
            return False
        login = page.get_by_role("link", name=LOGIN_LINK).first
        return not _is_visible(login)
    except PlaywrightError:
        return False


def _wait_for_element(
    page: Page,
    selectors: Sequence[str],
    captcha_waiter: Callable[[], bool],
) -> Locator | None:
    """Ждёт появления элемента; капчу отдаёт человеку и продолжает ожидание."""
    deadline = time.monotonic() + WAIT_FORM_MS / 1000
    while time.monotonic() < deadline:
        element = _first_visible(page, selectors)
        if element is not None:
            return element
        if detect_captcha(page) and not captcha_waiter():
            return None
        page.wait_for_timeout(POLL_MS)
    return None


def _wait_for_result(page: Page, captcha_waiter: Callable[[], bool]) -> bool:
    """Ждёт подтверждение, что письмо добавлено к отклику."""
    deadline = time.monotonic() + WAIT_RESULT_SECONDS
    while time.monotonic() < deadline:
        page.wait_for_timeout(POLL_MS)
        if detect_captcha(page) and not captcha_waiter():
            return False
        if _has_selector(page, RESPONSE_MESSAGE_SELECTOR) and not _has_selector(
            page, LETTER_SELECTORS[0]
        ):
            return True
    return False


def _has_response(page: Page) -> bool:
    return _has_selector(page, RESPONSE_SECTION_SELECTOR) or _has_selector(
        page, RESPONSE_MESSAGE_SELECTOR
    )


def _find_action(
    page: Page,
    selectors: Sequence[str],
    name_pattern: re.Pattern[str],
) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        if _is_visible(locator):
            return locator
    try:
        locator = page.get_by_role("button", name=name_pattern).first
    except PlaywrightError:
        return None
    return locator if _is_visible(locator) else None


def _first_visible(scope: Locator | Page, selectors: Sequence[str]) -> Locator | None:
    for selector in selectors:
        locator = scope.locator(selector).first
        if _is_visible(locator):
            return locator
    return None


def _first_text(scope: Locator | Page, selectors: Sequence[str]) -> str:
    locator = _first_visible(scope, selectors)
    if locator is None:
        return ""
    try:
        return locator.inner_text().strip()
    except PlaywrightError:
        return ""


def _has_selector(scope: Locator | Page, selector: str) -> bool:
    try:
        return scope.locator(selector).count() > 0
    except PlaywrightError:
        return False


def _is_visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.is_visible()
    except PlaywrightError:
        return False


def _result(status: str, reason: str = "") -> ResponseResult:
    return ResponseResult(status=status, reason=reason)
