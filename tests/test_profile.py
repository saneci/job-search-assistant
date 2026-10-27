"""Тесты обезличивания профиля: персональные данные не попадают в промпт."""

from __future__ import annotations

import pytest

from agent.profile import (
    BIRTHDAY_REPLACEMENT,
    CONTACT_REPLACEMENT,
    DOCUMENT_REPLACEMENT,
    EMAIL_REPLACEMENT,
    PHONE_REPLACEMENT,
    Profile,
    normalize_text,
    scrub_personal_data,
)

RESUME_TEXT = """Java-разработчик
Опыт работы
ООО «Ромашка», ведущий разработчик, 2021-2026
Почта: ivan.petrov@example.com
Телефон: +7 (999) 123-45-67
Телеграм: @ivan_petrov, https://t.me/ivan_petrov
Дата рождения: 1 января 1990
СНИЛС: 123-456-789 00
Навыки: Java, Spring Boot, Kafka, PostgreSQL
"""


@pytest.mark.parametrize(
    ("text", "marker", "secret"),
    [
        ("Пишите на ivan.petrov@example.com", EMAIL_REPLACEMENT, "ivan.petrov@example.com"),
        ("Телефон +7 (999) 123-45-67", PHONE_REPLACEMENT, "123-45-67"),
        ("Телефон 8 999 123 45 67", PHONE_REPLACEMENT, "123 45 67"),
        ("Телеграм @ivan_petrov", CONTACT_REPLACEMENT, "@ivan_petrov"),
        ("Профиль https://t.me/ivan_petrov", CONTACT_REPLACEMENT, "ivan_petrov"),
        ("Дата рождения: 1 января 1990", BIRTHDAY_REPLACEMENT, "1990"),
        ("СНИЛС 123-456-789 00", DOCUMENT_REPLACEMENT, "123-456-789"),
        ("ИНН: 771234567890", DOCUMENT_REPLACEMENT, "771234567890"),
    ],
)
def test_personal_data_is_replaced(text: str, marker: str, secret: str) -> None:
    """Контакты и номера заменяются метками, а не остаются в тексте."""
    scrubbed = scrub_personal_data(text)

    assert marker in scrubbed
    assert secret not in scrubbed


def test_professional_facts_survive_scrubbing() -> None:
    """Профессиональная часть резюме не страдает от обезличивания."""
    scrubbed = scrub_personal_data(RESUME_TEXT)

    assert "Java" in scrubbed
    assert "Spring Boot" in scrubbed
    assert "Kafka" in scrubbed
    assert "ООО «Ромашка»" in scrubbed
    assert "ivan.petrov@example.com" not in scrubbed
    assert "+7 (999) 123-45-67" not in scrubbed
    assert "ivan_petrov" not in scrubbed


def test_nbsp_and_extra_spaces_are_normalized() -> None:
    """hh.ru отдаёт NBSP: без нормализации ломаются заголовки и дедупликация."""
    text = "Опыт\u00a0работы\n\n  Java\u00a021  \n\u200b\n"

    assert normalize_text(text) == "Опыт работы\nJava 21"


def test_technical_annotation_is_not_a_contact() -> None:
    """Аннотации в стеке (@Override, @Autowired) — факт о кандидате, а не контакт."""
    text = "Spring: @Override, @Autowired, @Transactional, @GetMapping"

    assert scrub_personal_data(text) == text


def test_profile_keeps_only_text() -> None:
    """Стиль живёт в SYSTEM_PROMPT.md: в профиле остаются только факты."""
    profile = Profile(text="Java")

    assert profile.text == "Java"
    assert not hasattr(profile, "examples")
    assert not hasattr(profile, "style_rules")
