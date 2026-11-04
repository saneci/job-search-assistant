"""Тесты Knockout-префильтра: снимок формы, ответы профиля, вердикт и пайплайн."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError

from agent.config import ConfigError, load_config
from agent.hh_vacancy import (
    STATUS_SENT,
    TEST_FAILED,
    TEST_PASSED,
    ResponseForm,
    ResponseResult,
    apply_answers,
)
from agent.llm import DecisionResult, GenerationResult
from agent.logging_setup import RunLogger
from agent.models import VacancyCard, VacancyDetails
from agent.pipeline import QUESTIONARY_MANUAL_REASON, QUEUE_REASON_TEXTS, _process_vacancy
from agent.prefilter import (
    VERDICT_AUTO_FILL,
    VERDICT_MANUAL,
    VERDICT_REJECT,
    Answer,
    Question,
    QuestionOption,
    evaluate,
    llm_answers,
    manual_hint,
    questions_from_records,
    read_questions,
)
from agent.profile import Profile
from agent.report import RunStats


@dataclass(frozen=True)
class Answers:
    """Ответы профиля для тестов: те же поля, что в pipeline.prefilter.answers."""

    skills: tuple[str, ...] = ()
    achievements: str = ""
    education_level: str = ""
    location_country: str = ""
    work_experience: float | None = None
    work_experience_total: float | None = None
    work_experience_profile: float | None = None
    salary_min: float | None = None
    salary_message: str = ""
    relocation_ready: str = ""
    work_format: tuple[str, ...] = ()
    work_permit: str = ""
    notice_period: int | None = None
    languages: str = ""
    has_electronic_workbook: bool | None = None
    travel_ready: bool | None = None
    self_employed: str = ""
    age: int | None = None
    russian_labour_code_expirience: float | None = None
    is_project_based_experience: bool | None = None
    payment_per_unit_engagement: bool | None = None
    has_remote_workspace: bool | None = None


def radio(question: str, labels: list[str], name: str = "q1") -> Question:
    """Вопрос-выбор: варианты ответа одной группы."""
    return Question(
        name=name,
        text=question,
        kind="radio",
        options=tuple(QuestionOption(label=label) for label in labels),
    )


# --- Снимок формы ---------------------------------------------------------


def test_records_of_one_group_become_one_question() -> None:
    """Радиокнопки с общим именем и формулировкой — варианты одного вопроса."""
    records = [
        {"name": "q1", "kind": "radio", "question": "Вы находитесь в РФ?", "label": "Да"},
        {
            "name": "q1",
            "kind": "radio",
            "question": "Вы находитесь в РФ?",
            "label": "Нет",
            "required": True,
        },
    ]

    questions = questions_from_records(records)

    assert len(questions) == 1
    assert questions[0].labels == ("Да", "Нет")
    assert questions[0].required is True


def test_radio_record_keeps_label_as_option() -> None:
    """Подпись радиокнопки — вариант ответа: снимок приходит с пустым списком опций."""
    records = [
        {"name": "q1", "kind": "radio", "question": "Вы в РФ?", "label": "Да", "options": []},
        {"name": "q1", "kind": "radio", "question": "Вы в РФ?", "label": "Нет", "options": []},
    ]

    questions = questions_from_records(records)

    assert len(questions) == 1
    assert questions[0].labels == ("Да", "Нет")


def test_select_record_keeps_its_options() -> None:
    record = {"name": "fmt", "kind": "select", "question": "Формат работы"}
    record["options"] = ["Офис", "Удалённо"]

    questions = questions_from_records([record])

    assert questions[0].kind == "select"
    assert questions[0].labels == ("Офис", "Удалённо")


def test_records_of_unknown_kind_and_garbage_are_ignored() -> None:
    assert questions_from_records([{"kind": "slider", "question": "Оцените"}]) == ()
    assert questions_from_records("нет") == ()
    assert questions_from_records(None) == ()


def test_generic_placeholder_is_not_a_question() -> None:
    """Подсказка «Писать тут» не становится вопросом: по ней ничего не распознать."""
    record = {"name": "salary", "kind": "text", "question": "", "hint": "Писать тут"}

    assert questions_from_records([record])[0].text == ""


def test_invisible_question_text_is_cleaned() -> None:
    """Невидимые символы разметки не попадают в формулировку вопроса."""
    record = {"name": "q1", "kind": "text", "question": "\u200b \ufeff", "hint": ""}

    assert questions_from_records([record])[0].text == ""


def test_block_question_wins_over_placeholder() -> None:
    """Настоящая формулировка блока важнее подсказки поля: у hh.ru там «Писать тут»."""
    record = {
        "name": "salary",
        "kind": "text",
        "question": "Ваши зарплатные ожидания",
        "hint": "Писать тут",
    }

    assert questions_from_records([record])[0].text == "Ваши зарплатные ожидания"


def test_meaningful_hint_fills_empty_question() -> None:
    """Осмысленная подсказка остаётся формулировкой, если блока вопроса нет."""
    record = {
        "name": "achievements",
        "kind": "text",
        "question": "",
        "hint": "Напишите пару важных достижений…",
    }

    assert questions_from_records([record])[0].text == "Напишите пару важных достижений…"


class _BrokenScope:
    """Форма перерисовалась, и снимок DOM не удался."""

    def evaluate(self, script: str) -> Any:
        raise PlaywrightError("форма перерисовалась")


def test_read_questions_returns_empty_on_dom_error() -> None:
    assert read_questions(_BrokenScope()) == ()  # type: ignore[arg-type]


class _EmptyList:
    """Список, в котором ничего не нашлось."""

    def count(self) -> int:
        return 0

    @property
    def first(self) -> "_EmptyList":
        return self


class _EmptyScope:
    """Форма, в которой нет ни варианта ответа, ни поля ввода."""

    def get_by_label(self, label: str, exact: bool = True) -> _EmptyList:
        return _EmptyList()

    def locator(self, selector: str) -> _EmptyList:
        return _EmptyList()


def test_apply_answers_counts_only_found_fields() -> None:
    """Ответ, которого нет в форме, не считается заполненным."""
    question = radio("Вы находитесь в РФ?", ["Да", "Нет"])
    answer = Answer(question=question, cluster="location", field="location_country", value="Да")

    assert apply_answers(_EmptyScope(), (answer,)) == 0  # type: ignore[arg-type]


# --- Вердикт по анкете ----------------------------------------------------


def test_location_question_rejects_candidate_outside_russia() -> None:
    question = radio("Вы находитесь в РФ?", ["Да", "Нет"])

    result = evaluate([question], Answers(location_country="KZ"))

    assert result.verdict == VERDICT_REJECT
    assert "Вы находитесь в РФ?" in result.reason
    assert result.rejects is True


def test_location_question_is_answered_for_russia() -> None:
    question = radio("Вы находитесь в РФ?", ["Да", "Нет"])

    result = evaluate([question], Answers(location_country="RU"))

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Да"
    assert result.clusters == ("location",)


def test_unknown_question_goes_to_manual_and_never_rejects() -> None:
    question = radio("Какой у вас любимый цвет?", ["Красный", "Синий"])

    result = evaluate([question], Answers(location_country="RU"))

    assert result.verdict == VERDICT_MANUAL
    assert result.unknown == 1
    assert result.rejects is False
    assert result.reason.startswith("Анкета работодателя")


def test_empty_profile_field_goes_to_manual() -> None:
    question = radio("Вы находитесь в РФ?", ["Да", "Нет"])

    result = evaluate([question], Answers())

    assert result.verdict == VERDICT_MANUAL
    assert result.rejects is False


def test_reject_wins_over_unknown_question() -> None:
    questions = [
        radio("Вы находитесь в РФ?", ["Да", "Нет"]),
        radio("Какой у вас любимый цвет?", ["Красный"], name="q2"),
    ]

    result = evaluate(questions, Answers(location_country="KZ"))

    assert result.verdict == VERDICT_REJECT


def test_empty_form_is_auto_fill() -> None:
    """Форма без вопросов — обычный отклик: ничего заполнять не нужно."""
    result = evaluate((), Answers())

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers == ()
    assert result.answers_to_fill() == ()


@pytest.mark.parametrize(("years", "verdict"), [(7, VERDICT_AUTO_FILL), (2, VERDICT_REJECT)])
def test_experience_compares_with_requirement(years: float, verdict: str) -> None:
    question = radio("Опыт коммерческой разработки на Java от 3 лет?", ["Да", "Нет"])

    result = evaluate([question], Answers(work_experience=years))

    assert result.verdict == verdict


def test_custom_option_is_not_guessed() -> None:
    """«Свой вариант» требует ввода: при единственном таком варианте — ручной разбор."""
    question = Question(
        name="q1",
        text="Вы находитесь в РФ?",
        kind="radio",
        options=(QuestionOption(label="Свой вариант", is_custom=True),),
    )

    result = evaluate([question], Answers(location_country="KZ"))

    assert result.verdict == VERDICT_MANUAL


def test_salary_is_written_into_text_question() -> None:
    question = Question(name="salary", text="Ожидаемая зарплата, ₽", kind="text")

    result = evaluate([question], Answers(salary_min=250000))

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers_to_fill()[0].value == "250000"


def test_work_format_chooses_matching_option() -> None:
    question = radio("Какой формат работы вам подходит?", ["Офис", "Удалённо", "Гибрид"])

    result = evaluate([question], Answers(work_format=("remote",)))

    assert result.answers[0].value == "Удалённо"


def test_work_format_list_uses_profile_priority() -> None:
    """В профиле список форматов: берём первый, который есть в анкете."""
    question = radio("Какой формат работы вам подходит?", ["Офис", "Удалённо"])

    result = evaluate([question], Answers(work_format=("hybrid", "remote")))

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Удалённо"


def test_work_format_without_matching_option_is_manual() -> None:
    question = radio("Какой формат работы вам подходит?", ["Офис"])

    result = evaluate([question], Answers(work_format=("hybrid", "remote")))

    assert result.verdict == VERDICT_MANUAL


def test_salary_message_is_used_for_vague_question() -> None:
    """«Пожелания по зарплате» — свободный текст, а не голая сумма."""
    question = Question(name="salary", text="Ваши пожелания по зарплате?", kind="text")
    message = "Ориентируюсь на рынок для Middle, готов обсудить вилку."

    result = evaluate([question], Answers(salary_min=150000, salary_message=message))

    assert result.answers_to_fill()[0].value == message
    assert result.answers_to_fill()[0].field == "salary_message"


def test_salary_amount_is_used_when_sum_is_asked() -> None:
    question = Question(name="salary", text="Ожидаемая зарплата, ₽", kind="text")

    result = evaluate([question], Answers(salary_min=150000, salary_message="Пожелания"))

    assert result.answers_to_fill()[0].value == "150000"
    assert result.answers_to_fill()[0].field == "salary_min"


def test_salary_message_alone_answers_vague_question() -> None:
    question = Question(name="salary", text="Расскажите про зарплатные ожидания", kind="text")

    result = evaluate([question], Answers(salary_message="Готов обсудить вилку."))

    assert result.answers_to_fill()[0].value == "Готов обсудить вилку."


def test_skill_question_is_answered_yes() -> None:
    question = radio(
        "Есть ли у вас опыт работы со Spring / Spring Boot?", ["Да", "Нет", "Свой вариант"]
    )

    result = evaluate([question], Answers(skills=("java", "spring boot")))
    answers = result.answers_to_fill()

    assert result.verdict == VERDICT_AUTO_FILL
    assert answers[0].value == "Да"
    assert answers[0].cluster == "skills"


def test_unknown_skill_goes_to_manual_not_no() -> None:
    """Технологии нет в списке — не отвечаем «Нет», а отдаём человеку."""
    question = radio("Есть ли у вас опыт с TLS криптографией?", ["Да", "Нет", "Свой вариант"])

    result = evaluate([question], Answers(skills=("java", "spring")))

    assert result.verdict == VERDICT_MANUAL
    assert result.rejects is False


def test_skill_matching_respects_word_boundaries() -> None:
    """«java» не должен совпадать с «JavaScript»."""
    question = radio("Есть ли у вас опыт с JavaScript?", ["Да", "Нет"])

    result = evaluate([question], Answers(skills=("java",)))

    assert result.verdict == VERDICT_MANUAL


def test_experience_question_is_not_swallowed_by_skills() -> None:
    """Вопрос про стаж на Java — это опыт, а не «навык Java»."""
    question = radio("Опыт коммерческой разработки на Java от 4 лет?", ["Да", "Нет"])

    result = evaluate([question], Answers(skills=("java",), work_experience=2.5))

    assert result.verdict == VERDICT_REJECT


def test_skills_answer_question_about_java_language() -> None:
    question = radio(
        "Уверенное владение языком Java (17+), ООП, коллекциями и JVM?", ["Да", "Нет"]
    )

    result = evaluate([question], Answers(skills=("java",)))

    assert result.answers_to_fill()[0].value == "Да"


def test_achievements_fill_text_question() -> None:
    question = Question(
        name="about",
        text="Напишите пару важных достижений для отклика на вакансию",
        kind="text",
    )
    text = "Перевёл монолит на микросервисы и сократил время сборки в два раза."

    result = evaluate([question], Answers(achievements=text))

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers_to_fill()[0].value == text
    assert result.answers_to_fill()[0].field == "achievements"


def test_empty_achievements_go_to_manual() -> None:
    question = Question(name="about", text="Ваши достижения?", kind="text")

    result = evaluate([question], Answers())

    assert result.verdict == VERDICT_MANUAL


def test_education_option_is_chosen_by_level() -> None:
    """Анкета перечисляет уровни: выбираем свой, а не первое «высшее»."""
    question = radio(
        "Ваше образование?", ["Высшее техническое", "Высшее", "Среднее"], name="edu"
    )

    result = evaluate([question], Answers(education_level="higher_other"))

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Высшее"


def test_missing_education_is_rejected_by_direct_question() -> None:
    question = radio("У вас есть высшее образование?", ["Да", "Нет"])

    result = evaluate([question], Answers(education_level="none"))

    assert result.verdict == VERDICT_REJECT


# --- Конфигурация ---------------------------------------------------------


def _write_config(tmp_path: Path, updates: dict[str, Any] | None = None) -> Path:
    data: dict[str, Any] = {
        "search": {"queries": "Java", "area": "Москва"},
        "pipeline": {"limit_per_run": 5, "delay_between_actions": {"from": 0, "to": 0}},
    }
    for key, value in (updates or {}).items():
        current = data.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_prefilter_is_off_by_default(tmp_path: Path) -> None:
    """Схема по умолчанию не меняет прежнее поведение отклика."""
    config = load_config(_write_config(tmp_path))

    assert config.pipeline.prefilter.enabled is False
    assert config.pipeline.prefilter.submit_auto_fill is False
    assert config.pipeline.prefilter.answers.location_country == ""
    assert config.pipeline.prefilter.answers.work_experience is None


def test_prefilter_answers_come_from_local_file(tmp_path: Path) -> None:
    _write_config(tmp_path, {"pipeline": {"prefilter": {"enabled": True}}})
    local = {"pipeline": {"prefilter": {"answers": {"location_country": "RU", "work_experience": 7}}}}
    (tmp_path / "config.local.yaml").write_text(
        json.dumps(local, ensure_ascii=False), encoding="utf-8"
    )

    config = load_config(tmp_path / "config.yaml")

    assert config.pipeline.prefilter.enabled is True
    assert config.pipeline.prefilter.answers.location_country == "RU"
    assert config.pipeline.prefilter.answers.work_experience == 7.0


def test_prefilter_answers_are_not_read_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Личные ответы живут только в config.local.yaml."""
    monkeypatch.setenv("PIPELINE__PREFILTER__ANSWERS__LOCATION_COUNTRY", "KZ")

    config = load_config(_write_config(tmp_path))

    assert config.pipeline.prefilter.answers.location_country == ""


