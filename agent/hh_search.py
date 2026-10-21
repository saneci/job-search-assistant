"""Поиск вакансий на hh.ru и сбор карточек выдачи."""

from __future__ import annotations

import re
from typing import Sequence
from urllib.parse import urlencode, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .browser import NAVIGATION_TIMEOUT_MS, CaptchaError, PageLoadError, detect_captcha
from .config import SearchConfig
from .models import VacancyCard

SEARCH_URL = "https://hh.ru/search/vacancy"
HH_BASE_URL = "https://hh.ru"

# hh.ru отдаёт в вёрстке не больше 20 карточек на страницу, даже если просить
# items_on_page=50. Если отправить 50, hh сдвигает страницы по 50, а карточек
# рисует 20 — часть вакансий терялась. Поэтому просим ровно столько, сколько
# реально приходит в DOM, а глубину задаём через pages_limit.
HH_PAGE_SIZE = 20

TITLE_SELECTOR = "a[data-qa='serp-item__title'], a[data-qa='vacancy-serp__vacancy-title']"

_VACANCY_ID_RE = re.compile(r"/vacancy/(\d+)")

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
        "items_on_page": str(min(search.per_page, HH_PAGE_SIZE)),
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
    """Возвращает сработавшее стоп-слово в названии вакансии или None."""
    return match_keyword(title, keywords)


def is_company_excluded(company: str, keywords: Sequence[str]) -> str | None:
    """Возвращает сработавшее стоп-слово в названии компании или None."""
    return match_keyword(company, keywords)


def match_keyword(text: str, keywords: Sequence[str]) -> str | None:
    """Ищет ключевое слово без учёта регистра («ё» приравнивается к «е»)."""
    if not text:
        return None
    lowered = text.lower().replace("ё", "е")
    for keyword in keywords:
        normalized = keyword.strip().lower().replace("ё", "е")
        if normalized and normalized in lowered:
            return keyword
    return None


def vacancy_keys(title: str, company: str, url: str) -> tuple[str, ...]:
    """Ключи вакансии для дедупликации между запросами и страницами выдачи.

    Первый ключ — id вакансии из URL, второй — «название + компания». Второй
    нужен потому, что работодатели публикуют одну и ту же вакансию несколько раз
    (разные id, одинаковые название и компания).
    """
    keys: list[str] = []
    vid = vacancy_id(url)
    if vid:
        keys.append(f"id:{vid}")
    text = " ".join(f"{title} {company}".lower().replace("ё", "е").split())
    if text:
        keys.append(f"text:{text}")
    return tuple(keys)


def vacancy_id(url: str) -> str | None:
    """id вакансии из URL hh.ru вида /vacancy/123456."""
    match = _VACANCY_ID_RE.search(url or "")
    return match.group(1) if match else None


def open_search_page(
    page: Page,
    search: SearchConfig,
    query: str,
    page_index: int,
) -> tuple[list[VacancyCard], int | None]:
    """Открывает страницу выдачи и собирает карточки вакансий.

    Возвращает (карточки, сколько всего вакансий нашёл hh.ru).
    """
    url = build_search_url(search, query, page_index)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise PageLoadError(
            f"страница поиска не загрузилась за {NAVIGATION_TIMEOUT_MS // 1000} с: {url}"
        ) from exc
    page.wait_for_timeout(2000)
    if detect_captcha(page):
        raise CaptchaError("hh.ru показал капчу на странице поиска")
    _wait_for_results(page)
    return collect_cards(page), read_total(page)


TOTAL_RE = re.compile(r"Найден[оа]\s+([\d\s\u00a0]+)\s+ваканс")


def parse_total(html: str) -> int | None:
    """Извлекает из шапки выдачи число найденных вакансий («Найдено 44 вакансии»)."""
    text = (html or "").replace("&nbsp;", " ")
    match = TOTAL_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def read_total(page: Page) -> int | None:
    """Сколько всего вакансий нашёл hh.ru по запросу (для лога и диагностики)."""
    try:
        html = page.content()
    except PlaywrightError:
        return None
    return parse_total(html)


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
