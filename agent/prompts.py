"""Промпты для LLM (раздел 5 требований)."""

from __future__ import annotations

import re
from pathlib import Path

from .models import VacancyDetails
from .profile import Profile


# Основной системный промпт хранится в SYSTEM_PROMPT.md (правится без изменения кода).
# Константа ниже — аварийный минимум на случай, если файл недоступен.
FALLBACK_SYSTEM_PROMPT = """Ты — персональный ассистент по поиску работы. Оцени вакансию
на соответствие профилю пользователя и, если она подходит, напиши
сопроводительное письмо от его имени, используя только факты из профиля.

ФОРМАТ ОТВЕТА (строго JSON):
{
  "verdict": "подходит" | "не подходит",
  "reason": "краткое обоснование (1–2 предложения)",
  "cover_letter": "текст письма (только если verdict = подходит)"
}"""

USER_PROMPT_TEMPLATE = """ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ (резюме):
{profile_md}

ТЕКСТ ВАКАНСИИ:
{title}
{company}
{salary}
{description}
{opening_hint}
Оцени вакансию и, если подходит, напиши письмо. Верни строго JSON."""

# Подсказка для случая, когда работодатель требует начать письмо с конкретного
# слова: модель редко угадывает такой формат, а проверка остаётся в коде.
OPENING_HINT_TEMPLATE = """ТРЕБОВАНИЕ ВАКАНСИИ К НАЧАЛУ ПИСЬМА:
Первым идёт «{opening}» дословно, сразу после него - «Здравствуйте!», затем обычный текст письма.
"""

# Подсказка для случая, когда вакансия просит приложить ссылки на проекты.
PROJECT_LINKS_HINT_TEMPLATE = """ТРЕБОВАНИЕ ВАКАНСИИ К ПРОЕКТАМ:
В вакансии просят приложить ссылки на проекты в сопроводительном письме.
{links}
Не придумывай ссылки: используй только профиль и список выше.
"""

# Фразы, которыми вакансии просят ссылки на проекты в письме.
_PROJECT_LINKS_RE = re.compile(
    r"ссылк\w*\s+(?:на|к)\s+(?:ваш\w*\s+)?"
    r"(?:проект\w*|портфолио|github|gitlab|репозитори\w*)"
    r"|(?:приложите|приложить|пришлите|укажите|указать|добавьте|добавить|"
    r"оставьте|оставить)\s+(?:ссылк\w*|портфолио)",
    re.IGNORECASE,
)


def requires_project_links(vacancy: VacancyDetails) -> bool:
    """Просит ли вакансия приложить к письму ссылки на проекты."""
    return bool(_PROJECT_LINKS_RE.search(vacancy.description or ""))


# Фразы, которыми вакансии задают обязательное начало письма:
# «Начните сопроводительное письмо со слова 3PL», «Письмо должно начинаться с фразы «...»».
_OPENING_TRIGGER_RE = re.compile(
    r"(?:(?:начните|начни|начинайте)"
    r"|(?:письмо|отклик|сообщение|сопроводительное)\s+(?:должно\s+|должен\s+)?начина(?:ется|ться))"
    r"[^.!?\n]{0,80}?"
    r"(?:со\s+слов(?:а|ом)?|с\s+слов(?:а|ом)?|со\s+фразы|с\s+фразы|фразой|словом|с)\s+"
    r"(?P<opening>[^.!?\n]+)",
    re.IGNORECASE,
)
# Начало вроде «с приветствия» или «с рассказа» это не требуемое слово, а описание.
_OPENING_STOPWORDS = frozenset(
    {
        "приветствия",
        "приветствием",
        "обращения",
        "обращением",
        "рассказа",
        "рассказом",
        "описания",
        "себя",
        "того",
        "фразы",
        "слова",
        "имени",
        "названия",
    }
)
_MAX_OPENING_WORDS = 5
_MAX_OPENING_LENGTH = 60

RETRY_JSON_INSTRUCTION = (
    "Предыдущий ответ не является корректным JSON. "
    "Верни только JSON-объект без markdown-разметки и пояснений."
)

REJECTION_SYSTEM_PROMPT = """Ты объясняешь кандидату, почему вакансия ему не подходит.
Одно предложение до 200 символов: чего не хватает или что именно не совпало.
Только причина — без вступлений, извинений и советов. Пиши по-русски."""

REJECTION_MAX_TOKENS = 120
REJECTION_MAX_LENGTH = 240
_REJECTION_DESCRIPTION_LIMIT = 1500