def test_none_labour_code_experience_means_empty(tmp_path: Path) -> None:
    """`none` в числовом поле значит «не заполнено», а не ошибку схемы."""
    config = load_config(
        _write_config(
            tmp_path,
            {
                "pipeline": {
                    "prefilter": {
                        "answers": {"russian_labour_code_expirience": "none"}
                    }
                }
            },
        )
    )

    assert config.pipeline.prefilter.answers.russian_labour_code_expirience is None


def test_self_employed_unknown_value_fails_fast(tmp_path: Path) -> None:
    """Статус ИП принимает только yes/no/maybe."""
    with pytest.raises(ConfigError, match="self_employed"):
        load_config(
            _write_config(
                tmp_path,
                {
                    "pipeline": {
                        "prefilter": {"answers": {"self_employed": "позже"}}
                    }
                },
            )
        )


def test_unknown_prefilter_answer_fails_fast(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path, {"pipeline": {"prefilter": {"answers": {"education_level": "высшее"}}}}
    )

    with pytest.raises(ConfigError, match="pipeline.prefilter.answers.education_level"):
        load_config(path)


def test_work_format_list_and_free_text_are_accepted(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "pipeline": {
                "prefilter": {
                    "answers": {
                        "work_format": ["hybrid", "remote"],
                        "salary_message": "Ориентируюсь на рынок.\n\n  Готов обсудить вилку.  ",
                        "achievements": "Перевёл\n монолит на  микросервисы.",
                    }
                }
            }
        },
    )

    answers = load_config(path).pipeline.prefilter.answers

    assert answers.work_format == ("hybrid", "remote")
    # Свободный текст — одной строкой: в форме это обычное поле ввода.
    assert answers.salary_message == "Ориентируюсь на рынок. Готов обсудить вилку."
    assert answers.achievements == "Перевёл монолит на микросервисы."


