"""Тесты промптов: обязательное начало письма из текста вакансии."""

from __future__ import annotations

from agent.models import VacancyDetails
from agent.profile import Profile
from agent.prompts import (
    build_user_prompt,
    detect_required_opening,
    opening_from_text,
    requires_project_links,
)

PROFILE = Profile(text="Java-разработчик")

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

