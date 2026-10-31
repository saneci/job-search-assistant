"""Поиск вакансий на hh.ru и сбор карточек выдачи."""

from __future__ import annotations

import re
import time
from typing import Sequence
from urllib.parse import urlencode, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .browser import NAVIGATION_TIMEOUT_MS, CaptchaError, PageLoadError, detect_captcha
from .config import SearchConfig
from .models import VacancyCard

SEARCH_URL = "https://hh.ru/search/vacancy"
HH_BASE_URL = "https://hh.ru"

# Глубина выдачи: больше 50 страниц hh.ru не отдаёт ни по одному запросу.
# Это только предохранитель — обычно число страниц считаем по «Найдено N».
HH_MAX_PAGES = 50

# hh.ru рисует карточки по мере прокрутки: в DOM сразу появляется первая
# двадцатка, остальные из items_on_page подгружаются порциями по 20.
SCROLL_PAUSE_MS = 500
# Сколько секунд ждём прироста карточек, прежде чем считать, что выдача этой
# страницы закончилась. Порция приходит не мгновенно: при ожидании в 1,2 с
# (3 прокрутки по 400 мс) медленная страница обрезалась до 20 карточек.
SCROLL_STABLE_SECONDS = 8.0
# Предохранитель на всю страницу: даже если карточки всё приходят и приходят.
SCROLL_TIMEOUT_SECONDS = 90.0
# Пустая страница: ждать нечего, но пару секунд на «а вдруг отрисуется» даём.
SCROLL_EMPTY_SECONDS = 2.0

TITLE_SELECTOR = "a[data-qa='serp-item__title'], a[data-qa='vacancy-serp__vacancy-title']"

# Один шаблон на оба канала: hh.ru — /vacancy/<id>, Habr — /vacancies/<id>.
_VACANCY_ID_RE = re.compile(r"/vacanc(?:y|ies)/(\d+)")

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
    """Собирает URL поиска hh.ru с фильтрами из конфигурации.

    Множественные значения фильтра уходят повторяющимися параметрами
    (``experience=a&experience=b``): hh.ru отмечает по ним несколько чекбоксов.
    """
    params: list[tuple[str, str]] = [
        ("text", query),
        ("area", str(search.area_id)),
        ("page", str(page_index)),
        ("items_on_page", str(search.per_page)),
    ]
    if search.salary_from is not None:
        params.append(("salary", str(search.salary_from)))
        params.append(("only_with_salary", "true"))
    for value in search.experience:
        params.append(("experience", value))
    if search.schedule:
        params.append(("schedule", search.schedule))
    return f"{SEARCH_URL}?{urlencode(params)}"


def pages_plan(total: int | None, per_page: int) -> int:
    """Сколько страниц выдачи обойти, когда глубина не задана в конфиге.

    Считаем от числа найденных: «Найдено 546 вакансий» при `per_page: 100` —
    это 6 страниц. Если hh.ru число не показал, идём до первой пустой
    страницы, но не больше ``HH_MAX_PAGES``.
    """
    if not total or per_page <= 0:
        return HH_MAX_PAGES
    pages = -(-total // per_page)
    return max(1, min(pages, HH_MAX_PAGES))


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


def profile_key(profile: str) -> str:
    """Нормализованное имя резюме для ключа дедупликации."""
    return " ".join((profile or "").lower().replace("ё", "е").split())


def vacancy_keys(
    title: str,
    company: str,
    url: str,
    profile: str | None = "",
) -> tuple[str, ...]:
    """Ключи записи для дедупликации: вакансия и название резюме.

    Первый ключ — id вакансии из URL, второй — «название + компания». Второй
    нужен потому, что работодатели публикуют одну и ту же вакансию несколько раз
    (разные id, одинаковые название и компания). Профиль входит в каждый ключ:
    разными резюме можно отвечать на одну вакансию, и такие записи независимы.
    `profile=None` — старый режим поиска без учёта резюме.
    """
    keys: list[str] = []
    # Пустой профиль — старые ключи без резюме: так работали прошлые отчёты.
    suffix = f":profile:{profile_key(profile)}" if profile else ""
    vid = vacancy_id(url)
    if vid:
        keys.append(f"id:{vid}{suffix}")
    text = " ".join(f"{title} {company}".lower().replace("ё", "е").split())
    if text:
        keys.append(f"text:{text}{suffix}")
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
    # «Найдено N» читаем из шапки до прокрутки: на последней странице карточек
    # меньше per_page, и ждать полный per_page там бессмысленно.
    total = read_total(page)
    _load_all_cards(page, expected_cards(total, search.per_page, page_index))
    return collect_cards(page), total


def expected_cards(total: int | None, per_page: int, page_index: int) -> int:
    """Сколько карточек ждём на странице: per_page, а на последней — остаток.

    Пример: 350 найденных при per_page 100 — это 100, 100, 100 и 50 карточек.
    """
    if total is None:
        return per_page
    left = total - page_index * per_page
    return max(min(per_page, left), 0)


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


def _load_all_cards(page: Page, expected: int) -> None:
    """Догружает карточки прокруткой страницы выдачи.

    hh.ru отдаёт за страницу 20, 50 или 100 вакансий, но в DOM сначала
    появляется первая двадцатка — остальные приходят порциями по 20 по мере
    прокрутки. Порция приходит не мгновенно, поэтому ждём прироста
    `SCROLL_STABLE_SECONDS`: если список не растёт дольше, считаем, что
    выдача этой страницы закончилась.
    """
    deadline = time.monotonic() + SCROLL_TIMEOUT_SECONDS
    previous = -1
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        count = _count_cards(page)
        if count is None:
            return
        if count != previous:
            previous = count
            quiet_since = time.monotonic()
        if not _needs_scroll(count, expected, time.monotonic() - quiet_since):
            return
        _scroll_to_bottom(page)
        page.wait_for_timeout(SCROLL_PAUSE_MS)


def _count_cards(page: Page) -> int | None:
    """Сколько карточек сейчас в DOM; None — страница уже недоступна."""
    try:
        return page.locator(TITLE_SELECTOR).count()
    except PlaywrightError:
        return None


def _scroll_to_bottom(page: Page) -> None:
    """Прокручивает выдачу вниз, чтобы hh.ru догрузил следующую порцию карточек.

    Крутим и окно, и внутренние контейнеры с прокруткой: у hh.ru бывает и то,
    и другое, а от события scroll зависит ленивая отрисовка. Небольшой отскок
    вверх нужен, чтобы событие точно возникло, даже если страница уже в низу.
    """
    try:
        page.evaluate(
            """() => {
                window.scrollBy(0, -400);
                window.scrollTo(0, document.body.scrollHeight);
                for (const node of document.querySelectorAll('div, main, section')) {
                    const overflow = node.scrollHeight - node.clientHeight;
                    if (overflow > 200 && node.clientHeight > 300) {
                        node.scrollTop = node.scrollHeight;
                    }
                }
            }"""
        )
    except PlaywrightError:
        return


def _needs_scroll(count: int, expected: int, quiet_seconds: float) -> bool:
    """Нужно ли прокрутить выдачу ещё раз, чтобы догрузить карточки.

    `quiet_seconds` — сколько секунд карточки не прибавлялись: если дольше
    `SCROLL_STABLE_SECONDS`, считаем, что карточки этой страницы кончились.
    Пустую страницу ждём меньше (`SCROLL_EMPTY_SECONDS`): там нечего грузить.
    """
    limit = SCROLL_STABLE_SECONDS if count else SCROLL_EMPTY_SECONDS
    return count < expected and quiet_seconds < limit


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