def test_skills_are_read_as_a_list(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "pipeline": {
                "prefilter": {
                    "answers": {"skills": ["java", " spring ", "spring", "junit"]}
                }
            }
        },
    )

    answers = load_config(path).pipeline.prefilter.answers

    assert answers.skills == ("java", "spring", "junit")


def test_unknown_work_format_in_list_fails_fast(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {"pipeline": {"prefilter": {"answers": {"work_format": ["remote", "дача"]}}}},
    )

    with pytest.raises(ConfigError, match="pipeline.prefilter.answers.work_format"):
        load_config(path)


def text_question(text: str, name: str = "q1") -> Question:
    """Текстовый вопрос формы."""
    return Question(name=name, text=text, kind="text")


def test_salary_short_zp_is_recognized() -> None:
    """«Ваши зп ожидания?» — это вопрос про зарплату, а не неизвестный."""
    result = evaluate((text_question("Ваши зп ожидания?"),), Answers(salary_min=250000))

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "250000"


def test_age_question_is_answered() -> None:
    """Возраст из профиля вписывается в текстовое поле."""
    result = evaluate(
        (text_question("Ваш полный возраст (количество полных лет)"),), Answers(age=25)
    )

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "25"


def test_labour_code_experience_uses_its_own_field() -> None:
    """Подтверждённый стаж по трудовой книжке берётся из отдельного поля."""
    question = radio(
        "Имеется ли у вас подтвержденный стаж работы по данной должности от 3-х лет, "
        "зафиксированный в трудовой книжке?",
        ["Да", "Нет"],
    )

    result = evaluate(
        (question,),
        Answers(work_experience_profile=2.5, russian_labour_code_expirience=5),
    )

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Да"


