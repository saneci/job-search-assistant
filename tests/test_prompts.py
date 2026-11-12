"""Тесты промптов: обязательное начало письма из текста вакансии."""

from __future__ import annotations

import pytest

from agent.models import VacancyDetails
from agent.profile import Profile
from agent.prompts import (
    build_rejection_prompt,
    build_user_prompt,
    detect_required_opening,
    opening_from_text,
    profile_facts,
    requires_project_links,
    split_requirements,
    years_text,
)

PROFILE = Profile(text="Java-разработчик", examples="- пример", style_rules="Коротко")

INSTRUCTION = (
    "Как откликнуться\n"
    "Начните сопроводительное письмо со слова 3PL. "
    "Дальше в 2-3 предложениях расскажите почему вам откликается именно эта роль."
)


def make_vacancy(description: str) -> VacancyDetails:
    return VacancyDetails(
        url="https://hh.ru/vacancy/1",
        title="Java-разработчик",
        company="Ozon",
        salary="200 000 ₽",
        description=description,
    )


def test_required_opening_from_company_instruction() -> None:
    """Инцидент 2026-10-21: вакансия просила начать письмо со слова 3PL."""
    assert detect_required_opening(make_vacancy(INSTRUCTION)) == "3PL"


def test_required_opening_variants() -> None:
    assert (
        opening_from_text('Письмо должно начинаться со фразы "Хочу работать в вашей команде".')
        == "Хочу работать в вашей команде"
    )
    assert opening_from_text("Начните письмо с «Привет, команда».") == "Привет, команда"
    assert opening_from_text("Начните отклик с 3PL") == "3PL"
    assert opening_from_text("Начните со слова 3PL (обязательно), далее по тексту") == "3PL"


def test_no_required_opening_for_usual_vacancy() -> None:
    assert opening_from_text("") == ""
    assert opening_from_text("Пишите по существу. Spring Boot, Kafka.") == ""
    assert opening_from_text("Работа начинается с 9 утра.") == ""


def test_description_is_not_mistaken_for_required_opening() -> None:
    """«Начните с рассказа о себе» это не обязательное слово, а просьба."""
    assert opening_from_text("Начните с рассказа о себе.") == ""
    assert opening_from_text("Начните с приветствия.") == ""


def test_user_prompt_has_explicit_requirement() -> None:
    prompt = build_user_prompt(PROFILE, make_vacancy(INSTRUCTION), "3PL")

    assert "ТРЕБОВАНИЕ ВАКАНСИИ К НАЧАЛУ ПИСЬМА" in prompt
    assert "«3PL»" in prompt


def test_user_prompt_without_requirement() -> None:
    prompt = build_user_prompt(PROFILE, make_vacancy("Spring Boot, Kafka."))

    assert "ТРЕБОВАНИЕ ВАКАНСИИ" not in prompt


def test_project_links_requirement_detected() -> None:
    assert requires_project_links(
        make_vacancy(
            "P. S. Пожалуйста, приложите к отклику в сопроводительном письме "
            "ссылку на ваши проекты (например, на GitHub)."
        )
    )
    assert requires_project_links(make_vacancy("Укажите ссылки на проекты."))
    assert requires_project_links(make_vacancy("Ссылка на портфолио обязательна."))
    assert not requires_project_links(make_vacancy("Spring Boot, Kafka, GitHub в качестве хранилища."))
    assert not requires_project_links(make_vacancy("Проекты на GitHub."))


def test_user_prompt_includes_pet_projects() -> None:
    vacancy = make_vacancy("Приложите ссылку на ваши проекты в письме.")

    prompt = build_user_prompt(
        PROFILE,
        vacancy,
        pet_projects=("https://github.com/user/project",),
        needs_project_links=True,
    )

    assert "ТРЕБОВАНИЕ ВАКАНСИИ К ПРОЕКТАМ" in prompt
    assert "https://github.com/user/project" in prompt


def test_user_prompt_without_requirement_has_no_projects_hint() -> None:
    prompt = build_user_prompt(
        PROFILE,
        make_vacancy("Spring Boot, Kafka."),
        pet_projects=("https://github.com/user/project",),
        needs_project_links=False,
    )

    assert "ТРЕБОВАНИЕ ВАКАНСИИ К ПРОЕКТАМ" not in prompt



