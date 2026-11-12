"""Подъём резюме в поиске hh.ru: не чаще, чем раз в resume.boost_interval_hours.

Кэш (`paths.cache`) — плоский JSON ``{название резюме: время последнего подъёма}``.
Ошибка браузера не останавливает запуск: агент пишет событие в лог и переходит
к основному сценарию. Если список резюме загрузился, а настроенного резюме в нём
нет, запуск останавливается: отклик другим резюме отправлять нельзя.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .browser import NAVIGATION_TIMEOUT_MS
from .config import AppConfig
from .logging_setup import RunLogger

RESUMES_URL = "https://hh.ru/applicant/my_resumes"
RESUME_CARD_SELECTOR = "[data-qa='resume']"
RESUME_TITLE_SELECTOR = "[data-qa='resume-title']"
RESUME_LINK_SELECTOR = "a[data-qa^='resume-card-link-']"
RESUME_LIST_WAIT_MS = 5000
RESUME_DETAIL_URL_RE = re.compile(r"/resume/(?!edit/)")
BOOST_RE = re.compile(r"поднять в поиске", re.IGNORECASE)
BOOST_SELECTORS = (
    "[data-qa='resume-update-button']",
    "[data-qa='resume-boost-button']",
    "button[data-qa*='resume-update']",
    "[data-qa='resume-update-button'] button",
)
BOOST_RESULT_WAIT_MS = 2000

STATUS_BOOSTED = "boosted"
STATUS_FRESH = "fresh"
STATUS_DISABLED = "disabled"
STATUS_UNAVAILABLE = "unavailable"


class ResumeListNotFoundError(RuntimeError):
    """В списке резюме hh.ru нет резюме с настроенным названием."""

    def __init__(self, resume: str) -> None:
        super().__init__(f"в списке резюме hh.ru нет резюме «{resume}»")
        self.resume = resume


class ResumeCache:
    """Кэш подъёмов резюме: когда каждое резюме поднимали в последний раз."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, str] = {}

    def load(self) -> dict[str, str]:
        """Читает кэш; битый или отсутствующий файл считается пустым."""
        if not self.path.exists():
            self._items = {}
            return self._items
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._items = {}
            return self._items
        if not isinstance(raw, dict):
            self._items = {}
            return self._items
        self._items = {str(key): str(value) for key, value in raw.items()}
        return self._items

    def last_boost(self, name: str) -> datetime | None:
        """Время последнего подъёма резюме или None (в кэше нет отметки)."""
        value = self.load().get(name)
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def due(self, name: str, interval_hours: int, now: datetime | None = None) -> bool:
        """Пора ли поднимать резюме: прошло не меньше interval_hours с прошлого раза."""
        last = self.last_boost(name)
        if last is None:
            return True
        moment = now or datetime.now().astimezone()
        if last.tzinfo is None:
            last = last.astimezone()
        return moment - last >= timedelta(hours=interval_hours)

    def mark(self, name: str, moment: datetime | None = None) -> datetime:
        """Запоминает время подъёма резюме и сохраняет кэш."""
        at = moment or datetime.now().astimezone()
        items = self.load()
        items[name] = at.isoformat(timespec="seconds")
        self._items = items
        self._save()
        return at

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)