def test_project_experience_flag_answers_choice() -> None:
    """Флаг проектного опыта закрывает вопрос выбором «Да»."""
    result = evaluate(
        (radio("Является ли ваш опыт проектной разработкой?", ["Да", "Нет"]),),
        Answers(is_project_based_experience=True),
    )

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Да"


def test_payment_per_unit_answers_choice() -> None:
    """Готовность к оплате за результат закрывает вопрос выбором."""
    result = evaluate(
        (radio("Готовы ли вы работать с оплатой за единицу результата?", ["Да", "Нет"]),),
        Answers(payment_per_unit_engagement=False),
    )

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Нет"


def test_remote_workspace_text_answers_ready() -> None:
    """Оснащение домашнего офиса в текстовое поле отвечается без рода."""
    result = evaluate(
        (
            text_question(
                'Оснащение вашего домашнего офиса: укажите "Готов(а)", если есть гарнитура '
                "и веб-камера"
            ),
        ),
        Answers(has_remote_workspace=True),
    )

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Всё необходимое есть"


def test_llm_answer_drops_meta_about_profile() -> None:
    """Ответ на анкету не ссылается на профиль: мета-фраза отбрасывается."""
    question = text_question("Расскажите об автоматизации тестирования")
    answers = llm_answers(
        (question,),
        {
            1: (
                "На последнем месте работы я занимался разработкой и внедрением "
                "тестового покрытия. Информация о ручном тестировании на этом месте "
                "в профиле не указана."
            )
        },
    )

    assert answers[0].value == (
        "На последнем месте работы я занимался разработкой и внедрением тестового покрытия."
    )


