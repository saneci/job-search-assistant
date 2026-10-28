"""Knockout-префильтр анкеты работодателя: вопросы формы, поля профиля, вердикт.

Анкета появляется только после нажатия «Откликнуться»: это отдельная форма,
которую рендерит hh.ru или ATS работодателя. В тексте вакансии вопросов нет,
поэтому работаем с DOM формы: читаем вопросы, сопоставляем их с полями профиля
(`prefilter.answers`) и решаем судьбу вакансии.

Вердикты:

- `reject` — ответ гарантированно ведёт к отказу (анкета требует того, чего
  в профиле нет): вакансию отсекаем до Generation, токены не тратим;
- `auto_fill` — все ответы вычислены, форму можно заполнить кодом;
- `manual_required` — вопрос незнаком или поле профиля пустое: нужен человек.

Правило fail-open: любая неопределённость (незнакомый вопрос, пустое поле,
нет подходящего варианта ответа) — это `manual_required`, но никогда не отсев.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

# Значения enum — из схемы конфига: одно место правды для допустимых значений.
from .config import WORK_FORMAT_VALUES

KIND_RADIO = "radio"
KIND_CHECKBOX = "checkbox"
KIND_TEXT = "text"
KIND_SELECT = "select"
KINDS = (KIND_RADIO, KIND_CHECKBOX, KIND_TEXT, KIND_SELECT)

VERDICT_REJECT = "reject"
VERDICT_AUTO_FILL = "auto_fill"
VERDICT_MANUAL = "manual_required"

# Кластер, который не удалось распознать: в отчёте это «неизвестный вопрос».
UNKNOWN_CLUSTER = "unknown"

# Предохранитель: больше вопросов в анкете не разбираем (это уже не отсечка).
MAX_QUESTIONS = 30
# Длина формулировки вопроса в причине отчёта: анкета может быть огромной.
REASON_TEXT_LIMIT = 120


class Answers(Protocol):
    """Значения профиля для анкеты: их даёт схема конфига (prefilter.answers).

    Значение `None` или пустая строка означают «поле не заполнено» — такой
    вопрос уходит в `manual_required` и никогда не приводит к отсеву.
    """

    skills: tuple[str, ...]
    achievements: str
    education_level: str
    location_country: str
    work_experience: float | None
    salary_min: float | None
    salary_message: str
    relocation_ready: str
    work_format: tuple[str, ...]
    work_permit: str
    notice_period: int | None
    languages: str
    has_electronic_workbook: bool | None
    travel_ready: bool | None


@dataclass(frozen=True)
class QuestionOption:
    """Вариант ответа в форме: подпись и признак «Свой вариант»."""

    label: str
    is_custom: bool = False


@dataclass(frozen=True)
class Question:
    """Вопрос анкеты, прочитанный из DOM формы отклика."""

    name: str
    text: str
    kind: str
    options: tuple[QuestionOption, ...] = ()
    required: bool = False

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(option.label for option in self.options)


@dataclass(frozen=True)
class Answer:
    """Ответ на вопрос анкеты: что проставить и чем это закончится."""

    question: Question
    cluster: str
    field: str
    value: str = ""
    # Ответ гарантированно ведёт к отказу — вакансию отсекаем.
    rejects: bool = False
    # Почему ответ не вычислен: причина для отчёта.
    reason: str = ""

    @property
    def filled(self) -> bool:
        return bool(self.value) and not self.rejects


@dataclass(frozen=True)
class PrefilterResult:
    """Вердикт Knockout-префильтра по анкете."""

    verdict: str
    answers: tuple[Answer, ...] = ()
    reason: str = ""

    @property
    def rejects(self) -> bool:
        return self.verdict == VERDICT_REJECT

    @property
    def manual(self) -> bool:
        return self.verdict == VERDICT_MANUAL

    @property
    def clusters(self) -> tuple[str, ...]:
        """Распознанные кластеры в порядке вопросов — для журнала."""
        return tuple(dict.fromkeys(answer.cluster for answer in self.answers))

    @property
    def unknown(self) -> int:
        """Сколько вопросов не удалось сопоставить с профилем."""
        return sum(1 for answer in self.answers if answer.cluster == UNKNOWN_CLUSTER)

    def answers_to_fill(self) -> tuple[Answer, ...]:
        """Ответы, которые можно проставить в форму перед отправкой."""
        return tuple(answer for answer in self.answers if answer.filled)


# --- Чтение вопросов из формы ---------------------------------------------
# Один снимок DOM вместо десятка CSS-селекторов: разметка анкеты у каждого
# работодателя своя, а снимок разбирается в Python и проверяется тестами.
# Стратегии поиска блока вопроса: fieldset / role=radiogroup / data-qa*question,
# затем legend, data-qa-заголовок, aria-label и первый абзац группы.

_QUESTIONS_BODY = r"""
  const norm = (value) => (value || "").replace(/\s+/g, " ").trim();
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const SKIP_RE = /сопроводительн|response-letter|popup-form-letter|резюме для отклика|видимость резюме/i;
  const CUSTOM_RE = /^(свой вариант|другое|другой вариант|иное)$/i;
  const GROUP_SELECTOR = "fieldset, [role='radiogroup'], [role='group'], " +
    "[data-qa*='question'], [data-qa*='Question']";
  const TITLE_SELECTORS = [
    "legend",
    "[data-qa*='question-title']",
    "[data-qa*='question-text']",
    "[data-qa='vacancy-response-popup-form-question']"
  ];
  const SKIP_DIRECT = "label, input, select, textarea, option, button, [role='radiogroup']";

  const labelOf = (control) => {
    const id = control.getAttribute("id");
    if (id) {
      const explicit = root.querySelector('label[for="' + CSS.escape(id) + '"]');
      if (explicit && visible(explicit)) return norm(explicit.innerText);
    }
    const wrapper = control.closest("label");
    if (wrapper) return norm(wrapper.innerText);
    const aria = control.getAttribute("aria-label");
    if (aria) return norm(aria);
    const described = control.getAttribute("aria-labelledby");
    if (described) {
      return norm(described.split(/\s+/).map((token) => {
        const node = document.getElementById(token);
        return node ? norm(node.innerText) : "";
      }).join(" "));
    }
    return "";
  };

  const questionOf = (block) => {
    for (const selector of TITLE_SELECTORS) {
      const node = block.querySelector(selector);
      const text = node ? norm(node.innerText) : "";
      if (text) return text;
    }
    const aria = block.getAttribute("aria-label");
    if (aria) return norm(aria);
    for (const child of Array.from(block.children)) {
      if (child.matches(SKIP_DIRECT)) continue;
      const text = norm(child.innerText);
      if (text && text.length <= 300) return text;
    }
    return "";
  };

  const records = [];
  const controls = Array.from(
    root.querySelectorAll("input[type='radio'], input[type='checkbox'], select, textarea")
  );
  for (const control of controls) {
    if (control.disabled || !visible(control)) continue;
    const block = control.closest(GROUP_SELECTOR) || control.parentElement;
    if (!block || !visible(block)) continue;
    const question = questionOf(block);
    const label = labelOf(control);
    const name = control.getAttribute("name") || control.getAttribute("id") || "";
    if (SKIP_RE.test(name + " " + label + " " + question)) continue;
    const required = control.required === true || control.getAttribute("aria-required") === "true";
    if (control.tagName === "SELECT") {
      const options = Array.from(control.options)
        .map((option) => norm(option.textContent))
        .filter((text) => text);
      records.push({
        name: name, kind: "select", question: question, label: "",
        custom: false, required: required, options: options
      });
    } else if (control.tagName === "TEXTAREA") {
      // Подсказка в поле точнее обёртки: «Напишите пару важных достижений…».
      const placeholder = norm(control.getAttribute("placeholder"));
      records.push({
        name: name, kind: "text", question: placeholder || question,
        label: placeholder || label,
        custom: false, required: required, options: []
      });
    } else {
      records.push({
        name: name, kind: control.type === "checkbox" ? "checkbox" : "radio",
        question: question, label: label, custom: CUSTOM_RE.test(label),
        required: required, options: []
      });
    }
    if (records.length >= MAX_QUESTIONS) break;
  }
  return records;
