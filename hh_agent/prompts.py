"""Промпты для LLM (раздел 5 требований)."""

from __future__ import annotations

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

USER_PROMPT_TEMPLATE = """ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:
{profile_md}

ПРИМЕРЫ ПИСЕМ ПОЛЬЗОВАТЕЛЯ:
{examples}

ПРАВИЛА СТИЛЯ:
{style_rules}

ТЕКСТ ВАКАНСИИ:
{title}
{company}
{salary}
{description}

Оцени вакансию и, если подходит, напиши письмо. Верни строго JSON."""

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




def build_user_prompt(profile: Profile, vacancy: VacancyDetails) -> str:
    """Подставляет данные профиля и вакансии в шаблон user prompt."""
    values = {
        "{profile_md}": profile.text,
        "{examples}": profile.examples or "примеры не заданы",
        "{style_rules}": profile.style_rules,
        "{title}": vacancy.title,
        "{company}": vacancy.company or "не указана",
        "{salary}": vacancy.salary_text,
        "{description}": vacancy.description,
    }
    prompt = USER_PROMPT_TEMPLATE
    for placeholder, value in values.items():
        prompt = prompt.replace(placeholder, str(value))
    return prompt


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