def test_requirements_block_is_extracted_by_heading() -> None:
    """Блок «Требования» берётся из тела вакансии, а не из шапки сайта."""
    description = (
        "Ищем разработчика.\n\n"
        "Обязанности\nРазработка сервисов\n\n"
        "Требования\nопыт коммерческой разработки с Java от 3 лет\n"
        "знание JavaScript/TypeScript\n\n"
        "Будет плюсом\nреальный опыт с React\n\n"
        "Условия\nгибридный формат"
    )

    result = split_requirements(description)

    assert result.mandatory == (
        "опыт коммерческой разработки с Java от 3 лет\nзнание JavaScript/TypeScript"
    )
    assert result.optional == "реальный опыт с React"


def test_requirements_synonyms_and_inline_text() -> None:
    """Синонимы заголовка и строка «Требования: ...» тоже дают блок требований."""
    assert split_requirements(
        "Что мы ждём от кандидата\nJava, Spring\n\nУсловия\nофис"
    ).mandatory == "Java, Spring"
    assert split_requirements("Обязательные требования:\nJava, Kafka").mandatory == (
        "Java, Kafka"
    )
    assert split_requirements("Кого ищем\nJava-разработчика").mandatory == "Java-разработчика"
    assert split_requirements("Требования: опыт от 5 лет, Java, UML").mandatory == (
        "опыт от 5 лет, Java, UML"
    )


def test_requirements_block_absent() -> None:
    """Без заголовка блок пустой: Decision получает полное описание."""
    result = split_requirements("Spring Boot, Kafka, PostgreSQL.")

    assert result.mandatory == ""
    assert result.optional == ""


def test_profile_facts_use_questionnaire_numbers() -> None:
    """Факты профиля — числа из анкеты и правило «нет в профиле — нет опыта»."""
    profile = Profile(
        text="Java-разработчик",
        examples="",
        style_rules="",
        experience_total_years=11,
        experience_role_years=2.5,
        english_level="b1",
    )

    facts = profile_facts(profile)

    assert "всего опыта в IT: 11 лет" in facts
    assert "опыт в роли разработчика: 2.5 года" in facts
    assert "английский: b1" in facts
    assert "не доказывает отсутствие опыта" in facts
    assert "более высокий подтверждённый уровень" in facts
    assert "Java Core" in facts
    assert "highload" in facts
    assert "не подтверждены" in facts


@pytest.mark.parametrize(
    ("value", "expected"),
    ((2.5, "2.5 года"), (11, "11 лет"), (1, "1 год"), (21, "21 год"), (0.5, "0.5 года")),
)
def test_years_text(value: float, expected: str) -> None:
    assert years_text(value) == expected


@pytest.mark.parametrize(
    "heading",
    ["Какие знания и навыки нам важны", "Навыки и знания", "Что важно знать"],
)
def test_requirements_heading_synonyms(heading: str) -> None:
    """Новые заголовки блока требований дают тот же результат, что и «Требования»."""
    result = split_requirements("\n".join([heading, "Java, Spring"]))

    assert result.mandatory == "Java, Spring"


def test_duties_heading_stops_requirements_block() -> None:
    """«Что нужно делать» относится к обязанностям и не попадает в требования."""
    result = split_requirements(
        "Требования\nJava, Spring\n\nЧто нужно делать\nРазработка сервисов"
    )

    assert result.mandatory == "Java, Spring"


def test_rejection_prompt_has_full_description_and_requirements() -> None:
    """Объяснение отказа получает полное описание и отдельный блок требований."""
    description = (
        "Требования\nJava, Spring Boot\n\n"
        + "Очень длинное описание. " * 200
        + "\nФинальное требование: опыт с Kafka."
    )
    prompt = build_rejection_prompt(
        PROFILE, make_vacancy(description), probability=0.21, threshold=0.8
    )

    assert "ТРЕБОВАНИЯ ВАКАНСИИ" in prompt
    assert "ОПИСАНИЕ ВАКАНСИИ" in prompt
    assert "Финальное требование: опыт с Kafka." in prompt
    assert "Оценка соответствия: 21% (порог 80%)." in prompt