"""


def _script(page_level: bool) -> str:
    """Скрипт снимка формы: у Page корень document, у Locator — сам элемент."""
    body = _QUESTIONS_BODY.replace("MAX_QUESTIONS", str(MAX_QUESTIONS))
    if page_level:
        return "() => {\n  const root = document;\n" + body + "\n}"
    return "(root) => {\n" + body + "\n}"


def read_questions(scope: Locator | Page) -> tuple[Question, ...]:
    """Читает вопросы анкеты из открытой формы отклика.

    Ошибка чтения DOM означает «вопросов нет»: обычная форма отклика идёт
    дальше по прежнему сценарию (fail-open).
    """
    try:
        records = scope.evaluate(_script(isinstance(scope, Page)))
    except PlaywrightError:
        return ()
    return questions_from_records(records)


def questions_from_records(records: object) -> tuple[Question, ...]:
    """Собирает вопросы из снимка DOM: контролы одной группы — один вопрос.

    Радиокнопки и чекбоксы с общим `name` и одной формулировкой — варианты
    одного вопроса; `select` приходит со списком опций; `textarea` — текстовый
    вопрос. Вопрос без формулировки остаётся в списке: он уйдёт в ручные,
    а не в отсев.
    """
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return ()

    built: list[Question] = []
    index: dict[tuple[str, str], int] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        kind = clean_text(record.get("kind"))
        if kind not in KINDS:
            continue
        text = clean_text(record.get("question"))
        name = clean_text(record.get("name"))
        options = _record_options(record)
        key = (text, name)
        if key in index:
            position = index[key]
            current = built[position]
            built[position] = replace(
                current,
                options=_merge_options(current.options, options),
                # Группа обязательна, если обязателен хотя бы один её контрол.
                required=current.required or bool(record.get("required")),
            )
            continue
        index[key] = len(built)
        built.append(
            Question(
                name=name,
                text=text,
                kind=kind,
                options=options,
                required=bool(record.get("required")),
            )
        )
        if len(built) >= MAX_QUESTIONS:
            break
    return tuple(built)


def _record_options(record: Mapping[str, Any]) -> tuple[QuestionOption, ...]:
    """Варианты ответа: из списка опций `select` или из подписи контрола."""
    custom = bool(record.get("custom"))
    raw = record.get("options")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        labels = [clean_text(item) for item in raw]
    else:
        labels = [clean_text(record.get("label"))]
    return tuple(
        QuestionOption(label=label, is_custom=custom) for label in labels if label
    )


def _merge_options(
    current: tuple[QuestionOption, ...], extra: tuple[QuestionOption, ...]
) -> tuple[QuestionOption, ...]:
    """Дополняет варианты группы, сохраняя порядок и не повторяя подписи."""
    labels = {option.label for option in current}
    return current + tuple(option for option in extra if option.label not in labels)


def clean_text(value: object) -> str:
    """Однострочный текст из значения снимка DOM."""
    return " ".join(str(value or "").split())


# --- Сопоставление вопроса с профилем -------------------------------------
# У кластера одно поле профиля и своё правило сравнения. Варианты ответа
# приводятся к общим токенам, чтобы «Да», «Есть» и «Имею» у разных
# работодателей означали одно и то же.

# Ответ-согласие и ответ-отказ: встречаются почти в любой анкете.
CONSENT_TOKENS: dict[str, re.Pattern[str]] = {
    "yes": re.compile(
        r"^\s*(?:да\b|есть\b|имею\b|имеется\b|готов\b|готова\b|согласен\b|согласна\b)", re.IGNORECASE
    ),
    "no": re.compile(r"^\s*(?:нет\b|не\b|отсутству)", re.IGNORECASE),
}

# Токены конкретных значений: нужны, когда анкета перечисляет варианты.
VALUE_TOKENS: dict[str, re.Pattern[str]] = {
    "higher_tech": re.compile(r"техническ", re.IGNORECASE),
    "higher_other": re.compile(r"высшее|высш\.", re.IGNORECASE),
    "office": re.compile(r"офис|на\s+месте", re.IGNORECASE),
    "hybrid": re.compile(r"гибрид", re.IGNORECASE),
    "remote": re.compile(r"удал[её]н|дистанц|из\s+дома", re.IGNORECASE),
    "any": re.compile(r"любой|любая|любые|не\s+важно|неважно", re.IGNORECASE),
    "ru": re.compile(r"\bрф\b|росси", re.IGNORECASE),
    "other": re.compile(
        r"за\s+границ|за\s+предел|не\s+в\s+росси|друг\w*\s+стран|иностран", re.IGNORECASE
    ),
    "citizen": re.compile(r"гражданин|гражданств", re.IGNORECASE),
    "permit": re.compile(
        r"разрешени|патент|вид\s+на\s+жительство|виза|право\s+работ", re.IGNORECASE
    ),
    "sponsorship": re.compile(r"спонсор|оформлени\w*\s+разрешени|нужно\s+разрешени", re.IGNORECASE),
    "remote_only": re.compile(r"только\s+удал|без\s+переезд", re.IGNORECASE),
}

NO_OPTION_REASON = "в анкете нет подходящего варианта ответа"
NOT_FILLED_REASON = "поле профиля не заполнено"
TEXT_REQUIRED_REASON = "на вопрос нужен текстовый ответ, угадывать не будем"

# Коды стран, которые считаем «нахожусь в России».
RUSSIA_CODES = frozenset({"RU", "РФ", "РОССИЯ", "RUSSIA"})

_YEARS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:год|года|лет)", re.IGNORECASE)
# Вопрос про сумму: просят число, а не пожелания.
_SALARY_AMOUNT_RE = re.compile(r"сумм|₽|\bруб|цифр|числ|сколько", re.IGNORECASE)
# Вопрос про пожелания: уместен свободный текст из профиля.
_SALARY_VAGUE_RE = re.compile(r"пожелан|ожидан|расскаж|опиш|комментар|вилк", re.IGNORECASE)
_DAYS_RE = re.compile(r"(\d+)\s*(?:календарн\w*\s*)?(?:дн\w*|день|дня)", re.IGNORECASE)
_LEVELS = ("a1", "a2", "b1", "b2", "c1", "c2", "native")
_QUESTION_LEVEL_RE = re.compile(
    r"\b(a1|a2|b1|b2|c1|c2)\b|свободн\w*|продвинут\w*|средн\w*|начальн\w*|родной", re.IGNORECASE
)
_PROFILE_LANGUAGES_RE = re.compile(r"([a-zа-я]{2})\s*[:=]\s*([a-zа-я0-9]+)", re.IGNORECASE)
_LANGUAGE_CODES = {
    "англ": "en",
    "english": "en",
    "русск": "ru",
    "russian": "ru",
    "немец": "de",
    "нем": "de",
    "француз": "fr",
    "испан": "es",
}
_LEVEL_WORDS = {
    "родной": "native",
    "свободн": "c1",
    "продвинут": "c1",
    "средн": "b1",
    "начальн": "a1",
}


def _value(answers: Answers, field: str) -> str:
    """Строковое значение поля профиля; пустая строка — поле не заполнено."""
    return clean_text(getattr(answers, field, "") or "")


def _number_text(value: float | int) -> str:
    """Число в том виде, в каком его удобно вписать в поле формы."""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _threshold(pattern: re.Pattern[str], text: str) -> float | None:
    """Порог из формулировки вопроса («от 3 лет», «в течение 30 дней»)."""
    match = pattern.search(text or "")
    if match is None:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _manual(question: Question, cluster: str, field: str, reason: str) -> Answer:
    """Вопрос без вычисленного ответа: вакансия уходит в ручные, не в отсев."""
    return Answer(question=question, cluster=cluster, field=field, reason=reason)


def _find_option(question: Question, token: str) -> str | None:
    """Подпись варианта, соответствующего токену.

    «Свой вариант» не выбираем никогда: он требует текстового ввода, а угадывать
    за пользователя нельзя.
    """
    pattern = CONSENT_TOKENS.get(token) or VALUE_TOKENS.get(token)
    if pattern is None:
        return None
    for option in question.options:
        if option.is_custom:
            continue
        if pattern.search(option.label):
            return option.label
    return None


def _choice(
    question: Question,
    cluster: str,
    field: str,
    token: str,
    *,
    rejects: bool = False,
    reason: str = NO_OPTION_REASON,
) -> Answer:
    """Ответ выбором варианта: подпись варианта и признак гарантированного отказа."""
    if question.kind == KIND_TEXT:
        return _manual(question, cluster, field, TEXT_REQUIRED_REASON)
    label = _find_option(question, token)
    if label is None:
        return _manual(question, cluster, field, reason)
    return Answer(question=question, cluster=cluster, field=field, value=label, rejects=rejects)


# --- Правила сравнения по кластерам ---------------------------------------
# Отсев (`rejects`) ставим только там, где сама формулировка вопроса означает
# жёсткое требование: находиться в РФ, иметь право работать, иметь нужный
# стаж и образование. Остальные кластеры заполняются честным ответом.

UNKNOWN_VALUE_REASON = "неизвестное значение в профиле"
_MONEY_FIELD_REASON = "зарплату вписываем только в текстовое поле"


def _resolve_education(question: Question, answers: Answers) -> Answer:
    level = _value(answers, "education_level")
    if not level:
        return _manual(question, "education", "education_level", NOT_FILLED_REASON)
    technical = bool(re.search(r"техническ", question.text, re.IGNORECASE))
    satisfied = level == "higher_tech" or (level == "higher_other" and not technical)
    answer = _choice(
        question,
        "education",
        "education_level",
        "yes" if satisfied else "no",
        rejects=not satisfied,
    )
    if answer.value:
        return answer
    # Анкета перечисляет уровни образования, а не «да/нет».
    label = _education_option(question, level)
    if label is not None:
        return Answer(
            question=question, cluster="education", field="education_level", value=label
        )
    return answer


def _education_option(question: Question, level: str) -> str | None:
    """Вариант с уровнем образования: «высшее» без «технического», если его нет в профиле."""
    for option in question.options:
        if option.is_custom:
            continue
        if level == "higher_tech" and VALUE_TOKENS["higher_tech"].search(option.label):
            return option.label
        if level == "higher_other" and VALUE_TOKENS["higher_other"].search(option.label):
            if not VALUE_TOKENS["higher_tech"].search(option.label):
                return option.label
    return None


def _resolve_location(question: Question, answers: Answers) -> Answer:
    country = _value(answers, "location_country")
    if not country:
        return _manual(question, "location", "location_country", NOT_FILLED_REASON)
    in_russia = country.upper() in RUSSIA_CODES
    answer = _choice(
        question,
        "location",
        "location_country",
        "yes" if in_russia else "no",
        rejects=not in_russia,
    )
    if answer.value:
        return answer
    return _choice(
        question,
        "location",
        "location_country",
        "ru" if in_russia else "other",
        rejects=not in_russia,
    )


def _resolve_work_permit(question: Question, answers: Answers) -> Answer:
    permit = _value(answers, "work_permit")
    if not permit:
        return _manual(question, "work_permit", "work_permit", NOT_FILLED_REASON)
    allowed = permit != "sponsorship_needed"
    answer = _choice(
        question, "work_permit", "work_permit", "yes" if allowed else "no", rejects=not allowed
    )
    if answer.value:
        return answer
    return _choice(question, "work_permit", "work_permit", permit, rejects=not allowed)


def _resolve_experience(question: Question, answers: Answers) -> Answer:
    years = answers.work_experience
    if years is None:
        return _manual(question, "experience", "work_experience", NOT_FILLED_REASON)
    required = _threshold(_YEARS_RE, question.text)
    if required is None:
        return _manual(
            question, "experience", "work_experience", "в вопросе не указан требуемый стаж"
        )
    satisfied = float(years) >= required
    answer = _choice(
        question,
        "experience",
        "work_experience",
        "yes" if satisfied else "no",
        rejects=not satisfied,
    )
    if answer.value:
        return answer
    if question.kind == KIND_TEXT:
        return Answer(
            question=question,
            cluster="experience",
            field="work_experience",
            value=_number_text(years),
            rejects=not satisfied,
        )
    return answer


def _resolve_salary(question: Question, answers: Answers) -> Answer:
    """Зарплата: сумма, если просят число, иначе свободный текст про пожелания."""
    amount = answers.salary_min
    message = clean_text(getattr(answers, "salary_message", ""))
    if amount is None and not message:
        return _manual(question, "salary", "salary_min", NOT_FILLED_REASON)
    if question.kind != KIND_TEXT:
        return _manual(question, "salary", "salary_min", _MONEY_FIELD_REASON)
    text = question.text or ""
    if _SALARY_AMOUNT_RE.search(text) and amount is not None:
        return _salary_amount(question, amount)
    if _SALARY_VAGUE_RE.search(text) and message:
        return _salary_message(question, message)
    if amount is not None:
        return _salary_amount(question, amount)
    return _salary_message(question, message)


def _salary_amount(question: Question, amount: float) -> Answer:
    """Ответ числом: минимальная зарплата из профиля."""
    return Answer(
        question=question, cluster="salary", field="salary_min", value=_number_text(amount)
    )


def _salary_message(question: Question, message: str) -> Answer:
    """Ответ свободным текстом: пожелания по зарплате из профиля."""
    return Answer(
        question=question, cluster="salary", field="salary_message", value=message
    )


_SKILL_MISS_REASON = "технология из вопроса не найдена в prefilter.answers.skills"
_ACHIEVEMENTS_FIELD_REASON = "достижения вписываем только в текстовое поле"


def _skill_answer(question: Question, answers: Answers) -> Answer | None:
    """Вопрос про стек: технология из профиля упомянута в формулировке.

    Отсутствие технологии в списке навыков — не повод отвечать «Нет»: профиль
    мог быть неполным, поэтому такой вопрос уходит в ручные (fail-open).
    """
    skills = tuple(skill for skill in (getattr(answers, "skills", ()) or ()) if skill)
    if not skills:
        return None
    if any(_skill_pattern(skill).search(question.text or "") for skill in skills):
        return _choice(question, "skills", "skills", "yes")
    return None


def _skill_pattern(skill: str) -> re.Pattern[str]:
    """Шаблон технологии: «spring boot» ловит и «Spring Boot», и «Spring/Boot».

    Границы слова не дают «java» совпасть с «JavaScript».
    """
    parts = [re.escape(part) for part in clean_text(skill).split()]
    if not parts:
        return re.compile(r"(?!)")
    body = r"[\s\-/]*".join(parts)
    return re.compile(rf"(?<![0-9a-zа-яё]){body}(?![0-9a-zа-яё])", re.IGNORECASE)


def _resolve_achievements(question: Question, answers: Answers) -> Answer:
    """Достижения: свободный текст профиля в текстовое поле анкеты."""
    text = clean_text(getattr(answers, "achievements", ""))
    if not text:
        return _manual(question, "achievements", "achievements", NOT_FILLED_REASON)
    if question.kind != KIND_TEXT:
        return _manual(question, "achievements", "achievements", _ACHIEVEMENTS_FIELD_REASON)
    return Answer(question=question, cluster="achievements", field="achievements", value=text)


def _resolve_relocation(question: Question, answers: Answers) -> Answer:
    ready = _value(answers, "relocation_ready")
    if not ready:
        return _manual(question, "relocation", "relocation_ready", NOT_FILLED_REASON)
    if ready not in ("yes", "no", "remote_only"):
        return _manual(question, "relocation", "relocation_ready", UNKNOWN_VALUE_REASON)
    # Переезд не считаем отсечкой: вопрос «готовы ли» ещё не требование.
    return _choice(question, "relocation", "relocation_ready", ready)


def _resolve_work_format(question: Question, answers: Answers) -> Answer:
    """Формат работы: в профиле может быть список, порядок = приоритет."""
    formats = tuple(getattr(answers, "work_format", ()) or ())
    if not formats:
        return _manual(question, "work_format", "work_format", NOT_FILLED_REASON)
    for work_format in formats:
        if work_format not in WORK_FORMAT_VALUES:
            return _manual(question, "work_format", "work_format", UNKNOWN_VALUE_REASON)
    # Берём первый формат из профиля, который есть среди вариантов анкеты.
    for work_format in formats:
        answer = _choice(question, "work_format", "work_format", work_format)
        if answer.value:
            return answer
    return _manual(question, "work_format", "work_format", NO_OPTION_REASON)


def _resolve_notice_period(question: Question, answers: Answers) -> Answer:
    days = answers.notice_period
    if days is None:
        return _manual(question, "notice_period", "notice_period", NOT_FILLED_REASON)
    limit = _threshold(_DAYS_RE, question.text)
    satisfied = limit is None or float(days) <= limit
    if question.kind == KIND_TEXT:
        return Answer(
            question=question,
            cluster="notice_period",
            field="notice_period",
            value=_number_text(days),
            rejects=not satisfied,
        )
    if limit is None:
        return _manual(
            question, "notice_period", "notice_period", "в вопросе не указан срок выхода"
        )
    return _choice(
        question,
        "notice_period",
        "notice_period",
        "yes" if satisfied else "no",
        rejects=not satisfied,
    )


def _resolve_languages(question: Question, answers: Answers) -> Answer:
    languages = _value(answers, "languages")
    if not languages:
        return _manual(question, "languages", "languages", NOT_FILLED_REASON)
    code = _language_code(question.text)
    if not code:
        return _manual(question, "languages", "languages", "язык вопроса не распознан")
    profile_level = _profile_level(languages, code)
    if not profile_level:
        return _manual(
            question, "languages", "languages", "в профиле не указан уровень этого языка"
        )
    required = _question_level(question.text)
    # Язык не отсекает: работодатель может счесть уровень достаточным.
    satisfied = not required or _level_value(profile_level) >= _level_value(required)
    return _choice(question, "languages", "languages", "yes" if satisfied else "no")


def _flag_resolver(cluster: str, field: str) -> Callable[[Question, Answers], Answer]:
    """Правило «да/нет» для булевых полей профиля."""

    def resolve(question: Question, answers: Answers) -> Answer:
        value = getattr(answers, field, None)
        if value is None:
            return _manual(question, cluster, field, NOT_FILLED_REASON)
        return _choice(question, cluster, field, "yes" if value else "no")

    return resolve


def _language_code(text: str) -> str:
    """Код языка из формулировки вопроса («Владеете английским?»)."""
    lowered = (text or "").lower()
    for word, code in _LANGUAGE_CODES.items():
        if word in lowered:
            return code
    return ""


def _profile_level(languages: str, code: str) -> str:
    """Уровень языка из профиля: строка вида «ru: native, en: b2»."""
    for match in _PROFILE_LANGUAGES_RE.finditer(languages or ""):
        if match.group(1).lower() == code:
            return match.group(2).lower()
    return ""


def _question_level(text: str) -> str:
    """Требуемый уровень языка из формулировки вопроса (пусто — не указан)."""
    match = _QUESTION_LEVEL_RE.search(text or "")
    if match is None:
        return ""
    found = (match.group(1) or match.group(0)).lower()
    if found in _LEVELS:
        return found
    for word, level in _LEVEL_WORDS.items():
        if found.startswith(word):
            return level
    return ""


def _level_value(level: str) -> int:
    """Порядковый номер уровня языка: для сравнения требований."""
    return _LEVELS.index(level) if level in _LEVELS else -1


# --- Кластеры и вердикт ---------------------------------------------------


@dataclass(frozen=True)
class ClusterRule:
    """Кластер вопроса: формулировка, поле профиля и правило сравнения.

    Порядок в `CLUSTER_RULES` важен: вопрос попадает в первый подходящий
    кластер, поэтому частные формулировки идут раньше общих.
    """

    cluster: str
    field: str
    question: re.Pattern[str]
    resolve: Callable[[Question, Answers], Answer]


CLUSTER_RULES: tuple[ClusterRule, ...] = (
    ClusterRule(
        "achievements",
        "achievements",
        re.compile(
            r"достижен|о себе|расскажите о себе|несколько слов|дополнительн\w*\s+информац",
            re.IGNORECASE,
        ),
        _resolve_achievements,
    ),
    ClusterRule(
        "education",
        "education_level",
        re.compile(r"образован|высшее|вуз", re.IGNORECASE),
        _resolve_education,
    ),
    ClusterRule(
        "work_permit",
        "work_permit",
        re.compile(r"право\s+(?:на\s+)?работ|гражданств|патент|виза|разрешени\w*\s+на\s+работ", re.IGNORECASE),
        _resolve_work_permit,
    ),
    ClusterRule(
        "location",
        "location_country",
        re.compile(r"наход\w*|стран|город|регион|прожива|место\s+жительств", re.IGNORECASE),
        _resolve_location,
    ),
    ClusterRule(
        "experience",
        "work_experience",
        # Только вопросы со стажем в цифрах: «есть ли опыт со Spring» — это навыки.
        re.compile(r"\d+(?:[.,]\d+)?\s*(?:год|года|лет)|стаж", re.IGNORECASE),
        _resolve_experience,
    ),
    ClusterRule(
        "salary",
        "salary_min",
        re.compile(r"зарплат|оклад|доход|оплат|вознагражд", re.IGNORECASE),
        _resolve_salary,
    ),
    ClusterRule(
        "relocation",
        "relocation_ready",
        re.compile(r"переезд|переехать|релокац", re.IGNORECASE),
        _resolve_relocation,
    ),
    ClusterRule(
        "work_format",
        "work_format",
        re.compile(r"формат\s+работ|офис|удал[её]н|гибрид|дистанц", re.IGNORECASE),
        _resolve_work_format,
    ),
    ClusterRule(
        "notice_period",
        "notice_period",
        re.compile(r"срок\w*\s+(?:выход|уведомл)|выйти\s+на\s+работу|отработк|уведомлени", re.IGNORECASE),
        _resolve_notice_period,
    ),
    ClusterRule(
        "languages",
        "languages",
        re.compile(
            r"англ|english|немец|француз|испан|китайск|языков|языками|языках", re.IGNORECASE
        ),
        _resolve_languages,
    ),
    ClusterRule(
        "electronic_workbook",
        "has_electronic_workbook",
        re.compile(r"электронн\w*\s+трудов|этк", re.IGNORECASE),
        _flag_resolver("electronic_workbook", "has_electronic_workbook"),
    ),
    ClusterRule(
        "travel_ready",
        "travel_ready",
        re.compile(r"командировк|разъездн", re.IGNORECASE),
        _flag_resolver("travel_ready", "travel_ready"),
    ),
)


def match_cluster(text: str) -> ClusterRule | None:
    """Кластер по формулировке вопроса: первый подходящий по порядку правил."""
    for rule in CLUSTER_RULES:
        if rule.question.search(text or ""):
            return rule
    return None


def evaluate(questions: Sequence[Question], answers: Answers) -> PrefilterResult:
    """Вердикт по анкете: отсев, автозаполнение или ручной разбор.

    Отсев важнее ручного разбора: если хотя бы один ответ гарантированно ведёт
    к отказу, вакансию не откликаем, даже когда другие вопросы непонятны.
    """
    collected = tuple(_answer_question(question, answers) for question in questions)
    rejected = [answer for answer in collected if answer.rejects]
    if rejected:
        return PrefilterResult(VERDICT_REJECT, collected, _reject_reason(rejected[0]))
    manual = [answer for answer in collected if not answer.filled]
    if manual:
        return PrefilterResult(VERDICT_MANUAL, collected, _manual_reason(manual[0]))
    return PrefilterResult(VERDICT_AUTO_FILL, collected)


def _answer_question(question: Question, answers: Answers) -> Answer:
    """Ответ на один вопрос: кластер по формулировке, затем упоминание навыка."""
    rule = match_cluster(question.text)
    if rule is not None:
        return rule.resolve(question, answers)
    skill = _skill_answer(question, answers)
    if skill is not None:
        return skill
    return _manual(question, UNKNOWN_CLUSTER, "", "вопрос не сопоставлен с профилем")


def _question_label(question: Question) -> str:
    """Формулировка вопроса для отчёта: без значений профиля и без обрезки глазами."""
    text = clean_text(question.text) or "без формулировки"
    if len(text) <= REASON_TEXT_LIMIT:
        return text
    return text[:REASON_TEXT_LIMIT].rstrip() + "…"


def question_label(question: Question) -> str:
    """Публичное имя формулировки вопроса: нужно отчёту и файлу ответов."""
    return _question_label(question)


def _reject_reason(answer: Answer) -> str:
    """Причина отсева: сам вопрос, без персональных данных из профиля."""
    return (
        f"Анкета работодателя: на вопрос «{_question_label(answer.question)}» "
        "профиль даёт отрицательный ответ"
    )


def _manual_reason(answer: Answer) -> str:
    """Причина ручного разбора: вопрос и что именно не удалось вычислить."""
    reason = answer.reason or NO_OPTION_REASON
    return f"Анкета работодателя: вопрос «{_question_label(answer.question)}» — {reason}"


# --- Ответы модели на нерешённые вопросы -----------------------------------
# Профиль закрывает типовые вопросы анкеты. Остальные (тесты по вакансии,
# нестандартные формулировки) отправляются одним Generation-вызовом вместе
# с заявкой на письмо: один вызов — письмо и ответы сразу.

LLM_CLUSTER = "llm"


def unanswered_questions(result: PrefilterResult) -> tuple[Question, ...]:
    """Вопросы без ответа из профиля: их закрывает один LLM-вызов."""
    return tuple(
        answer.question
        for answer in result.answers
        if not answer.filled and not answer.rejects
    )


def question_lines(questions: Sequence[Question]) -> str:
    """Вопросы для промпта: номер, формулировка и варианты ответа.

    Номер нужен, чтобы модель вернула ответы по номерам: формулировки
    у разных работодателей повторяются и как ключ ненадёжны.
    """
    lines: list[str] = []
    for number, question in enumerate(questions, 1):
        text = _question_label(question)
        if question.kind == KIND_TEXT:
            lines.append(f"{number}. {text} (нужен текст)")
        elif question.labels:
            lines.append(f"{number}. {text} Варианты: {' | '.join(question.labels)}")
        else:
            lines.append(f"{number}. {text}")
    return "\n".join(lines)


def llm_answers(
    questions: Sequence[Question], values: Mapping[int, str]
) -> tuple[Answer, ...]:
    """Ответы модели: номер вопроса с единицы и текст ответа.

    Ответ, которого нет среди вариантов анкеты, отбрасывается: проставить его
    в форму нельзя, а подменять чужим вариантом нельзя тем более.
    """
    collected: list[Answer] = []
    for number, raw in sorted(values.items()):
        if not 1 <= number <= len(questions):
            continue
        question = questions[number - 1]
        value = _match_answer(question, raw)
        if value is None:
            continue
        collected.append(Answer(question=question, cluster=LLM_CLUSTER, field="", value=value))
    return tuple(collected)


def _match_answer(question: Question, raw: str) -> str | None:
    """Подпись варианта из ответа модели или текст для текстового вопроса."""
    text = clean_text(raw)
    if not text:
        return None
    if question.kind == KIND_TEXT:
        return text
    lowered = text.casefold()
    for option in question.options:
        if clean_text(option.label).casefold() == lowered:
            return option.label
    for option in question.options:
        if lowered in clean_text(option.label).casefold():
            return option.label
    return None


def without_rejects(result: PrefilterResult) -> PrefilterResult:
    """Снимает отсев: в тесте ответ влияет на балл, а не на право отклика."""
    answers = tuple(replace(answer, rejects=False) for answer in result.answers)
    manual = any(not answer.filled for answer in answers)
    verdict = VERDICT_MANUAL if manual else VERDICT_AUTO_FILL
    return PrefilterResult(verdict, answers, result.reason)


def merge_answers(
    result: PrefilterResult, extra: Sequence[Answer]
) -> tuple[Answer, ...]:
    """Итоговые ответы для формы: профиль первым, ответы модели — на остальное.

    Модель не переопределяет профиль: ответ из `prefilter.answers` точнее,
    а расхождение с ним означало бы выдумку.
    """
    collected = list(result.answers_to_fill())
    taken = {answer.question.name or answer.question.text for answer in collected}
    for answer in extra:
        if not answer.filled:
            continue
        key = answer.question.name or answer.question.text
        if key in taken:
            continue
        taken.add(key)
        collected.append(answer)
    return tuple(collected)