def test_llm_answer_with_only_meta_is_unanswered() -> None:
    """Если после очистки ничего не осталось, вопрос уходит в ручные."""
    question = text_question("Расскажите об автоматизации тестирования")

    assert llm_answers((question,), {1: "В профиле нет информации"}) == ()


def test_choice_prefers_short_yes_over_long_option() -> None:
    """Если есть короткое «Да», не выбираем длинную формулировку с «готов(а)»."""
    question = radio(
        "Имеется ли у вас подтвержденный стаж работы по данной должности от 3-х лет, "
        "зафиксированный в трудовой книжке?",
        [
            "Да, подтверждаю наличие необходимого стажа по ТК РФ и готов(а) "
            "документально его верифицировать",
            "Да",
            "Нет",
        ],
    )

    result = evaluate((question,), Answers(russian_labour_code_expirience=5))

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Да"


def test_self_employed_maybe_finds_option() -> None:
    """«Нет, но могу открыть» выбирается, если такой вариант есть в анкете."""
    question = radio(
        "Есть ли у вас открытое и действующее статусное ИП?",
        ["Да", "Нет", "Нет, но могу открыть"],
    )

    result = evaluate((question,), Answers(self_employed="maybe"))

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Нет, но могу открыть"


# --- Пайплайн -------------------------------------------------------------


