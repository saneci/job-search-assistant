"""Поиск вакансий на hh.ru и сбор карточек выдачи."""

from __future__ import annotations

from typing import Sequence
from urllib.parse import urlencode, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .browser import CaptchaError, detect_captcha
from .config import SearchConfig
from .models import VacancyCard

SEARCH_URL = "https://hh.ru/search/vacancy"
HH_BASE_URL = "https://hh.ru"

TITLE_SELECTOR = "a[data-qa='serp-item__title'], a[data-qa='vacancy-serp__vacancy-title']"

EMPLOYER_SELECTORS = (
    "[data-qa='vacancy-serp__vacancy-employer']",
    "[data-qa='vacancy-serp__vacancy-employer-text']",
    "[data-qa='vacancy-serp-item__meta-info-company']",
)

SALARY_SELECTORS = (
    "[data-qa='vacancy-serp__vacancy-compensation']",
    "[data-qa='vacancy-serp-item__compensation']",
)

CARD_XPATHS = (
    "xpath=ancestor::div[@data-qa='vacancy-serp-item__layout'][1]",
    "xpath=ancestor::div[contains(@data-qa, 'vacancy-serp')][1]",
    "xpath=ancestor::li[1]",
)


def build_search_url(search: SearchConfig, query: str, page_index: int = 0) -> str:
    """Собирает URL поиска hh.ru с фильтрами из конфигурации."""
    params: dict[str, str] = {
        "text": query,
        "area": str(search.area_id),
        "page": str(page_index),
        "items_on_page": str(search.per_page),
    }
    if search.salary_from is not None:
        params["salary"] = str(search.salary_from)
        params["only_with_salary"] = "true"
    if search.experience:
        params["experience"] = search.experience
    if search.schedule:
        params["schedule"] = search.schedule
    return f"{SEARCH_URL}?{urlencode(params)}"


def is_excluded(title: str, keywords: Sequence[str]) -> str | None:
    """Возвращает сработавшее стоп-слово или None."""
    lowered = title.lower().replace("ё", "е")
    for keyword in keywords:
        normalized = keyword.strip().lower().replace("ё", "е")
        if normalized and normalized in lowered:
            return keyword
    return None


def open_search_page(
    page: Page,
    search: SearchConfig,
    query: str,
    page_index: int,
) -> list[VacancyCard]:
    """Открывает страницу выдачи и собирает карточки вакансий."""
    url = build_search_url(search, query, page_index)
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    if detect_captcha(page):
        raise CaptchaError("hh.ru показал капчу на странице поиска")
    _wait_for_results(page)
    return collect_cards(page)


def collect_cards(page: Page) -> list[VacancyCard]:
    """Собирает карточки вакансий с текущей страницы выдачи."""
    links = page.locator(TITLE_SELECTOR)
    try:
        total = links.count()
    except PlaywrightError:
        return []

    cards: list[VacancyCard] = []
    seen: set[str] = set()
    for index in range(total):
        link = links.nth(index)
        try:
            title = link.inner_text().strip()
            href = (link.get_attribute("href") or "").strip()
        except PlaywrightError:
            continue
        if not title or not href:
            continue

        url = urljoin(HH_BASE_URL, href.split("?")[0])
        if url in seen:
            continue
        seen.add(url)

        container = _card_container(link)
        company = _first_text(container, EMPLOYER_SELECTORS)
        salary = _first_text(container, SALARY_SELECTORS)
        cards.append(
            VacancyCard(
                title=title,
                company=_clean_company(company),
                url=url,
                salary=salary,
            )
        )
    return cards


def _wait_for_results(page: Page) -> None:
    try:
        page.wait_for_selector(TITLE_SELECTOR, timeout=15000)
    except PlaywrightTimeoutError:
        # Пустая выдача или другая вёрстка: карточек просто не будет.
        return


def _card_container(link: Locator) -> Locator | None:
    for xpath in CARD_XPATHS:
        try:
            container = link.locator(xpath)
            if container.count() > 0:
                return container.first
        except PlaywrightError:
            continue
    return None


def _first_text(container: Locator | None, selectors: Sequence[str]) -> str:
    if container is None:
        return ""
    for selector in selectors:
        try:
            locator = container.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                text = locator.inner_text().strip()
                if text:
                    return text
        except PlaywrightError:
            continue
    return ""


def _clean_company(value: str) -> str:
    """Убирает из названия компании рейтинг и число отзывов."""
    line = value.splitlines()[0].strip() if value else ""
    return line
