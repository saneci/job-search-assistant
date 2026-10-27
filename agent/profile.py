"""Профиль пользователя для LLM: обезличенный профессиональный блок резюме.

Профиль собирается из резюме hh.ru (`agent/resume.py`) и живёт только в памяти
процесса. Персональные данные не читаются и не сохраняются: блоки контактов,
фото и личных сведений не извлекаются, а остатки контактов и номеров вычищает
`scrub_personal_data`. Стиль письма живёт в `SYSTEM_PROMPT.md`, а не в профиле.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class ProfileError(RuntimeError):
    """Профиль пользователя получить не удалось."""


@dataclass(frozen=True)
class Profile:
    """Данные профиля для промпта LLM: только факты из резюме."""

    text: str


# Метки, которые подставляются вместо персональных данных.
EMAIL_REPLACEMENT = "[почта скрыта]"
PHONE_REPLACEMENT = "[телефон скрыт]"
BIRTHDAY_REPLACEMENT = "[дата рождения скрыта]"
DOCUMENT_REPLACEMENT = "[номер скрыт]"
CONTACT_REPLACEMENT = "[контакт скрыт]"

# Что вычищаем из текста резюме перед отправкой в LLM. Порядок важен:
# почта вычищается раньше «собаки», иначе от почты останется имя пользователя.
_SCRUB_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.IGNORECASE), EMAIL_REPLACEMENT),
    (
        re.compile(r"https?://(?:t\.me|telegram\.me|wa\.me|vk\.me)/\S+", re.IGNORECASE),
        CONTACT_REPLACEMENT,
    ),
    # Ник в мессенджере: только с явным словом-подсказкой или с подчёркиванием.
    # Так аннотации вроде @Override и @Autowired остаются частью профиля.
    (
        re.compile(
            r"(?:телеграм|telegram|tg|ник|username|контакт)\w*[^\n@]{0,20}@[A-Za-z0-9_]{4,}",
            re.IGNORECASE,
        ),
        CONTACT_REPLACEMENT,
    ),
    (re.compile(r"(?<![\w@.])@[A-Za-z0-9]*_[A-Za-z0-9_]+"), CONTACT_REPLACEMENT),
    (
        re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"),
        PHONE_REPLACEMENT,
    ),
    (
        re.compile(
            r"(?:дата\s+рождения|родил(?:ся|ась))\s*[:\-]?\s*[^\n,]{0,40}",
            re.IGNORECASE,
        ),
        BIRTHDAY_REPLACEMENT,
    ),
    (re.compile(r"\b\d{3}-\d{3}-\d{3}[ \-]?\d{2}\b"), DOCUMENT_REPLACEMENT),
    (re.compile(r"\bИНН\s*[:\-]?\s*\d{10,12}\b", re.IGNORECASE), DOCUMENT_REPLACEMENT),
)


def normalize_text(text: str) -> str:
    """Приводит текст страницы к обычным пробелам и убирает пустые строки.

    hh.ru отдаёт неразрывные пробелы (NBSP) и невидимые символы: они ломают
    сравнение заголовков, дедупликацию и склейку профиля, поэтому нормализуем
    текст до разбора.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    unified = unified.replace("\u200b", "").replace("\ufeff", "")
    lines = [" ".join(line.split()) for line in unified.split("\n")]
    return "\n".join(line for line in lines if line)


def scrub_personal_data(text: str) -> str:
    """Заменяет контакты и номера в тексте метками: страховка перед промптом."""
    result = text
    for pattern, replacement in _SCRUB_RULES:
        result = pattern.sub(replacement, result)
    return result


# Метки, которые остаются в тексте после обезличивания.
_MARKER_RE = re.compile(
    r"\[(?:почта|телефон|дата рождения|номер|контакт)\s+скрыт[ао]?\]",
    re.IGNORECASE,
)
# Подписи контактов: без значения такая строка бесполезна.
_CONTACT_LABEL_RE = re.compile(
    r"^(?:почта|e-?mail|телефон|тел\.?|telegram|телеграм|tg|whatsapp|skype|vk)\s*:?\s*$",
    re.IGNORECASE,
)


def drop_contact_markers(text: str) -> str:
    """Убирает строки, где после обезличивания не осталось полезного текста."""
    lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            lines.append(line)
            continue
        rest = _MARKER_RE.sub("", line).strip(" \t:;,.—-–")
        if not rest or _CONTACT_LABEL_RE.match(rest):
            continue
        lines.append(line)
    return "\n".join(lines)



