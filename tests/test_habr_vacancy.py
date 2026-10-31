"""Тесты состояний страницы вакансии career.habr.com без браузера."""

from __future__ import annotations

from typing import Any

from agent.habr_vacancy import detect_page_blocker, is_authenticated, respond
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


class AuthPage:
    """Страница проверки сессии: меню входа задаётся списком видимостей."""

    def __init__(
        self,
        final_url: str,
        *,
        menus: list[bool] | None = None,
        start_url: str | None = None,
    ) -> None:
        self.final_url = final_url
        self.menus = [
            FakeElement("Войти", visible=visible) for visible in (menus or [])
        ]
        self.url = start_url or final_url
        self.goto_url = ""

    def goto(self, url: str, **kwargs: Any) -> None:
        del kwargs
        self.goto_url = url
        self.url = self.final_url

    def wait_for_timeout(self, milliseconds: int) -> None:
        del milliseconds

    def locator(self, selector: str) -> FakeList:
        if selector in (".user-auth-menu", ".user-auth-menu button", ".user-auth-menu a"):
            return FakeList(self.menus)
        return FakeList([])


def test_habr_guest_is_not_authenticated() -> None:
    """Гостю шапка показывает видимую кнопку «Войти»."""
    page: Any = AuthPage("https://career.habr.com/", menus=[False, True])

    assert is_authenticated(page) is False


def test_habr_mobile_login_menu_does_not_count() -> None:
    """Скрытое мобильное меню входа не делает авторизованного гостём."""
    page: Any = AuthPage("https://career.habr.com/", menus=[False, False])

    assert is_authenticated(page) is True


def test_habr_user_is_authenticated() -> None:
    """У авторизованного пользователя кнопки «Войти» в шапке нет."""
    page: Any = AuthPage("https://career.habr.com/")

    assert is_authenticated(page) is True


def test_habr_login_page_is_not_authenticated() -> None:
    """Редирект на account.habr.com — сессии точно нет."""
    page: Any = AuthPage("https://account.habr.com/oauth/authorize/?action=login")

    assert is_authenticated(page) is False


def test_habr_session_check_navigates_to_home() -> None:
    """Проверка сессии не ходит в ленту, а открывает главную канала."""
    page: Any = AuthPage("https://career.habr.com/", start_url="about:blank")

    is_authenticated(page)

    assert page.goto_url == "https://career.habr.com/"


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