class _Page:
    """Страница-заглушка: пайплайну нужен только адрес."""

    def __init__(self, url: str) -> None:
        self.url = url


class _FakeLLM:
    """LLM-заглушка: считает вызовы, чтобы видеть экономию токенов."""

    def __init__(self, answers: dict[int, str] | None = None) -> None:
        # Ответы модели на вопросы анкеты: номер с единицы и текст.
        self.answers = answers or {}
        self.questionnaire = ""
        self.decisions = 0
        self.generations = 0

    def decide(self, profile: Profile, vacancy: VacancyDetails) -> DecisionResult:
        self.decisions += 1
        return DecisionResult(
            fits=True,
            probability=0.95,
            threshold=0.8,
            probabilities={"подходит": 0.95, "не подходит": 0.05},
            chosen="подходит",
        )

    def generate(
        self,
        profile: Profile,
        vacancy: VacancyDetails,
        max_length: int = 1500,
        signature: str = "",
        questionnaire: str = "",
    ) -> GenerationResult:
        self.generations += 1
        self.questionnaire = questionnaire
        return GenerationResult(
            verdict="подходит",
            reason="ok",
            cover_letter="Здравствуйте! Готов обсудить.",
            answers=dict(self.answers),
        )


VACANCY_URL = "https://hh.ru/vacancy/138435882"


def _run_vacancy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    form: ResponseForm,
    *,
    answers: dict[str, Any] | None = None,
    enabled: bool = True,
    submit_auto_fill: bool = False,
    llm_answers: dict[int, str] | None = None,
) -> tuple[RunStats, _FakeLLM, RunLogger]:
    """Прогоняет одну вакансию через пайплайн с готовой формой отклика."""
    prefilter: dict[str, Any] = {"enabled": enabled, "submit_auto_fill": submit_auto_fill}
    if answers is not None:
        prefilter["answers"] = answers
    config = load_config(_write_config(tmp_path, {"pipeline": {"prefilter": prefilter}}))
    # Файл ответов пишется в data проекта: в тестах его подменяем заглушкой.
    monkeypatch.setattr(
        "agent.pipeline.questionary_store.save_screenshot", lambda *args, **kwargs: None
    )
    details = VacancyDetails(
        url="https://hh.ru/vacancy/138435882",
        title="Java-разработчик",
        company="Ozon",
        salary="",
        description="Требуется Java-разработчик.",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, card: details)
    monkeypatch.setattr(
        "agent.pipeline.open_response_form", lambda page, waiter=None, **kwargs: form
    )
    llm = _FakeLLM(llm_answers)
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    logger = RunLogger(tmp_path / "logs", run_id="prefilter")
    _process_vacancy(
        _Page(details.url),
        config,
        Profile(text="Java", examples="", style_rules=""),
        llm,
        stats,
        logger,
        VacancyCard(title=details.title, company=details.company, url=details.url),
    )
    logger.close()
    return stats, llm, logger


def test_questionnaire_reject_skips_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Анкета с отсечкой: вакансия в skipped, Generation не вызывается."""
    form = ResponseForm(status="ready", questions=(radio("Вы находитесь в РФ?", ["Да", "Нет"]),))

    stats, llm, logger = _run_vacancy(
        tmp_path, monkeypatch, form, answers={"location_country": "KZ"}
    )

    assert llm.generations == 0
    assert len(stats.skipped) == 1
    assert "Вы находитесь в РФ?" in stats.skipped[0].reason
    assert stats.queued == []
    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    prefilter = [item for item in events if item["event"] == "prefilter"]
    assert prefilter[0]["verdict"] == VERDICT_REJECT
    assert prefilter[0]["clusters"] == ["location"]


def test_unknown_question_is_answered_by_the_same_llm_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Незнакомый профилю вопрос закрывает тот же вызов, что и письмо."""
    form = ResponseForm(status="ready", questions=(radio("Какой у вас любимый цвет?", ["Красный"]),))
    monkeypatch.setattr(
        "agent.pipeline.submit_response",
        lambda page, form, letter, waiter=None, answers=(), after_fill=None: ResponseResult(
            status=STATUS_SENT
        ),
    )

    stats, llm, _ = _run_vacancy(
        tmp_path,
        monkeypatch,
        form,
        answers={"location_country": "RU"},
        submit_auto_fill=True,
        llm_answers={1: "красный"},
    )

    assert llm.generations == 1
    assert "1. Какой у вас любимый цвет? Варианты: Красный" in llm.questionnaire
    assert len(stats.sent) == 1


