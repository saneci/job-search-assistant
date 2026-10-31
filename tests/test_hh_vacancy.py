"""Тесты определения тестов/анкет в форме отклика hh.ru (без браузера)."""

from __future__ import annotations

from typing import Any

import pytest

from agent.hh_vacancy import (
    ResumeNotFoundError,
    ResponseResult,
    _has_success,
    detect_page_blocker,
    detect_response_blocker,
    match_resume,
    response_notice,
    response_state,
    resume_name,
    resume_options,
    select_resume,
    set_resume_name,
)


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


class _Hidden:
    """Невидимый элемент: count = 0."""

    def count(self) -> int:
        return 0

    def is_visible(self) -> bool:
        return False

    def inner_text(self) -> str:
        return ""

    def evaluate(self, script: str) -> Any:
        return False

    def get_attribute(self, name: str) -> str:
        return ""


class FakeTextList:
    def __init__(self, items: list[FakeElement]) -> None:
        self._items = items

    @property
    def first(self) -> Any:
        return self._items[0] if self._items else _Hidden()

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> FakeElement:
        return self._items[index]


class FakeTextPage:
    """Страница-заглушка: поиск по тексту и пустые локаторы."""

    def __init__(self, mapping: dict[str, list[FakeElement]] | None = None) -> None:
        self.url = "https://hh.ru/vacancy/1"
        self._mapping = mapping or {}

    def get_by_text(self, text: str, exact: bool = False) -> FakeTextList:
        return FakeTextList(self._mapping.get(text, []))

    def get_by_role(self, role: str, name: Any = None) -> FakeTextList:
        return FakeTextList([])

    def locator(self, selector: str) -> FakeTextList:
        return FakeTextList([])


def test_detects_blocked_vacancy() -> None:
    """«У вас нет доступа к этой вакансии» — вакансию пропускаем."""
    page = FakeTextPage(
        {"У вас нет доступа к этой вакансии": [FakeElement("У вас нет доступа к этой вакансии")]}
    )

    assert detect_page_blocker(page) == "blocked"  # type: ignore[arg-type]


def test_detects_rejected_vacancy() -> None:
    """«Вам отказали» — отказ работодателя по отправленному отклику."""
    page = FakeTextPage({"Вам отказали": [FakeElement("Вам отказали")]})

    assert detect_page_blocker(page) == "rejected"  # type: ignore[arg-type]


def test_detects_invited_vacancy() -> None:
    """Отметка «Вас пригласили» — отклик уже не нужен."""
    page = FakeTextPage({"Вас пригласили": [FakeElement("Вас пригласили")]})

    assert detect_page_blocker(page) == "invited"  # type: ignore[arg-type]


def test_hidden_invited_badge_is_ignored() -> None:
    page = FakeTextPage({"Вас пригласили": [FakeElement("Вас пригласили", visible=False)]})

    assert detect_page_blocker(page) is None  # type: ignore[arg-type]


def test_open_vacancy_has_no_page_blocker() -> None:
    page = FakeTextPage({"Java-разработчик": [FakeElement("Java-разработчик")]})

    assert detect_page_blocker(page) is None  # type: ignore[arg-type]


def test_success_found_when_first_match_is_hidden() -> None:
    """Отклик отправлен: hh.ru держит невидимую заготовку раньше видимого сообщения."""
    page = FakeTextPage(
        {
            "Отклик отправлен": [
                FakeElement("Отклик отправлен", visible=False),
                FakeElement("Отклик отправлен", visible=True),
            ]
        }
    )

    assert _has_success(page)  # type: ignore[arg-type]


def test_success_not_found_when_all_matches_hidden() -> None:
    page = FakeTextPage({"Вы откликнулись": [FakeElement("Вы откликнулись", visible=False)]})

    assert not _has_success(page)  # type: ignore[arg-type]


def test_response_state_has_page_url_and_no_letter() -> None:
    state = response_state(FakeTextPage())  # type: ignore[arg-type]

    assert state == {"page_url": "https://hh.ru/vacancy/1"}


class FakeNoticeList:
    """Список элементов модалки с доступным `.first`."""

    def __init__(self, items: list[FakeElement]) -> None:
        self._items = items

    @property
    def first(self) -> Any:
        return self._items[0] if self._items else _Hidden()

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> FakeElement:
        return self._items[index]


class FakeNoticeModal(FakeElement):
    """Модалка формы отклика: отдаёт текстовые блоки по селектору."""

    def __init__(self, blocks: dict[str, list[FakeElement]]) -> None:
        super().__init__("")
        self._blocks = blocks

    def locator(self, selector: str) -> FakeNoticeList:
        return FakeNoticeList(self._blocks.get(selector, []))


class FakeNoticePage:
    """Страница с видимой модалкой отклика."""

    def __init__(self, modal: FakeNoticeModal) -> None:
        self.url = "https://hh.ru/vacancy/137974891"
        self._modal = modal

    def locator(self, selector: str) -> FakeNoticeList:
        if selector in ("[data-qa='modal-content']", "[role='dialog']"):
            return FakeNoticeList([self._modal])
        return FakeNoticeList([])

    def get_by_text(self, text: str, exact: bool = False) -> FakeTextList:
        return FakeTextList([])

    def get_by_role(self, role: str, name: Any = None) -> FakeTextList:
        return FakeTextList([])


def test_response_notice_reads_visibility_hint() -> None:
    """Текст в модалке — реальная причина, её пишем в отчёт вместо заглушки."""
    notice = (
        "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме на "
        "«Видно всем работодателям, зарегистрированным на hh.ru»"
    )
    page = FakeNoticePage(FakeNoticeModal({"p": [FakeElement(notice)]}))

    assert response_notice(page) == notice  # type: ignore[arg-type]


def test_response_notice_skips_ui_labels() -> None:
    """Подписи формы и заголовки — не причина отказа."""
    page = FakeNoticePage(
        FakeNoticeModal(
            {
                "p": [FakeElement("Сопроводительное письмо")],
                "div": [FakeElement("Отклик на вакансию")],
            }
        )
    )

    assert response_notice(page) == ""  # type: ignore[arg-type]


def test_response_result_reason_is_optional() -> None:
    assert ResponseResult(status="sent").reason == ""


@pytest.mark.parametrize(
    ("options", "name", "expected"),
    [
        (["Java Developer", "Team Lead"], "Java Developer", "Java Developer"),
        (["Java Developer", "Team Lead"], "  java   developer ", "Java Developer"),
        (["Java разработчик"], "Java разрабочик", None),
        ([], "Java Developer", None),
    ],
)
def test_match_resume_ignores_case_and_spaces(
    options: list[str], name: str, expected: str | None
) -> None:
    assert match_resume(options, name) == expected


def test_resume_options_are_read_from_listbox() -> None:
    scope = FakeScope(
        {"[role='option']": [FakeElement("Java Developer"), FakeElement("Team Lead")]}
    )

    assert resume_options(scope) == ["Java Developer", "Team Lead"]  # type: ignore[arg-type]


def test_select_resume_raises_when_name_is_missing() -> None:
    """Нет настроенного резюме в списке — отклик отправлять нельзя."""
    scope = FakeScope({"[role='option']": [FakeElement("Java Developer")]})

    with pytest.raises(ResumeNotFoundError) as error:
        select_resume(scope, "Team Lead")  # type: ignore[arg-type]

    assert error.value.resume == "Team Lead"
    assert error.value.options == ("Java Developer",)


def test_set_resume_name_strips_spaces() -> None:
    set_resume_name("  Java Developer ")

    assert resume_name() == "Java Developer"

    set_resume_name("")
