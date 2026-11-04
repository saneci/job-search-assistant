"""Снимок заполненной формы анкеты или теста: `data/questionary/<дата>-<вакансия>-questionary.png`.

Снимок — доказательство того, что именно агент отправил работодателю: по нему видно
заполненные поля, письмо и кнопку отправки. Ответы в текстовом виде хранятся
в записи дневного отчёта (блок `questionary`), отдельный JSON не нужен.
Данные локальные (`data/` не в git): в форме бывают личные числа из профиля.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page

from .browser import save_screenshot as save_page_screenshot

_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


def save_screenshot(
    directory: Path,
    page: Page,
    *,
    url: str,
    saved_at: datetime | date | None = None,
) -> Path | None:
    """Сохраняет снимок заполненной формы; None, если снять его не удалось.

    Имя файла — дата и номер вакансии: по каталогу видно, к какому дню и какой
    вакансии относится снимок. Сбой скриншота не должен ломать отклик, поэтому
    ошибки возвращаются как None.
    """
    day = saved_at or datetime.now()
    name = f"{day.strftime('%Y-%m-%d')}-{vacancy_number(url)}-questionary.png"
    try:
        return save_page_screenshot(page, directory / name)
    except Exception:
        return None


def vacancy_number(url: str) -> str:
    """Номер вакансии из последнего сегмента пути: `138435882` для ссылки hh.ru.

    Query и кириллица отбрасываются: имя файла остаётся латиницей и цифрами,
    иначе часть файловых систем имя не примет.
    """
    path = urlparse(url or "").path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    return _slug(tail) or "vacancy"


def _slug(value: str) -> str:
    """Короткий безопасный код: только латиница, цифры и дефис."""
    return _UNSAFE_RE.sub("-", (value or "").casefold()).strip("-")[:60]