def build_rejection_prompt(
    profile: Profile,
    vacancy: VacancyDetails,
    probability: float,
    threshold: float,
) -> str:
    """Короткий user-промпт для объяснения отказа (дешёвый запрос)."""
    description = vacancy.description or ""
    if len(description) > _REJECTION_DESCRIPTION_LIMIT:
        description = description[: _REJECTION_DESCRIPTION_LIMIT] + "…"
    return (
        f"ПРОФИЛЬ:\n{profile.text}\n\n"
        f"ВАКАНСИЯ:\n{vacancy.title} — {vacancy.company or 'компания не указана'}\n"
        f"{description}\n\n"
        f"Вероятность соответствия: {probability:.2f} (порог {threshold:.2f})."
    )




def build_user_prompt(
    profile: Profile,
    vacancy: VacancyDetails,
    opening: str = "",
    pet_projects: tuple[str, ...] = (),
    needs_project_links: bool = False,
) -> str:
    """Подставляет данные профиля и вакансии в шаблон user prompt.

    `opening` — обязательное начало письма из текста вакансии (если есть):
    тогда в промпт добавляется явное требование, а не надежда на догадку модели.
    """
    values = {
        "{profile_md}": profile.text,
        "{title}": vacancy.title,
        "{company}": vacancy.company or "не указана",
        "{salary}": vacancy.salary_text,
        "{description}": vacancy.description,
        "{opening_hint}": OPENING_HINT_TEMPLATE.format(opening=opening) if opening else "",
    }
    prompt = USER_PROMPT_TEMPLATE
    for placeholder, value in values.items():
        prompt = prompt.replace(placeholder, str(value))
    links = tuple(str(link).strip() for link in pet_projects if str(link).strip())
    if needs_project_links:
        if links:
            listing = "\n".join(f"- {link}" for link in links)
            hint = PROJECT_LINKS_HINT_TEMPLATE.format(
                links=f"Ссылки пользователя:\n{listing}"
            )
        else:
            hint = PROJECT_LINKS_HINT_TEMPLATE.format(
                links="Ссылок в профиле и конфиге нет: не придумывай их."
            )
        prompt = prompt.replace("\nОцени вакансию", f"\n{hint}\nОцени вакансию", 1)

    return prompt


def detect_required_opening(vacancy: VacancyDetails) -> str:
    """Обязательное начало письма из текста вакансии (пусто, если требования нет)."""
    return opening_from_text(vacancy.description or "")


def opening_from_text(description: str) -> str:
    """Ищет в описании вакансии требование «начните письмо со слова ...».

    Это дешёвая эвристика вместо отдельного вызова LLM: находим фразу-триггер,
    берём то, что после неё, и отбрасываем описания вроде «с рассказа о себе».
    """
    for match in _OPENING_TRIGGER_RE.finditer(description or ""):
        opening = _clean_opening(match.group("opening"))
        if _is_literal_opening(opening):
            return opening
    return ""


def _clean_opening(value: str) -> str:
    text = " ".join(value.split())
    for opening_quote, closing_quote in (("«", "»"), ("“", "”"), ("„", "“"), ('"', '"')):
        if text.startswith(opening_quote):
            end = text.find(closing_quote, 1)
            if end > 0:
                text = text[1:end]
                break
    else:
        for separator in (" (", ", "):
            text = text.split(separator)[0]
    return text.strip(" \t\"'«»“”„.,:;!?—-–")


def _is_literal_opening(opening: str) -> bool:
    if not opening or len(opening) > _MAX_OPENING_LENGTH:
        return False
    words = opening.split()
    if len(words) > _MAX_OPENING_WORDS:
        return False
    if words[0].lower() in _OPENING_STOPWORDS:
        return False
    return any(char.isalnum() for char in opening)


def load_system_prompt(path: Path | None = None) -> str:
    """Читает системный промпт из файла (обычно SYSTEM_PROMPT.md).

    Если файл не задан, отсутствует или пуст — возвращает FALLBACK_SYSTEM_PROMPT,
    чтобы запрос к LLM всё равно содержал формат ответа. В рабочем запуске путь
    проверяется при загрузке конфигурации, поэтому fallback не используется.
    """
    if path is None or not path.exists():
        return FALLBACK_SYSTEM_PROMPT
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return FALLBACK_SYSTEM_PROMPT
    return text or FALLBACK_SYSTEM_PROMPT
