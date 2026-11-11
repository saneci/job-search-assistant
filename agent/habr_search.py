"""Поиск вакансий на career.habr.com: лента «Подходящие» и сбор карточек.

У Habr нет поиска по произвольным запросам: авторизованному пользователю
показывается готовая лента «Подходящие», построенная по его профилю. Поэтому
`search.queries`, `area`, `experience` и `schedule` к этому каналу не относятся.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .browser import NAVIGATION_TIMEOUT_MS, CaptchaError, PageLoadError, detect_captcha
from .models import (
    STATUS_INVITE,
    STATUS_OFFER,
    STATUS_REJECTED,
    STATUS_SENT,
    VacancyCard,
)

HABR_BASE_URL = "https://career.habr.com"
SUITABLE_URL = f"{HABR_BASE_URL}/vacancies?type=suitable"
RESPONSES_URL = f"{HABR_BASE_URL}/responses"

# Habr отдаёт ровно 25 карточек на страницу, размер страницы не настраивается.
HABR_PER_PAGE = 25
# Предохранитель: глубже этого ленту не листаем даже по счётчику найденных.
HABR_MAX_PAGES = 50

CARD_SELECTOR = "div.vacancy-card[data-vacancy-id]"
TITLE_SELECTOR = ".vacancy-card__title-link"
COMPANY_SELECTOR = ".vacancy-card__company a.link-comp"
SALARY_SELECTOR = ".vacancy-card__salary"
TOTAL_SELECTOR = ".search-total"
BACKDROP_SELECTOR = "a.vacancy-card__backdrop-link"
NO_SALARY_MARKER = "vacancy-card__meta--noSalary"
# Признак уже отправленного отклика прямо на карточке ленты.
SUCCESS_CHIP_SELECTOR = ".success-chip"
ALREADY_RESPONDED_TEXT = "Вы откликнулись"

# Страница откликов: здесь Habr показывает, прочитан отклик, пришёл отказ
# или приглашение. В карточке вакансии этого статуса нет.
RESPONSES_ROW_SELECTOR = "table tr"
RESPONSES_TITLE_SELECTOR = ".title a"
RESPONSES_COMPANY_SELECTOR = ".comapny a"
RESPONSES_STATUS_SELECTOR = ".status"
# Предохранитель от бесконечной пагинации: даже по 25 откликов на страницу
# двадцать страниц покрывают пятьсот откликов.
RESPONSES_MAX_PAGES = 20

# Сколько ждём отрисовку ленты после загрузки страницы.
RENDER_WAIT_MS = 1500

_VACANCY_ID_RE = re.compile(r"/vacancies/(\d+)")
_TOTAL_RE = re.compile(r"Найдено\s+([\d\s\u00a0]+)\s+ваканс", re.IGNORECASE)


@dataclass(frozen=True)
class HabrResponse:
    """Строка страницы откликов: вакансия, подпись статуса и наш статус."""

    url: str
    title: str
    company: str
    label: str
    status: str


def response_status(label: str) -> str:
    """Переводит подпись Habr в статус отчёта; неизвестную подпись не меняем."""
    text = " ".join((label or "").lower().replace("ё", "е").split())
    if "отказ" in text:
        return STATUS_REJECTED
    if "приглаш" in text:
        return STATUS_INVITE
    if "оффер" in text or "предложен" in text:
        return STATUS_OFFER
    if "отправлен" in text or "прочитано" in text or "просмотр" in text:
        return STATUS_SENT
    return ""


def build_search_url(page_index: int = 0) -> str:
    """URL ленты «Подходящие»; страница 0 — без параметра page."""
    if page_index <= 0:
        return SUITABLE_URL
    return f"{SUITABLE_URL}&page={page_index + 1}"


def parse_total(text: str) -> int | None:
    """Число найденных из «Найдено 168 вакансий, которые подходят именно вам»."""
    match = _TOTAL_RE.search(text or "")
    if match is None:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def pages_plan(total: int | None) -> int:
    """Сколько страниц ленты обойти: по счётчику, а без него — до пустой страницы."""
    if not total:
        return HABR_MAX_PAGES
    pages = -(-total // HABR_PER_PAGE)
    return max(1, min(pages, HABR_MAX_PAGES))


def vacancy_id(url: str) -> str | None:
    """id вакансии из URL career.habr.com вида /vacancies/1000167337."""
    match = _VACANCY_ID_RE.search(url or "")
    return match.group(1) if match else None


def open_responses_page(page: Page, page_index: int = 0) -> list[HabrResponse]:
    """Открывает страницу откликов Habr и читает строки таблицы.

    Страница одна на пользователя, но при большом числе откликов Habr
    включает пагинацию. Номер страницы начинается с нуля.
    """
    url = RESPONSES_URL if page_index <= 0 else f"{RESPONSES_URL}?page={page_index + 1}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise PageLoadError(f"Страница откликов не загрузилась: {url}") from exc
    page.wait_for_timeout(RENDER_WAIT_MS)
    if detect_captcha(page):
        raise CaptchaError(f"career.habr.com показал капчу на странице откликов: {page.url}")
    try:
        page.wait_for_selector("table", timeout=5000)
    except PlaywrightTimeoutError:
        # Пустой список откликов — нормальный конец, а не ошибка загрузки.
        pass
    return collect_responses(page)


def collect_responses(page: Page) -> list[HabrResponse]:
    """Собирает отклики: ссылку, название, компанию и подпись статуса."""
    try:
        rows = page.locator(RESPONSES_ROW_SELECTOR)
        total = rows.count()
    except PlaywrightError as exc:
        raise PageLoadError(f"Не удалось прочитать страницу откликов: {exc}") from exc

    responses: list[HabrResponse] = []
    for index in range(1, total):
        row = rows.nth(index)
        try:
            cells = row.locator("td")
            if cells.count() < 3:
                continue
            link = cells.nth(0).locator(RESPONSES_TITLE_SELECTOR).first
            href = link.get_attribute("href") or ""
            title = link.inner_text().strip()
        except PlaywrightError:
            continue
        if not href:
            continue
        url = urljoin(HABR_BASE_URL, href.split("?")[0])
        label = _card_text(cells.nth(2), RESPONSES_STATUS_SELECTOR)
        responses.append(
            HabrResponse(
                url=url,
                title=title,
                company=_card_text(cells.nth(0), RESPONSES_COMPANY_SELECTOR),
                label=label,
                status=response_status(label),
            )
        )
    return responses


def open_search_page(
    page: Page,
    query: str,
    page_index: int,
) -> tuple[list[VacancyCard], int | None]:
    """Открывает страницу ленты «Подходящие» и собирает карточки.

    `query` не используется: лента одна на пользователя, поисковых запросов нет.
    """
    del query
    url = build_search_url(page_index)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise PageLoadError(f"Страница вакансий не загрузилась: {url}") from exc
    page.wait_for_timeout(RENDER_WAIT_MS)
    if detect_captcha(page):
        raise CaptchaError(f"career.habr.com показал капчу: {page.url}")
    try:
        page.wait_for_selector(CARD_SELECTOR, timeout=5000)
    except PlaywrightTimeoutError:
        # Пустая лента — это нормальный конец выдачи, а не ошибка загрузки.
        pass
    return collect_cards(page), read_total(page)


def read_total(page: Page) -> int | None:
    """Сколько вакансий нашёл сайт; None — счётчик не показан."""
    try:
        locator = page.locator(TOTAL_SELECTOR).first
        if locator.count() == 0:
            return None
        return parse_total(locator.inner_text())
    except PlaywrightError:
        return None


def collect_cards(page: Page) -> list[VacancyCard]:
    """Собирает карточки ленты: заголовок, компания, зарплата, признак внешнего отклика."""
    try:
        locator = page.locator(CARD_SELECTOR)
        total = locator.count()
    except PlaywrightError as exc:
        raise PageLoadError(f"Не удалось прочитать выдачу: {exc}") from exc

    cards: list[VacancyCard] = []
    for index in range(total):
        card = locator.nth(index)
        url = _card_url(card)
        title = _card_text(card, TITLE_SELECTOR) or _backdrop_label(card)
        if not url or not title:
            continue
        salary = "" if _has_selector(card, f".{NO_SALARY_MARKER}") else _card_text(
            card, SALARY_SELECTOR
        )
        cards.append(
            VacancyCard(
                title=title,
                company=_card_text(card, COMPANY_SELECTOR),
                url=url,
                salary=salary,
                already_responded=_already_responded(card),
            )
        )
    return cards


def _card_url(card: Locator) -> str:
    """Ссылка на вакансию без query-параметров."""
    try:
        link = card.locator(BACKDROP_SELECTOR).first
        href = link.get_attribute("href") or ""
    except PlaywrightError:
        return ""
    if not href:
        return ""
    return urljoin(HABR_BASE_URL, href.split("?")[0])


def _backdrop_label(card: Locator) -> str:
    """Запасной источник заголовка: aria-label ссылки-подложки."""
    try:
        return (card.locator(BACKDROP_SELECTOR).first.get_attribute("aria-label") or "").strip()
    except PlaywrightError:
        return ""


def _card_text(card: Locator, selector: str) -> str:
    try:
        locator = card.locator(selector).first
        if locator.count() == 0 or not locator.is_visible():
            return ""
        return locator.inner_text().strip()
    except PlaywrightError:
        return ""


def _has_selector(card: Locator, selector: str) -> bool:
    try:
        return card.locator(selector).count() > 0
    except PlaywrightError:
        return False


def _already_responded(card: Locator) -> bool:
    """На карточке уже есть наш отклик: чип «Вы откликнулись» или его текст."""
    if _has_selector(card, SUCCESS_CHIP_SELECTOR):
        return True
    try:
        text = card.inner_text()
    except PlaywrightError:
        return False
    return ALREADY_RESPONDED_TEXT.casefold() in text.casefold()