def test_question_without_answer_queues_vacancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Модель промолчала — в форму пустой ответ не пишем, вакансия в ручные."""
    form = ResponseForm(status="ready", questions=(radio("Какой у вас любимый цвет?", ["Красный"]),))

    stats, llm, _ = _run_vacancy(
        tmp_path, monkeypatch, form, answers={"location_country": "RU"}, submit_auto_fill=True
    )

    assert llm.generations == 1
    assert stats.sent == []
    assert stats.queued[0]["reason"] == QUESTIONARY_MANUAL_REASON
    assert "Какой у вас любимый цвет?" in stats.queued[0]["message"]


def test_partial_answers_are_saved_in_questionary_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ответы, которые LLM успела дать, сохраняются даже при неполной анкете."""
    form = ResponseForm(
        status="ready",
        questions=(
            radio("Какой у вас любимый цвет?", ["Красный"], name="q1"),
            radio("Какое у вас хобби?", ["Спорт", "Книги"], name="q2"),
        ),
    )

    stats, _, _ = _run_vacancy(
        tmp_path,
        monkeypatch,
        form,
        answers={},
        submit_auto_fill=True,
        llm_answers={1: "красный"},
    )

    assert stats.questionary[VACANCY_URL] == [
        {"question": "Какой у вас любимый цвет?", "answer": "Красный"}
    ]
    assert stats.queued[0]["reason"] == QUESTIONARY_MANUAL_REASON
    assert "Какое у вас хобби?" in stats.queued[0]["message"]


def test_manual_verdict_logs_short_message_and_keeps_reason_in_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Журнал печатает короткую строку, а причина ручного разбора остаётся в отчёте."""
    form = ResponseForm(status="ready", questions=(radio("Вы находитесь в РФ?", ["Да", "Нет"]),))

    stats, _, logger = _run_vacancy(tmp_path, monkeypatch, form, answers={})

    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    messages = [item["message"] for item in events if item["event"] == "info"]
    assert any(
        "не удалось заполнить анкету, подробная информация в дневном отчёте" in item
        for item in messages
    )
    assert not any("Что сделать:" in item for item in messages)
    prefilter = [item for item in events if item["event"] == "prefilter"][0]
    assert "Вы находитесь в РФ?" in prefilter["reason"]
    assert "ответьте на анкету вручную" in prefilter["reason"]
    assert stats.queued[0]["reason"] == QUESTIONARY_MANUAL_REASON
    assert "Вы находитесь в РФ?" in stats.queued[0]["message"]
    assert "требует ручного ответа" in stats.queued[0]["message"]


def test_unknown_question_reason_is_subjective_and_needs_manual_answer() -> None:
    """Незнакомый профилю вопрос в отчёте описан как субъективный, без технических кодов."""
    result = evaluate((radio("Какой у вас любимый цвет?", ["Красный"]),), Answers())

    assert result.verdict == VERDICT_MANUAL
    assert "субъективен" in result.reason
    assert "требует ручного ответа" in result.reason


def test_manual_hint_points_to_daily_report() -> None:
    """Короткая строка консоли отсылает человека к подробностям в дневном отчёте."""
    result = evaluate(
        (radio("Вы находитесь в РФ?", ["Может быть"]),), Answers(location_country="RU")
    )

    assert result.verdict == VERDICT_MANUAL
    assert "дневном отчёте" in manual_hint(result)


def test_experience_threshold_reads_hyphenated_years() -> None:
    """«от 3-х лет» — тот же порог, что «от 3 лет»: иначе вопрос уходит в ручные."""
    result = evaluate(
        (radio("Имеется ли у вас подтвержденный стаж работы от 3-х лет?", ["Да", "Нет"]),),
        Answers(work_experience=4),
    )

    assert result.verdict == VERDICT_AUTO_FILL
    assert result.answers[0].value == "Да"


def test_auto_fill_waits_for_human_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    form = ResponseForm(status="ready", questions=(radio("Вы находитесь в РФ?", ["Да", "Нет"]),))

    stats, llm, _ = _run_vacancy(tmp_path, monkeypatch, form, answers={"location_country": "RU"})

    assert llm.generations == 0
    assert stats.queued[0]["reason"] == QUEUE_REASON_TEXTS["prefilter_auto_fill_disabled"]


def test_auto_fill_sends_response_when_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    form = ResponseForm(status="ready", questions=(radio("Вы находитесь в РФ?", ["Да", "Нет"]),))
    monkeypatch.setattr(
        "agent.pipeline.submit_response",
        lambda page, form, letter, waiter=None, answers=(), after_fill=None: ResponseResult(
            status=STATUS_SENT
        ),
    )

    stats, llm, _ = _run_vacancy(
        tmp_path, monkeypatch, form, answers={"location_country": "RU"}, submit_auto_fill=True
    )

    assert llm.generations == 1
    assert len(stats.sent) == 1


def test_questionary_snapshot_is_taken_when_form_is_filled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пайплайн снимает заполненную форму до отправки: сбой съёмки виден в журнале."""
    form = ResponseForm(status="ready", questions=(radio("Вы находитесь в РФ?", ["Да", "Нет"]),))

    def fake_submit(
        page: Any,
        form: ResponseForm,
        letter: str,
        waiter: Any = None,
        answers: Any = (),
        after_fill: Any = None,
    ) -> ResponseResult:
        if after_fill is not None:
            after_fill()
        return ResponseResult(status=STATUS_SENT)

    monkeypatch.setattr("agent.pipeline.submit_response", fake_submit)

    _, _, logger = _run_vacancy(
        tmp_path, monkeypatch, form, answers={"location_country": "RU"}, submit_auto_fill=True
    )

    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    snapshot_events = [
        item["event"]
        for item in events
        if item["event"].startswith("questionary_screenshot")
    ]
    assert snapshot_events == ["questionary_screenshot_error"]


