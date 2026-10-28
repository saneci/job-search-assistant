"""Канал поиска работы: общий контракт pipeline для hh.ru и career.habr.com.

Канал описывает всё, что различается между сайтами: вход и проверку сессии,
выдачу, страницу вакансии, отклик и тексты причин. Всё, что не зависит от сайта
(LLM, лимиты, отчёты, дедупликация), остаётся в pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping
from urllib.parse import urlparse

from playwright.sync_api import Page

from . import habr_search, habr_vacancy
from .config import AppConfig
from .hh_vacancy import ResponseResult
from .models import VacancyCard, VacancyDetails

HH = "hh"
HABR = "habr"

CHANNEL_NAMES = (HH, HABR)

# Причины для файла дня: те же коды, что у hh.ru, но с названием своего сайта.
HABR_QUEUE_REASONS: dict[str, str] = {
    "empty_cover_letter": "Нужно заполнить сопроводительное письмо вручную",
    "project_links": "В вакансии требуется ссылка на проекты в письме",
    "external_form": "Отклик нужно отправить на сайте компании",
    "captcha": "career.habr.com просит пройти капчу: откликнитесь вручную",
    "no_form": "career.habr.com не показал форму отклика — проверьте вакансию вручную",
    "unknown": "career.habr.com не подтвердил отправку отклика — проверьте вакансию вручную",
}


@dataclass(frozen=True)
class QueryLabels:
    """Готовые формулировки для логов: у hh поиск по запросу, у Habr — лента."""

    start: str
    subject: str
    source: str
    title: str


@dataclass(frozen=True)
class Channel:
    """Всё, что pipeline делает с конкретным сайтом поиска вакансий."""

    name: str
    site_label: str
    login_url: str
    # Постоянный профиль браузера нужен Habr: без него не рисуется hCaptcha.
    uses_persistent_profile: bool
    # Подъём резюме есть только на hh.ru.
    supports_resume_boost: bool
    per_page: int
    queries: Callable[[], tuple[str, ...]]
    query_label: Callable[[str], str]
    labels: Callable[[str], QueryLabels]
    open_search_page: Callable[[Page, str, int], tuple[list[VacancyCard], int | None]]
    pages_plan: Callable[[int | None], int]
    vacancy_id: Callable[[str], str | None]
    open_vacancy: Callable[[Page, VacancyCard], VacancyDetails]
    respond: Callable[[Page, str, Callable[[], bool]], ResponseResult]
    response_state: Callable[[Page], dict[str, object]]
    # None — проверка hh.ru по файлу сессии (поведение по умолчанию).
    auth_check: Callable[[Page], bool] | None = None
    queue_reasons: Mapping[str, str] = field(default_factory=dict)


def channel_name_for_url(url: str) -> str:
    """Канал по ссылке из отчёта: нужен для `--retry`, где записи разных сайтов."""
    host = urlparse(url or "").netloc.lower()
    return HABR if "habr.com" in host else HH


def habr_channel(config: AppConfig) -> Channel:
    """Канал career.habr.com: лента «Подходящие» без поисковых запросов."""
    limit = config.search.pages_limit

    def pages_plan(total: int | None) -> int:
        pages = habr_search.pages_plan(total)
        return pages if limit is None else max(1, min(pages, limit))

    return Channel(
        name=HABR,
        site_label="career.habr.com",
        login_url=habr_vacancy.LOGIN_URL,
        uses_persistent_profile=True,
        supports_resume_boost=False,
        per_page=habr_search.HABR_PER_PAGE,
        queries=lambda: ("",),
        query_label=lambda query: "в ленте «Подходящие»",
        labels=lambda query: QueryLabels(
            start="в ленте «Подходящие»",
            subject="В ленте «Подходящие»",
            source="из ленты «Подходящие»",
            title="Лента «Подходящие»",
        ),
        open_search_page=habr_search.open_search_page,
        pages_plan=pages_plan,
        vacancy_id=habr_search.vacancy_id,
        open_vacancy=habr_vacancy.open_vacancy,
        respond=habr_vacancy.respond,
        response_state=habr_vacancy.response_state,
        auth_check=habr_vacancy.is_authenticated,
        queue_reasons=HABR_QUEUE_REASONS,
    )
