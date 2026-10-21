"""Тесты разбора PROFILE.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.profile import ProfileError, load_profile, split_style_section

SAMPLE_PROFILE = """# PROFILE: Тестовый пользователь

## Краткое описание

Java-разработчик с опытом микросервисов.

## Стиль написания сопроводительных писем

* **Тон:** Уверенный, без лести.
* **Правила:**
    * Не использовать шаблонные фразы.
    * Подкреплять утверждения примерами.
* **Примеры (для few-shot):**
    * «Здравствуйте! Мой опыт в Kafka подходит вакансии.»
    * «Добрый день. Разрабатывал кредитный конвейер.»

## Контакты

* Email: test@example.com
"""


def test_split_style_and_examples() -> None:
    style_rules, examples = split_style_section(SAMPLE_PROFILE)

    assert "Тон" in style_rules
    assert "шаблонные фразы" in style_rules
    assert "Примеры" not in style_rules
    assert "Kafka" in examples
    assert "кредитный конвейер" in examples
    assert "Контакты" not in examples


def test_missing_style_section() -> None:
    assert split_style_section("# PROFILE\n\n## Опыт\n\nJava") == ("", "")


def test_examples_heading_is_not_confused_with_word_naprimer() -> None:
    text = """## Стиль написания сопроводительных писем

* **Правила:**
    * Подкреплять примерами (например, «внедрил Prometheus»).
* **Примеры (для few-shot):**
    * «Первый пример письма,
      продолжение первого примера.»
    * «Второй пример письма.»

## Контакты
"""

    style_rules, examples = split_style_section(text)

    assert "например" in style_rules
    assert "Примеры (для few-shot)" not in style_rules
    assert examples == (
        "- «Первый пример письма, продолжение первого примера.»\n"
        "- «Второй пример письма.»"
    )


def test_load_profile(tmp_path: Path) -> None:
    path = tmp_path / "PROFILE.md"
    path.write_text(SAMPLE_PROFILE, encoding="utf-8")

    profile = load_profile(path)

    assert profile.text == SAMPLE_PROFILE
    assert "Уверенный" in profile.style_rules
    assert "Здравствуйте" in profile.examples


def test_load_profile_without_style_raises(tmp_path: Path) -> None:
    path = tmp_path / "PROFILE.md"
    path.write_text("# PROFILE\n\n## Опыт\n\nJava", encoding="utf-8")

    with pytest.raises(ProfileError):
        load_profile(path)


def test_load_profile_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load_profile(tmp_path / "PROFILE.md")
