"""Тесты ответов на анкеты и тесты: LLM-контракт, слияние с профилем, хранение."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agent.llm import parse_answers
from agent.prefilter import (
    UNKNOWN_CLUSTER,
    Question,
    QuestionOption,
    evaluate,
    llm_answers,
    merge_answers,
    question_lines,
    without_rejects,
)
from agent.questionary import file_name, save


def _radio(text: str, *labels: str) -> Question:
    options = tuple(QuestionOption(label) for label in labels)
    return Question(name=text[:8], text=text, kind="radio", options=options)


def _text(text: str) -> Question:
    return Question(name=text[:8], text=text, kind="text")


def _profile(**values: object) -> SimpleNamespace:
    """Профиль для префильтра: незаполненные поля дают пустую строку."""
    return SimpleNamespace(**values)


def test_parse_answers_reads_list_and_object() -> None:
    assert parse_answers([{"id": 1, "answer": "Да"}]) == {1: "Да"}
    assert parse_answers({"2": "Нет"}) == {2: "Нет"}
    # Номер по порядку, если модель его не указала.
    assert parse_answers([{"answer": "Да"}]) == {1: "Да"}


def test_parse_answers_rejects_multiple_choice_and_empty() -> None:
    """Множественный выбор и пустой ответ в форму не проставляем."""
    raw = [{"id": 1, "answer": ["Да", "Нет"]}, {"id": 2, "answer": ""}, "мусор"]

    assert parse_answers(raw) == {}


def test_question_lines_number_questions_with_options() -> None:
    questions = (_radio("Вы в РФ?", "Да", "Нет"), _text("Ваши достижения"))

    assert question_lines(questions) == (
        "1. Вы в РФ? Варианты: Да | Нет\n2. Ваши достижения (нужен текст)"
    )


def test_llm_answers_match_option_and_drop_unknown() -> None:
    """Ответ, которого нет среди вариантов, отбрасывается: его не проставить."""
    questions = (_radio("Любимый цвет?", "Красный", "Синий"),)

    matched = llm_answers(questions, {1: "красный"})
    dropped = llm_answers(questions, {1: "Зелёный"})
    out_of_range = llm_answers(questions, {9: "Красный"})

    assert matched[0].value == "Красный"
    assert dropped == ()
    assert out_of_range == ()


def test_llm_answers_take_text_for_text_question() -> None:
    questions = (_text("Ваши достижения"),)

    answers = llm_answers(questions, {1: "  Перевёл\n монолит  "})

    assert answers[0].value == "Перевёл монолит"


def test_merge_answers_keeps_profile_and_adds_model() -> None:
    """Профиль важнее модели: ответ модели не переопределяет профильный."""
    in_russia = _radio("Вы находитесь в РФ?", "Да", "Нет")
    color = _radio("Любимый цвет?", "Красный")
    result = evaluate((in_russia,), _profile(location_country="RU"))

    answers = merge_answers(result, llm_answers((color,), {1: "Красный"}))
    overridden = merge_answers(result, llm_answers((in_russia,), {1: "Нет"}))

    assert [(answer.question.text, answer.value) for answer in answers] == [
        ("Вы находитесь в РФ?", "Да"),
        ("Любимый цвет?", "Красный"),
    ]
    assert [answer.value for answer in overridden] == ["Да"]


def test_without_rejects_removes_knockout_for_test() -> None:
    """В тесте отрицательный ответ не отсекает вакансию."""
    question = _radio("Вы находитесь в РФ?", "Да", "Нет")
    result = evaluate((question,), _profile(location_country="KZ"))

    assert result.rejects
    converted = without_rejects(result)
    assert not converted.rejects
    assert converted.answers_to_fill()[0].value == "Нет"


def test_unknown_question_stays_unanswered() -> None:
    result = evaluate((_radio("Любимый цвет?", "Красный"),), _profile(location_country="RU"))

    assert result.answers[0].cluster == UNKNOWN_CLUSTER


def test_questionary_saved_per_vacancy_and_profile(tmp_path: Path) -> None:
    """Файл ответов: номер вакансии и резюме, содержимое — вопрос и ответ."""
    path = save(
        tmp_path,
        url="https://hh.ru/vacancy/138435882",
        profile="Java Developer",
        channel="hh",
        status="sent",
        questionary=[("Вы в РФ?", "Да")],
        title="Java-разработчик",
        company="Ozon",
        saved_at=datetime(2026, 10, 31, 12, 0),
    )

    assert path is not None
    assert path.name == "138435882-java-developer.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["questionary"] == [{"question": "Вы в РФ?", "answer": "Да"}]
    assert saved["url"] == "https://hh.ru/vacancy/138435882"
    assert saved["profile"] == "Java Developer"


def test_questionary_without_answers_saves_nothing(tmp_path: Path) -> None:
    assert save(
        tmp_path, url="https://hh.ru/vacancy/1", profile="Java", channel="hh", status="sent",
        questionary=[],
    ) is None
    assert list(tmp_path.iterdir()) == []


def test_questionary_file_name_is_safe_for_any_url() -> None:
    """Имя файла — только латиница, цифры и дефис: кириллица в имени не выживает."""
    assert file_name("https://hh.ru/vacancy/42", "Резюме Java") == "42-java.json"
