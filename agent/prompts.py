"""Промпты для LLM (раздел 5 требований)."""

from __future__ import annotations

import re
from dataclasses import dataclass
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
{opening_hint}
Оцени вакансию и, если подходит, напиши письмо. Верни строго JSON."""

# Подсказка для случая, когда работодатель требует начать письмо с конкретного
# слова: модель редко угадывает такой формат, а проверка остаётся в коде.
OPENING_HINT_TEMPLATE = """ТРЕБОВАНИЕ ВАКАНСИИ К НАЧАЛУ ПИСЬМА:
Первым идёт «{opening}» дословно, сразу после него - «Здравствуйте!», затем обычный текст письма.
"""

# Подсказка для случая, когда в форме отклика или теста есть вопросы анкеты:
# ответы приходят тем же вызовом, что и письмо (один вызов — один запрос к LLM).
QUESTIONNAIRE_HINT_TEMPLATE = """ВОПРОСЫ АНКЕТЫ ИЛИ ТЕСТА РАБОТОДАТЕЛЯ (ответь на каждый):
{questions}
"""

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

REJECTION_SYSTEM_PROMPT = """Ты объясняешь кандидату, насколько вакансия ему подходит.
Два-три предложения до 400 символов: сначала что из требований подтверждено профилем, затем какого конкретного требования не хватает.
Только факты из профиля и вакансии, без вступлений, извинений и советов. Пиши по-русски."""

# Описание картинками: работодатель на Habr отдаёт текст вакансии изображениями.
# Задача вызова — переписать текст, а не оценить вакансию.
DESCRIPTION_IMAGES_PROMPT = (
    "Перепиши весь текст с изображений вакансии по-русски, сохраняя порядок блоков и пунктов. "
    "Только то, что видно на изображениях, без пояснений и пересказа своими словами. "
    "Если текста на изображениях нет, верни пустую строку."
)

DESCRIPTION_IMAGES_MAX_TOKENS = 2000

REJECTION_MAX_TOKENS = 250
REJECTION_MAX_LENGTH = 400


def build_rejection_prompt(
    profile: Profile,
    vacancy: VacancyDetails,
    probability: float,
    threshold: float,
) -> str:
    """User-промпт для объяснения отказа: профиль, требования и полное описание.

    Описание не обрезается: требование может стоять в конце, а объяснение
    должно называть конкретную причину отказа.
    """
    description = vacancy.description or ""
    requirements = split_requirements(description).mandatory
    requirements_block = (
        f"ТРЕБОВАНИЯ ВАКАНСИИ:\n{requirements}\n\n" if requirements else ""
    )
    return (
        f"ПРОФИЛЬ:\n{profile.text}\n\n"
        f"ВАКАНСИЯ:\n{vacancy.title} — {vacancy.company or 'компания не указана'}\n"
        f"{requirements_block}"
        f"ОПИСАНИЕ ВАКАНСИИ:\n{description}\n\n"
        f"Оценка соответствия: {probability:.0%} (порог {threshold:.0%})."
    )


def build_user_prompt(
    profile: Profile,
    vacancy: VacancyDetails,
    opening: str = "",
    pet_projects: tuple[str, ...] = (),
    needs_project_links: bool = False,
    questionnaire: str = "",
) -> str:
    """Подставляет данные профиля и вакансии в шаблон user prompt.

    `opening` — обязательное начало письма из текста вакансии (если есть):
    тогда в промпт добавляется явное требование, а не надежда на догадку модели.
    `questionnaire` — вопросы анкеты или теста: ответы приходят тем же вызовом.
    """
    values = {
        "{profile_md}": profile.text,
        "{examples}": profile.examples or "примеры не заданы",
        "{style_rules}": profile.style_rules,
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

    questions = (questionnaire or "").strip()
    if questions:
        hint = QUESTIONNAIRE_HINT_TEMPLATE.format(questions=questions)
        prompt = prompt.replace(
            "\nОцени вакансию",
            f"\n{hint}\nОцени вакансию и ответь на вопросы анкеты.",
            1,
        )

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


# --- Требования вакансии и факты профиля для Decision ---------------------

# Заголовки блоков, которые встречаются в описаниях hh.ru и Habr.
_REQUIREMENTS_HEADINGS = (
    "обязательные требования",
    "квалификационные требования",
    "требования к кандидату",
    "требования к соискателю",
    "требуемый опыт",
    "требования",
    "что мы ждём от кандидата",
    "что мы ждем от кандидата",
    "что мы ждём",
    "что мы ждем",
    "что ожидаем от кандидата",
    "что ожидаем",
    "кого мы ищем",
    "кого ищем",
    "наши ожидания",
    "ключевые навыки",
    "какие знания и навыки нам важны",
    "навыки и знания",
    "что важно знать",
)
_OPTIONAL_HEADINGS = (
    "будет большим плюсом",
    "будет плюсом",
    "будет преимуществом",
    "плюсом будет",
    "желательно",
    "приветствуется",
)
_OTHER_HEADINGS = (
    "обязанности",
    "задачи",
    "что нужно делать",
    "условия работы",
    "условия",
    "мы предлагаем",
    "что мы предлагаем",
    "бонусы",
    "о компании",
    "о проекте",
    "о нас",
)


@dataclass(frozen=True)
class VacancyRequirements:
    """Блок требований вакансии: обязательные и «будет плюсом»."""

    mandatory: str = ""
    optional: str = ""


def split_requirements(description: str) -> VacancyRequirements:
    """Находит в описании блок требований, не полагаясь на шапку сайта.

    Заголовок блока ищется по строке целиком (hh.ru и Habr отдают заголовки
    отдельными строками) или по началу строки с двоеточием: «Требования: ...».
    Секция заканчивается на следующем известном заголовке.
    """
    sections: list[tuple[str, list[str]]] = []
    for line in (description or "").splitlines():
        heading = _section_heading(line)
        if heading is not None:
            kind, rest = heading
            sections.append((kind, [rest] if rest else []))
            continue
        if sections:
            sections[-1][1].append(line)
    return VacancyRequirements(
        mandatory=_section_text(sections, "requirements"),
        optional=_section_text(sections, "optional"),
    )


def _section_heading(line: str) -> tuple[str, str] | None:
    """Тип секции и остаток строки после заголовка (пусто — заголовок один)."""
    text = line.strip()
    if not text:
        return None
    lowered = text.casefold()
    groups = (
        ("requirements", _REQUIREMENTS_HEADINGS),
        ("optional", _OPTIONAL_HEADINGS),
        ("other", _OTHER_HEADINGS),
    )
    for kind, headings in groups:
        for heading in headings:
            if lowered == heading:
                return kind, ""
            prefix = heading + ":"
            if lowered.startswith(prefix):
                return kind, text[len(prefix):].strip()
    return None


def _section_text(sections: list[tuple[str, list[str]]], kind: str) -> str:
    """Текст секций одного типа: строки без отступов, абзацы через пустую строку."""
    blocks = [
        "\n".join(line.strip() for line in lines).strip()
        for section_kind, lines in sections
        if section_kind == kind
    ]
    return "\n\n".join(block for block in blocks if block)


def profile_facts(profile: Profile) -> str:
    """Факты профиля для Decision: числа из анкеты и правило «нет в профиле — нет опыта»."""
    lines: list[str] = []
    if profile.experience_total_years is not None:
        lines.append(f"- всего опыта в IT: {years_text(profile.experience_total_years)}")
    if profile.experience_role_years is not None:
        lines.append(
            f"- опыт в роли разработчика: {years_text(profile.experience_role_years)}"
        )
    if profile.english_level:
        lines.append(f"- английский: {profile.english_level}")
    lines.append(
        "- чего нет в тексте профиля, нельзя считать подтверждённым. Отсутствие в профиле "
        "не доказывает отсутствие опыта: это значит только «не подтверждено»"
    )
    lines.append(
        "- более высокий подтверждённый уровень в той же роли или технологии "
        "подразумевает базовые знания предыдущего уровня: коммерческий опыт Java и "
        "Spring подтверждает Java Core, OOP, Collections, Generics, исключения, "
        "HTTP/REST, SQL и клиент-серверную архитектуру, если вакансия не требует "
        "более высокого уровня"
    )
    lines.append(
        "- доменный опыт, highload, конкретные процессы и инструменты, а также уровень "
        "английского не выводятся из грейда: без факта в профиле они не подтверждены"
    )
    return "\n".join(lines)


def years_text(value: float) -> str:
    """Число лет по-русски: «2.5 года», «11 лет», «1 год»."""
    whole = int(value)
    tail = whole % 100
    if value != whole:
        word = "года"
    elif 11 <= tail <= 14:
        word = "лет"
    elif whole % 10 == 1:
        word = "год"
    elif 2 <= whole % 10 <= 4:
        word = "года"
    else:
        word = "лет"
    return f"{value:g} {word}"
