"""Тесты определения тестов/анкет в форме отклика hh.ru (без браузера)."""

from __future__ import annotations

from typing import Any

from hh_agent.hh_vacancy import detect_response_blocker


class FakeElement:
    """Минимальная заглушка элемента Playwright."""

    def __init__(self, text: str, *, visible: bool = True, in_description: bool = False) -> None:
        self._text = text
        self._visible = visible
        self._in_description = in_description

    def count(self) -> int:
        return 1

    def nth(self, index: int) -> "FakeElement":
        return self

    def is_visible(self) -> bool:
        return self._visible

    def inner_text(self) -> str:
        return self._text

    def evaluate(self, script: str) -> Any:
        return self._in_description

    def get_attribute(self, name: str) -> str:
        return ""


class FakeScope:
    """Заглушка Locator/Page, отдающая элементы по CSS-селектору."""

    def __init__(self, mapping: dict[str, list[FakeElement]]) -> None:
        self._mapping = mapping

    def locator(self, selector: str) -> "FakeList":
        return FakeList(self._mapping.get(selector, []))


class FakeList:
    def __init__(self, elements: list[FakeElement]) -> None:
        self._elements = elements

    def count(self) -> int:
        return len(self._elements)

    def nth(self, index: int) -> FakeElement:
        return self._elements[index]


def test_detects_test_screen_by_heading() -> None:
    scope = FakeScope({"h1": [FakeElement("Тест по вакансии")]})

    assert detect_response_blocker(scope) == "test"  # type: ignore[arg-type]


def test_detects_test_screen_by_button() -> None:
    scope = FakeScope({"button": [FakeElement("Пройти тест")]})

    assert detect_response_blocker(scope) == "test"  # type: ignore[arg-type]


def test_detects_questionnaire() -> None:
    scope = FakeScope({"h2": [FakeElement("Заполнить анкету")]})

    assert detect_response_blocker(scope) == "questionnaire"  # type: ignore[arg-type]


def test_detects_external_form() -> None:
    scope = FakeScope({"h2": [FakeElement("Отклик на сайте работодателя")]})

    assert detect_response_blocker(scope) == "external_form"  # type: ignore[arg-type]


def test_ignores_headings_inside_vacancy_description() -> None:
    """Слово «тестирование» в описании вакансии — не повод откладывать её."""
    scope = FakeScope(
        {
            "h1": [FakeElement("Java-разработчик")],
            "h3": [FakeElement("Тестирование", in_description=True)],
        }
    )

    assert detect_response_blocker(scope) is None  # type: ignore[arg-type]


def test_normal_form_has_no_blockers() -> None:
    scope = FakeScope(
        {
            "h2": [FakeElement("Отклик на вакансию")],
            "button": [FakeElement("Откликнуться")],
        }
    )

    assert detect_response_blocker(scope) is None  # type: ignore[arg-type]
