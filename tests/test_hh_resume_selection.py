"""Тесты выбора резюме в форме отклика hh.ru (без браузера)."""

from __future__ import annotations

from typing import Any

import pytest

import agent.hh_vacancy as hh_vacancy
from agent.hh_vacancy import (
    ResumeNotFoundError,
    resume_options,
    respond,
    select_resume,
    set_resume_name,
)


class FakeOption:
    """Опция списка резюме: видимость, текст и клик."""

    def __init__(
        self,
        text: str,
        *,
        visible: bool = True,
        on_click: Any = None,
    ) -> None:
        self._text = text
        self._visible = visible
        self._on_click = on_click

    @property
    def first(self) -> "FakeOption":
        return self

    def count(self) -> int:
        return 1

    def nth(self, index: int) -> "FakeOption":
        return self

    def is_visible(self) -> bool:
        return self._visible

    def inner_text(self) -> str:
        return self._text

    def click(self) -> None:
        if self._on_click is not None:
            self._on_click()


class FakeList:
    """Список элементов: поддерживает count/nth/first, как Locator."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    @property
    def first(self) -> Any:
        return self._items[0] if self._items else FakeOption("", visible=False)

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> Any:
        return self._items[index]


class FakeSelect:
    """Нативный select с опциями: запоминает выбранную подпись."""

    def __init__(self, options: list[str]) -> None:
        self.selected = ""
        self._options = [FakeOption(text) for text in options]

    @property
    def first(self) -> "FakeSelect":
        return self

    def count(self) -> int:
        return 1

    def is_visible(self) -> bool:
        return True

    def locator(self, selector: str) -> FakeList:
        return FakeList(self._options if selector == "option" else [])

    def select_option(self, *, label: str) -> None:
        self.selected = label


class FakeScope:
    """Локатор/страница с элементами по CSS-селектору."""

    def __init__(self, mapping: dict[str, list[Any]]) -> None:
        self._mapping = mapping

    def locator(self, selector: str) -> FakeList:
        return FakeList(self._mapping.get(selector, []))


class CustomListScope:
    """Кастомный список: опции видны только после клика по триггеру."""

    def __init__(
        self,
        options: list[str],
        *,
        trigger: str,
        native_select: FakeSelect | None = None,
    ) -> None:
        self.opened = False
        self.clicked: list[str] = []
        self._trigger_selector = trigger
        self._native_select = native_select
        self._options = [
            FakeOption(text, on_click=lambda text=text: self.clicked.append(text))
            for text in options
        ]
        self._trigger = FakeOption("Java Developer", on_click=self._open)

    def _open(self) -> None:
        self.opened = True

    def locator(self, selector: str) -> FakeList:
        if selector == self._trigger_selector:
            return FakeList([self._trigger])
        if selector == "[role='option']":
            return FakeList(self._options if self.opened else [])
        if selector == "select" and self._native_select is not None:
            return FakeList([self._native_select])
        return FakeList([])


def test_native_select_options_are_read_without_opening() -> None:
    """Опции нативного select не считаются видимыми, но выбрать их можно."""
    select = FakeSelect(["Java Developer", "Team Lead"])
    scope = FakeScope({"select": [select]})

    assert resume_options(scope) == ["Java Developer", "Team Lead"]  # type: ignore[arg-type]

    select_resume(scope, " team lead ")  # type: ignore[arg-type]

    assert select.selected == "Team Lead"


def test_native_select_prefers_list_with_target_name() -> None:
    """Если в форме несколько select, выбираем тот, где есть нужное резюме."""
    city = FakeSelect(["Москва", "Санкт-Петербург"])
    resume = FakeSelect(["Java Developer", "Team Lead"])
    scope = FakeScope({"select": [city, resume]})

    select_resume(scope, "Team Lead")  # type: ignore[arg-type]

    assert resume.selected == "Team Lead"
    assert city.selected == ""


def test_native_select_without_name_raises() -> None:
    """Нет нужного резюме в нативном списке — отклик не отправляем."""
    scope = FakeScope({"select": [FakeSelect(["Java Developer"])]})

    with pytest.raises(ResumeNotFoundError) as error:
        select_resume(scope, "Team Lead")  # type: ignore[arg-type]

    assert error.value.options == ("Java Developer",)


def test_custom_list_opens_trigger_before_reading_options() -> None:
    """Опции кастомного списка появляются после клика по триггеру."""
    scope = CustomListScope(
        ["Java Developer", "Team Lead"],
        trigger="[data-qa*='select-resume']",
    )

    select_resume(scope, "Team Lead")  # type: ignore[arg-type]

    assert scope.opened is True
    assert scope.clicked == ["Team Lead"]


def test_custom_list_is_used_when_other_select_has_no_target() -> None:
    """Другой select формы (например, город) не мешает кастомному списку резюме."""
    city = FakeSelect(["Москва", "Санкт-Петербург"])
    scope = CustomListScope(
        ["Java Developer", "Team Lead"],
        trigger="[data-qa*='select-resume']",
        native_select=city,
    )

    select_resume(scope, "Team Lead")  # type: ignore[arg-type]

    assert scope.clicked == ["Team Lead"]
    assert city.selected == ""


class FakeButton:
    """Кнопка формы: клик не требует браузера."""

    def click(self) -> None:
        return


def test_respond_selects_resume_before_letter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Резюме выбирается до заполнения письма: отклик уходит от нужного резюме."""
    order: list[tuple[str, str]] = []

    def fake_select(scope: Any, name: str) -> None:
        order.append(("resume", name))

    def fake_letter(page: Any, scope: Any, letter: str) -> bool:
        del page, scope, letter
        order.append(("letter", ""))
        return False

    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "detect_response_blocker", lambda scope: None)
    monkeypatch.setattr(hh_vacancy, "_response_scope", lambda page: page)
    monkeypatch.setattr(hh_vacancy, "_wait_for_response_ui", lambda page, waiter: "ready")
    monkeypatch.setattr(hh_vacancy, "_find_action", lambda scope, selectors, pattern: FakeButton())
    monkeypatch.setattr(hh_vacancy, "_is_enabled", lambda button: True)
    monkeypatch.setattr(hh_vacancy, "_wait_for_response_result", lambda page, waiter: "sent")
    monkeypatch.setattr(hh_vacancy, "select_resume", fake_select)
    monkeypatch.setattr(hh_vacancy, "_fill_letter", fake_letter)
    set_resume_name("Java Developer")
    try:
        result = respond(object(), "Здравствуйте!")  # type: ignore[arg-type]
    finally:
        set_resume_name("")

    assert order == [("resume", "Java Developer"), ("letter", "")]
    assert result.status == "sent"


def test_custom_list_without_name_raises() -> None:
    """Кастомный список открылся, но нужного резюме в нём нет."""
    scope = CustomListScope(
        ["Java Developer"],
        trigger="[data-qa*='select-resume']",
    )

    with pytest.raises(ResumeNotFoundError) as error:
        select_resume(scope, "Team Lead")  # type: ignore[arg-type]

    assert error.value.resume == "Team Lead"
    assert error.value.options == ("Java Developer",)