def boost_resume(page: Page, config: AppConfig, logger: RunLogger) -> str:
    """Поднимает резюме в поиске, если пришло время. Возвращает статус операции."""
    name = config.pipeline.resume.name.strip()
    if not name:
        logger.event("resume_boost_disabled")
        return STATUS_DISABLED

    cache = ResumeCache(config.data.paths.cache)
    interval = config.pipeline.resume.boost_interval_hours
    last = cache.last_boost(name)
    if last is not None and not cache.due(name, interval):
        logger.event("resume_boost_fresh", resume=name, last_boost=last.isoformat())
        logger.info(
            f"Резюме «{name}» поднимали {last:%Y-%m-%d %H:%M}, "
            f"следующий подъём не раньше чем через {interval} ч."
        )
        return STATUS_FRESH

    try:
        page.goto(RESUMES_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        _wait_for_resumes(page)
        link = _find_resume_link(page, name)
        if link is None:
            logger.event("resume_not_found", resume=name, url=page.url)
            logger.info(f"Не нашёл резюме «{name}» в списке резюме, запуск остановлен.")
            raise ResumeListNotFoundError(name)

        link.click()
        page.wait_for_url(RESUME_DETAIL_URL_RE, timeout=NAVIGATION_TIMEOUT_MS)
        page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        _wait_for_boost_button(page)

        button = _find_boost_button(page)
        if button is None or not _is_enabled(button):
            logger.event("resume_boost_unavailable", resume=name, url=page.url)
            _remember_check(cache, name, logger)
            logger.info(
                f"Кнопки «Поднять в поиске» нет: время проверки отмечено, "
                f"следующая проверка не раньше чем через {interval} ч."
            )
            return STATUS_UNAVAILABLE

        button.click()
        page.wait_for_timeout(BOOST_RESULT_WAIT_MS)
    except PlaywrightError as exc:
        logger.event("resume_boost_error", resume=name, error=str(exc))
        logger.info(f"Не удалось поднять резюме в поиске: {exc}")
        return STATUS_UNAVAILABLE

    try:
        at = cache.mark(name)
    except OSError as exc:
        logger.event("resume_cache_error", resume=name, error=str(exc))
        logger.info(f"Резюме поднято, но отметка в кэше не сохранена: {exc}")
        return STATUS_BOOSTED

    logger.event("resume_boosted", resume=name, boosted_at=at.isoformat())
    logger.info(f"Резюме «{name}» поднято в поиске.")
    return STATUS_BOOSTED


def _remember_check(cache: ResumeCache, name: str, logger: RunLogger) -> None:
    """Отмечает проверку страницы резюме, когда кнопки подъёма нет.

    hh сам обновляет резюме и дату обновления на странице не отдаёт: без
    отметки агент открывал бы страницу резюме на каждом запросе и запуске.
    """
    try:
        cache.mark(name)
    except OSError as exc:
        logger.event("resume_cache_error", resume=name, error=str(exc))
        logger.info(f"Не удалось сохранить отметку проверки резюме: {exc}")


def _wait_for_resumes(page: Page) -> None:
    """Ждёт карточки резюме: hh.ru дорисовывает список после загрузки страницы."""
    try:
        page.wait_for_selector(RESUME_CARD_SELECTOR, timeout=RESUME_LIST_WAIT_MS)
    except PlaywrightError:
        return


def _wait_for_boost_button(page: Page) -> None:
    """Ждёт кнопку на странице резюме, а не кнопку из списка резюме."""
    try:
        page.wait_for_selector(BOOST_SELECTORS[0], timeout=RESUME_LIST_WAIT_MS)
    except PlaywrightError:
        return


def _resume_cards(page: Page) -> list[Locator]:
    """Видимые карточки резюме на странице списка."""
    cards = page.locator(RESUME_CARD_SELECTOR)
    try:
        total = min(cards.count(), 50)
    except PlaywrightError:
        return []
    return [cards.nth(index) for index in range(total)]


def _resume_words(text: str) -> str:
    """Нормализация названия резюме для сравнения."""
    return " ".join((text or "").lower().replace("ё", "е").split())


def _find_resume_link(page: Page, name: str) -> Locator | None:
    """Ссылка на резюме с указанным названием в списке резюме.

    На hh.ru ссылка карточки имеет ``role='button'`` и содержит не только
    название, но и тип занятости, доход и регион. Поэтому сначала ищем карточку
    по точному названию внутри ``data-qa='resume-title'``, а уже потом ссылку
    внутри неё. Поиск по роли ссылки остаётся запасным вариантом для старой
    вёрстки.
    """
    wanted = _resume_words(name)
    for card in _resume_cards(page):
        title = card.locator(RESUME_TITLE_SELECTOR).first
        if not _is_visible(title):
            continue
        try:
            value = title.inner_text().strip()
        except PlaywrightError:
            continue
        if _resume_words(value) != wanted:
            continue
        link = card.locator(RESUME_LINK_SELECTOR).first
        return link if _is_visible(link) else title
    for locator in _candidates(page, name):
        if _is_visible(locator):
            return locator
    return None


def _candidates(page: Page, name: str) -> list[Locator]:
    candidates: list[Locator] = []
    for role in ("link", "button"):
        try:
            candidates.append(page.get_by_role(role, name=name).first)
        except PlaywrightError:
            pass
    try:
        candidates.append(page.get_by_text(name, exact=True).first)
    except PlaywrightError:
        pass
    return candidates


def _find_boost_button(page: Page) -> Locator | None:
    """Кнопка «Поднять в поиске» на странице резюме."""
    for selector in BOOST_SELECTORS:
        locator = page.locator(selector).first
        if _is_visible(locator):
            return locator
    for role in ("button", "link"):
        try:
            locator = page.get_by_role(role, name=BOOST_RE).first
        except PlaywrightError:
            continue
        if _is_visible(locator):
            return locator
    return None


def _is_visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.is_visible()
    except PlaywrightError:
        return False


def _is_enabled(locator: Locator) -> bool:
    try:
        return locator.is_enabled()
    except PlaywrightError:
        return False
