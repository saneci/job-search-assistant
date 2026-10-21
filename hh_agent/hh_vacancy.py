"""Страница вакансии hh.ru: разбор данных, проверки и отправка отклика."""

from __future__ import annotations

import re
from typing import Sequence
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .browser import CaptchaError, detect_captcha
from .models import VacancyCard, VacancyDetails

TITLE_SELECTORS = ("h1[data-qa='vacancy-title']", "h1.bloko-header-1", "h1")
DESCRIPTION_SELECTORS = (
    "[data-qa='vacancy-description']",
    "div.vacancy-description",
    "[data-qa='vacancy-description-content']",
)
COMPANY_SELECTORS = (
    "[data-qa='vacancy-company-name']",
    "a[data-qa='vacancy-company-name']",
    "[data-qa='vacancy-company-name'] a",
)
SALARY_SELECTORS = (
    "[data-qa='vacancy-salary']",
    "[data-qa='vacancy-compensation']",
)

RESPOND_BUTTON_SELECTORS = (
    "a[data-qa='vacancy-response-link-top']",
    "button[data-qa='vacancy-response-link-top']",
    "a[data-qa='vacancy-response-link']",
)
SUBMIT_SELECTORS = (
    "[data-qa='vacancy-response-submit-popup']",
    "[data-qa='vacancy-response-submit']",
)
LETTER_TOGGLE_SELECTORS = ("[data-qa='vacancy-response-letter-toggle']",)
LETTER_TEXTAREA_SELECTORS = (
    "textarea[data-qa='vacancy-response-popup-form-letter-input']",
    "textarea[data-qa='vacancy-response-letter-input']",
    "textarea[name='letter']",
    "textarea",
)
MODAL_SELECTORS = ("[data-qa='modal-content']", "[role='dialog']")

STATUS_SENT = "sent"
STATUS_ALREADY_RESPONDED = "already_responded"
STATUS_CAPTCHA = "captcha"
STATUS_NO_FORM = "no_form"
STATUS_UNKNOWN = "unknown"

RESPOND_RE = re.compile(r"^\s*откликнуться\s*$", re.IGNORECASE)
LETTER_TOGGLE_RE = re.compile(r"добавить сопроводительное", re.IGNORECASE)
TEST_ACTION_RE = re.compile(r"пройти тест|начать тест|пройти тестирование", re.IGNORECASE)
QUESTIONNAIRE_ACTION_RE = re.compile(
    r"заполнить анкету|пройти анкет|ответить на вопросы", re.IGNORECASE
)
EXTERNAL_ACTION_RE = re.compile(r"на сайте работодателя|перейти на сайт", re.IGNORECASE)

QUESTIONNAIRE_SELECTORS = (
    "[data-qa='vacancy-response-questionnaire']",
    "a[href*='questionnaire']",
)
TEST_SELECTORS = ("[data-qa='vacancy-response-test']", "[data-qa='vacancy-test']")


class VacancyParseError(RuntimeError):
    """Не удалось разобрать страницу вакансии."""


