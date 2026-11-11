"""Разбор PROFILE.md: описание пользователя, примеры писем и правила стиля."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class ProfileError(RuntimeError):
    """Профиль пользователя отсутствует или заполнен неверно."""


@dataclass(frozen=True)
class Profile:
    """Данные профиля для промпта LLM.

    `name` — значение из заголовка `# PROFILE:`: по нему в отчёте видно,
    какой профиль использовался (у Habr резюме hh.ru нет).
    """

    text: str
    examples: str
    style_rules: str
    name: str = ""
    # Факты для оценки вакансии: числа берутся из анкеты hh.
    experience_total_years: float | None = None
    experience_role_years: float | None = None
    english_level: str = ""


_HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*$", re.MULTILINE)
_PROFILE_NAME_RE = re.compile(r"^\s*#\s*PROFILE\s*:\s*(?P<name>.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_EXAMPLES_HEADING_RE = re.compile(r"^\s*[*-]\s*\*\*[^*]*пример", re.IGNORECASE)
_BULLET_RE = re.compile(r"^[*-]\s+")


def load_profile(path: Path) -> Profile:
    """Читает профиль и выделяет из него примеры писем и правила стиля."""
    if not path.exists():
        raise ProfileError(f"Файл профиля не найден: {path}")
    text = path.read_text(encoding="utf-8")
    style_rules, examples = split_style_section(text)
    if not style_rules:
        raise ProfileError(
            "В профиле не найден раздел со стилем писем. Ожидается заголовок вида "
            "'## Стиль написания сопроводительных писем'"
        )
    return Profile(
        text=text,
        examples=examples,
        style_rules=style_rules,
        name=parse_profile_name(text),
    )


def parse_profile_name(text: str) -> str:
    """Имя профиля из заголовка `# PROFILE:`; без заголовка — пустая строка."""
    match = _PROFILE_NAME_RE.search(text or "")
    return match.group("name").strip() if match else ""


def split_style_section(text: str) -> tuple[str, str]:
    """Возвращает (правила стиля, примеры писем) из раздела о стиле."""
    headings = list(_HEADING_RE.finditer(text))
    style_heading = next((match for match in headings if "стил" in match.group("title").lower()), None)
    if style_heading is None:
        return "", ""

    next_heading = next((match for match in headings if match.start() > style_heading.start()), None)
    section_end = next_heading.start() if next_heading else len(text)
    section = text[style_heading.end():section_end].strip()

    lines = section.splitlines()
    examples_index = next(
        (index for index, line in enumerate(lines) if _EXAMPLES_HEADING_RE.match(line)),
        None,
    )
    if examples_index is None:
        return section, ""

    style_rules = "\n".join(lines[:examples_index]).strip()
    examples: list[str] = []
    current: list[str] = []
    for line in lines[examples_index + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if _BULLET_RE.match(stripped):
            if current:
                examples.append(" ".join(current))
            current = [_BULLET_RE.sub("", stripped)]
        elif current:
            current.append(stripped)
    if current:
        examples.append(" ".join(current))
    return style_rules, "\n".join(f"- {example}" for example in examples)