def test_form_without_questions_follows_usual_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обычная форма без анкеты: отклик идёт прежним путём канала."""
    monkeypatch.setattr(
        "agent.pipeline.respond",
        lambda page, letter, captcha_waiter=None: ResponseResult(status=STATUS_SENT),
    )

    stats, llm, _ = _run_vacancy(tmp_path, monkeypatch, ResponseForm(status="ready"))

    assert llm.generations == 1
    assert len(stats.sent) == 1


def test_test_screen_is_passed_and_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тест проходится тем же вызовом LLM, после него уходит отклик."""
    questions = (radio("Какой уровень Kafka?", ["Высокий"]),)
    monkeypatch.setattr(
        "agent.pipeline.start_test",
        lambda page, waiter=None: ResponseForm(status="ready", questions=questions),
    )
    monkeypatch.setattr(
        "agent.pipeline.submit_test",
        lambda page, answers, waiter=None, after_fill=None: ResponseResult(status=TEST_PASSED),
    )
    monkeypatch.setattr(
        "agent.pipeline.respond",
        lambda page, letter, captcha_waiter=None: ResponseResult(status=STATUS_SENT),
    )

    stats, llm, _ = _run_vacancy(
        tmp_path,
        monkeypatch,
        ResponseForm(status="test"),
        answers={"location_country": "RU"},
        submit_auto_fill=True,
        llm_answers={1: "Высокий"},
    )

    assert llm.generations == 1
    assert "1. Какой уровень Kafka? Варианты: Высокий" in llm.questionnaire
    assert len(stats.sent) == 1
    assert stats.questionary[VACANCY_URL] == [
        {"question": "Какой уровень Kafka?", "answer": "Высокий"}
    ]

def test_failed_test_queues_vacancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тест не пройден — вакансия в ручные, отклик не отправляем."""
    questions = (radio("Какой уровень Kafka?", ["Высокий"]),)
    monkeypatch.setattr(
        "agent.pipeline.start_test",
        lambda page, waiter=None: ResponseForm(status="ready", questions=questions),
    )
    monkeypatch.setattr(
        "agent.pipeline.submit_test",
        lambda page, answers, waiter=None, after_fill=None: ResponseResult(status=TEST_FAILED),
    )

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("после непройденного теста отклик не отправляется")

    monkeypatch.setattr("agent.pipeline.respond", boom)

    stats, _, _ = _run_vacancy(
        tmp_path,
        monkeypatch,
        ResponseForm(status="test"),
        answers={"location_country": "RU"},
        submit_auto_fill=True,
        llm_answers={1: "Высокий"},
    )

    assert stats.sent == []
    assert stats.queued[0]["reason"] == "Тест по вакансии не пройден — откликнитесь вручную"


def test_disabled_prefilter_opens_form_after_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Префильтр выключен — форма не открывается до Generation."""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("форма не должна открываться до Generation")

    monkeypatch.setattr("agent.pipeline.open_response_form", boom)
    monkeypatch.setattr(
        "agent.pipeline.respond",
        lambda page, letter, captcha_waiter=None: ResponseResult(status=STATUS_SENT),
    )

    stats, llm, _ = _run_vacancy(tmp_path, monkeypatch, ResponseForm(status="ready"), enabled=False)

    assert llm.generations == 1
    assert len(stats.sent) == 1
