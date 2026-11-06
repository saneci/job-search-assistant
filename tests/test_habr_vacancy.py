"""Тесты состояний страницы вакансии career.habr.com без браузера."""

from __future__ import annotations

from typing import Any

import pytest

from agent.habr_vacancy import detect_page_blocker, is_authenticated, open_vacancy, respond
from agent.hh_vacancy import (
    PAGE_ARCHIVED,
    STATUS_ALREADY_RESPONDED,
    STATUS_CAPTCHA,
    STATUS_NO_FORM,
    VacancyParseError,
)
from agent.models import VacancyCard


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
        attrs: dict[str, str] | None = None,
    ) -> None:
        self._text = text
        self._visible = visible
        self._present = present
        self._mapping = mapping or {}
        self._attrs = attrs or {}

    @property
    def first(self) -> "FakeElement":
        return self

    def count(self) -> int:
        return 1 if self._present else 0

    def is_visible(self) -> bool:
        return self._visible

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str:
        return self._attrs.get(name, "")

    def locator(self, selector: str) -> FakeList:
        return FakeList(self._mapping.get(selector, []))


class FakePage:
    """Страница с заданной разметкой; капча определяется по адресу."""

    def __init__(
        self,
        url: str = "https://career.habr.com/vacancies/1",
        mapping: dict[str, list[FakeElement]] | None = None,
        texts: dict[str, list[FakeElement]] | None = None,
    ) -> None:
        self.url = url
        self._mapping = mapping or {}
        self._texts = texts or {}

    def goto(self, url: str, **kwargs: Any) -> None:
        del kwargs
        self.url = url

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        del selector, timeout

    def locator(self, selector: str) -> FakeList:
        return FakeList(self._mapping.get(selector, []))

    def get_by_text(self, text: str, exact: bool = False) -> FakeList:
        found: list[FakeElement] = []
        for label, items in self._texts.items():
            matches = label == text if exact else text in label
            if matches:
                found.extend(items)
        return FakeList(found)

    def get_by_role(self, role: str, name: Any = None) -> FakeList:
        del role, name
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


def test_habr_account_page_is_not_left_before_linking() -> None:
    """Страница связки аккаунтов: проверка не уводит с неё, иначе вход теряется."""
    page: Any = AuthPage(
        "https://career.habr.com/",
        start_url="https://account.habr.com/oauth/authorize/",
    )

    assert is_authenticated(page) is False
    assert page.goto_url == ""


def test_external_response_is_a_blocker() -> None:
    page: Any = FakePage(
        mapping={"a[href$='/external_response']": [FakeElement()]}
    )

    assert detect_page_blocker(page) == "external_form"


def test_description_falls_back_to_requirements_and_conditions() -> None:
    """Описание картинками: текст собирается из требований, условий и мета-описания."""
    page = FakePage(
        mapping={
            ".vacancy-description__text": [FakeElement("")],
            "meta[name='description']": [
                FakeElement(attrs={"content": "Вакансия удалённо в компании IRLIX."})
            ],
            ".content-section": [
                FakeElement(
                    mapping={
                        ".content-section__title": [FakeElement("Требования")],
                        ".vacancy-meta": [FakeElement("Бэкенд разработчик Middle Java")],
                    }
                ),
                FakeElement(
                    mapping={
                        ".content-section__title": [FakeElement("Условия")],
                        ".vacancy-meta": [FakeElement("Можно удалённо")],
                    }
                ),
            ],
        }
    )

    details = open_vacancy(page, VacancyCard(title="Java", company="IRLIX", url=page.url))  # type: ignore[arg-type]

    assert "Требования: Бэкенд разработчик Middle Java" in details.description
    assert "Условия: Можно удалённо" in details.description
    assert "Вакансия удалённо" in details.description


def test_description_images_are_collected_when_text_is_missing() -> None:
    """Описание картинками: адреса изображений из блока описания попадают в данные вакансии."""
    page = FakePage(
        mapping={
            ".vacancy-description__text": [
                FakeElement(
                    mapping={
                        "img": [
                            FakeElement(attrs={"src": "https://habrastorage.org/a.png"}),
                            FakeElement(attrs={"src": "https://habrastorage.org/b.png"}),
                        ]
                    }
                )
            ],
            "meta[name='description']": [
                FakeElement(attrs={"content": "Вакансия удалённо в компании IRLIX."})
            ],
        }
    )

    details = open_vacancy(page, VacancyCard(title="Java", company="IRLIX", url=page.url))  # type: ignore[arg-type]

    assert details.description_images == (
        "https://habrastorage.org/a.png",
        "https://habrastorage.org/b.png",
    )


def test_description_images_are_empty_when_text_exists() -> None:
    """Текст описания на месте: картинки к нему не примешиваются."""
    page = FakePage(
        mapping={
            ".vacancy-description__text": [
                FakeElement(
                    "Обязанности: разработка сервисов на Java.",
                    mapping={"img": [FakeElement(attrs={"src": "https://habrastorage.org/a.png"})]},
                )
            ]
        }
    )

    details = open_vacancy(page, VacancyCard(title="Java", company="IRLIX", url=page.url))  # type: ignore[arg-type]

    assert details.description == "Обязанности: разработка сервисов на Java."
    assert details.description_images == ()


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


def test_archived_page_is_a_skip_not_an_error() -> None:
    """Экран «Вакансия в архиве» — пропуск с признаком архива, а не ошибка."""
    page: Any = FakePage(
        mapping={"h1.page-title__title": [FakeElement("Java-разработчик")]},
        texts={"Вакансия в архиве": [FakeElement("Вакансия в архиве")]},
    )
    card = VacancyCard(
        title="Java-разработчик",
        company="Ozon",
        url="https://career.habr.com/vacancies/1",
    )

    details = open_vacancy(page, card)

    assert details.blocker == PAGE_ARCHIVED
    assert details.archived is True
    # Описания у архивной страницы нет, и это не повод падать в ошибку.
    assert details.description == ""
    assert details.title == "Java-разработчик"


def test_page_without_description_is_a_parse_error() -> None:
    """Обычная страница без описания остаётся ошибкой разбора."""
    page: Any = FakePage(mapping={"h1.page-title__title": [FakeElement("Java")]})
    card = VacancyCard(
        title="Java-разработчик",
        company="Ozon",
        url="https://career.habr.com/vacancies/1",
    )

    with pytest.raises(VacancyParseError):
        open_vacancy(page, card)
