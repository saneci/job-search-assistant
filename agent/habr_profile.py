"""Профиль кандидата с career.habr.com: заглушка до реализации механики.

Механика повторяет hh.ru (`agent/resume.py`): профиль собирается из профиля
пользователя на сайте, персональные данные не читаются, профиль живёт только
в памяти процесса. Разбор профиля Habr пока не реализован: функция
останавливает запуск понятной ошибкой, а не отдаёт LLM пустой профиль.
"""

from __future__ import annotations

from playwright.sync_api import Page

from .config import AppConfig
from .logging_setup import RunLogger
from .profile import Profile, ProfileError


def load_habr_profile(page: Page, config: AppConfig, logger: RunLogger) -> Profile:
    """Профиль из career.habr.com: пока не реализован, запуск останавливается."""
    del page, config
    logger.event("habr_profile_not_implemented", site="career.habr.com")
    raise ProfileError(
        "профиль из career.habr.com пока не собирается: "
        "для канала habr источник профиля не реализован"
    )