def open_vacancy(page: Page, card: VacancyCard) -> VacancyDetails:
    """Открывает страницу вакансии и извлекает данные."""
    page.goto(card.url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if detect_captcha(page):
        raise CaptchaError(f"hh.ru показал капчу на странице вакансии: {card.url}")

    try:
        page.wait_for_selector(", ".join(TITLE_SELECTORS), timeout=15000)
    except PlaywrightTimeoutError:
        pass

    title = _first_text(page, TITLE_SELECTORS) or card.title
    company = _first_text(page, COMPANY_SELECTORS) or card.company
    salary = _first_text(page, SALARY_SELECTORS) or card.salary
    if salary and "не указан" in salary.lower():
        salary = ""

    description = _first_text(page, DESCRIPTION_SELECTORS)
    if not description:
        raise VacancyParseError(f"Не удалось извлечь описание вакансии: {card.url}")

    return VacancyDetails(
        url=card.url,
        title=title,
        company=company.splitlines()[0].strip() if company else "",
        salary=salary,
        description=description,
        already_responded=_is_already_responded(page),
        blocker=detect_page_blocker(page),
    )


def detect_page_blocker(page: Page) -> str | None:
    """Ищет на странице вакансии признаки теста, анкеты или внешней формы."""
    if _has_selector(page, QUESTIONNAIRE_SELECTORS) or _has_role_name(page, QUESTIONNAIRE_ACTION_RE):
        return "questionnaire"
    if _has_selector(page, TEST_SELECTORS) or _has_role_name(page, TEST_ACTION_RE):
        return "test"
    if _has_role_name(page, EXTERNAL_ACTION_RE):
        return "external_form"
    return None


def detect_response_blocker(scope: Locator | Page) -> str | None:
    """Ищет признаки теста/анкеты/внешней формы в форме отклика."""
    if _has_selector(scope, QUESTIONNAIRE_SELECTORS) or _has_role_name(scope, QUESTIONNAIRE_ACTION_RE):
        return "questionnaire"
    if _has_selector(scope, TEST_SELECTORS) or _has_role_name(scope, TEST_ACTION_RE):
        return "test"
    if _has_role_name(scope, EXTERNAL_ACTION_RE):
        return "external_form"
    if not isinstance(scope, Page) and _has_external_link(scope):
        return "external_form"
    return None


def respond(page: Page, letter: str) -> str:
    """Отправляет отклик с сопроводительным письмом. Возвращает статус."""
    if detect_captcha(page):
        return STATUS_CAPTCHA

    button = _find_action(page, RESPOND_BUTTON_SELECTORS, RESPOND_RE)
    if button is None:
        return STATUS_ALREADY_RESPONDED if _is_already_responded(page) else STATUS_NO_FORM

    try:
        button.click()
    except PlaywrightError:
        return STATUS_UNKNOWN
    page.wait_for_timeout(2000)
    if detect_captcha(page):
        return STATUS_CAPTCHA

    scope = _response_scope(page)
    blocker = detect_response_blocker(scope)
    if blocker:
        _dismiss(page)
        return blocker

    _fill_letter(page, scope, letter)

    submit = _find_action(scope, SUBMIT_SELECTORS, RESPOND_RE)
    if submit is None:
        _dismiss(page)
        return STATUS_NO_FORM

    try:
        submit.click()
    except PlaywrightError:
        return STATUS_UNKNOWN
    page.wait_for_timeout(2500)
    if detect_captcha(page):
        return STATUS_CAPTCHA
    if _is_already_responded(page) or _modal_hidden(page):
        return STATUS_SENT
    return STATUS_UNKNOWN


def _fill_letter(page: Page, scope: Locator | Page, letter: str) -> bool:
    textarea = _first_visible(scope, LETTER_TEXTAREA_SELECTORS)
    if textarea is None:
        toggle = _find_action(scope, LETTER_TOGGLE_SELECTORS, LETTER_TOGGLE_RE)
        if toggle is not None:
            try:
                toggle.click()
                page.wait_for_timeout(1000)
            except PlaywrightError:
                pass
            textarea = _first_visible(scope, LETTER_TEXTAREA_SELECTORS)
    if textarea is None:
        return False
    try:
        textarea.click()
        textarea.fill(letter)
        textarea.dispatch_event("input")
        textarea.dispatch_event("change")
        return True
    except PlaywrightError:
        return False


def _response_scope(page: Page) -> Locator | Page:
    for selector in MODAL_SELECTORS:
        locator = page.locator(selector).first
        if _is_visible(locator):
            return locator
    return page


def _modal_hidden(page: Page) -> bool:
    return not any(_is_visible(page.locator(selector).first) for selector in MODAL_SELECTORS)


def _dismiss(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
    except PlaywrightError:
        pass
    if _modal_hidden(page):
        return
    close = page.locator("[data-qa='modal-close']").first
    if _is_visible(close):
        try:
            close.click()
        except PlaywrightError:
            pass


def _is_already_responded(page: Page) -> bool:
    for text in ("Отклик отправлен", "Вы откликнулись"):
        try:
            if _is_visible(page.get_by_text(text, exact=False).first):
                return True
        except PlaywrightError:
            continue
    return False


def _find_action(
    scope: Locator | Page,
    selectors: Sequence[str],
    name_pattern: re.Pattern[str],
) -> Locator | None:
    for selector in selectors:
        locator = scope.locator(selector).first
        if _is_visible(locator):
            return locator
    for role in ("button", "link"):
        try:
            locator = scope.get_by_role(role, name=name_pattern).first
        except PlaywrightError:
            continue
        if _is_visible(locator):
            return locator
    return None


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


def _is_visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.is_visible()
    except PlaywrightError:
        return False


def _has_selector(scope: Locator | Page, selectors: Sequence[str]) -> bool:
    for selector in selectors:
        try:
            if scope.locator(selector).count() > 0:
                return True
        except PlaywrightError:
            continue
    return False


def _has_role_name(scope: Locator | Page, name_pattern: re.Pattern[str]) -> bool:
    for role in ("button", "link"):
        try:
            if scope.get_by_role(role, name=name_pattern).count() > 0:
                return True
        except PlaywrightError:
            continue
    return False


def _has_external_link(scope: Locator | Page) -> bool:
    try:
        links = scope.locator("a[href^='http']")
        total = min(links.count(), 50)
    except PlaywrightError:
        return False
    for index in range(total):
        try:
            href = links.nth(index).get_attribute("href") or ""
        except PlaywrightError:
            continue
        host = urlparse(href).netloc.lower()
        if host and "hh.ru" not in host and "headhunter" not in host:
            return True
    return False
