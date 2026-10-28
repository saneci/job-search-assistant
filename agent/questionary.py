"""Сохранение ответов на анкеты и тесты: `data/questionary/<вакансия>-<резюме>.json`.

Файл на вакансию и резюме: по нему видно, что именно агент ответил работодателю,
если позже придёт отказ или приглашение. Данные локальные (`data/` не в git):
в ответах бывают личные числа из профиля. В журналы тексты ответов не попадают.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


def save(
    directory: Path,
    *,
    url: str,
    profile: str,
    channel: str,
    status: str,
    questionary: Sequence[tuple[str, str]],
    title: str = "",
    company: str = "",
    saved_at: datetime | None = None,
) -> Path | None:
    """Пишет ответы в файл вакансии; без ответов файла не создаёт.

    Возвращает путь к файлу или None, если сохранять нечего или не удалось:
    сбой записи не должен ломать отклик.
    """
    entries = [{"question": question, "answer": answer} for question, answer in questionary]
    if not entries:
        return None
    payload = {
        "url": url,
        "profile": profile,
        "channel": channel,
        "title": title,
        "company": company,
        "status": status,
        "saved_at": (saved_at or datetime.now()).isoformat(timespec="seconds"),
        "questionary": entries,
    }
    path = directory / file_name(url, profile)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except OSError:
        return None
    return path


def file_name(url: str, profile: str) -> str:
    """Имя файла: номер вакансии и резюме. Латиница безопасна для любой ФС."""
    vacancy = _slug(url.rstrip("/").rsplit("/", 1)[-1]) or "vacancy"
    resume = _slug(profile) or "resume"
    return f"{vacancy}-{resume}.json"


def _slug(value: str) -> str:
    """Короткий безопасный код: только латиница, цифры и дефис."""
    return _UNSAFE_RE.sub("-", (value or "").casefold()).strip("-")[:60]
