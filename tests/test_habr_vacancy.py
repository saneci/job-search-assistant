"""Тесты состояний страницы вакансии career.habr.com без браузера."""

from __future__ import annotations

from typing import Any

from agent.habr_vacancy import detect_page_blocker, respond
from agent.hh_vacancy import (
    STATUS_ALREADY_RESPONDED,
    STATUS_CAPTCHA,
    STATUS_NO_FORM,
)


class FakeList:
    def __init__(self, items: list["FakeElement"]) -> None:
        self._items = items

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> "FakeElement":
        return self._items[index]

    @property
    def first(self) -> "FakeElement":
        return self._items[0] if self._items else FakeElement(present=False)


class FakeElement:
    def __init__(
        self,
        text: str = "",
        visible: bool = True,
        mapping: dict[str, list["FakeElement"]] | None = None,
        present: bool = True,
    ) -> None:
        self._text = text
        self._visible = visible
        self._present = present
        self._mapping = mapping or {}

    @property
    def first(self) -> "FakeElement":
        return self

    def count(self) -> int:
        return 1 if self._present else 0

    def is_visible(self) -> bool:
        return self._visible

    def inner_text(self) -> str:
        return self._text

    def locator(self, selector: str) -> FakeList:
        return FakeList(self._mapping.get(selector, []))


class FakePage:
    """Страница с заданной разметкой; капча определяется по адресу."""

    def __init__(
        self,
        url: str = "https://career.habr.com/vacancies/1",
        mapping: dict[str, list[FakeElement]] | None = None,
    ) -> None:
        self.url = url
        self._mapping = mapping or {}

    def locator(self, selector: str) -> FakeList:
        return FakeList(self._mapping.get(selector, []))

    def get_by_role(self, role: str, name: Any = None) -> FakeList:
        return FakeList([])

    def wait_for_timeout(self, milliseconds: int) -> None:
        return


def test_external_response_is_a_blocker() -> None:
    page: Any = FakePage(
        mapping={"a[href$='/external_response']": [FakeElement()]}
    )

    assert detect_page_blocker(page) == "external_form"


def test_already_responded_status() -> None:
    page: Any = FakePage(mapping={".vacancy-response-section": [FakeElement()]})

    result = respond(page, "Здравствуйте!", lambda: False)

    assert result.status == STATUS_ALREADY_RESPONDED


def test_captcha_needs_manual_solution() -> None:
    page: Any = FakePage(url="https://career.habr.com/captcha?x=1")

    result = respond(page, "Здравствуйте!", lambda: False)

    assert result.status == STATUS_CAPTCHA


def test_missing_form_is_no_form() -> None:
    page: Any = FakePage()

    result = respond(page, "Здравствуйте!", lambda: False)

    assert result.status == STATUS_NO_FORM
    assert result.reason == ""


def test_external_form_reason_in_no_form() -> None:
    page: Any = FakePage(
        mapping={"a[href$='/external_response']": [FakeElement()]}
    )

    result = respond(page, "Здравствуйте!", lambda: False)

    assert result.status == STATUS_NO_FORM
    assert result.reason == "external_form"
