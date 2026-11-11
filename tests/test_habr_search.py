"""Тесты ленты «Подходящие» career.habr.com без браузера."""

from __future__ import annotations

from typing import Any

from agent.habr_search import (
    CARD_SELECTOR,
    HABR_MAX_PAGES,
    RESPONSES_ROW_SELECTOR,
    TOTAL_SELECTOR,
    build_search_url,
    collect_cards,
    collect_responses,
    pages_plan,
    parse_total,
    read_total,
    response_status,
    vacancy_id,
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
        return self._items[0] if self._items else FakeElement()


class FakeElement:
    """Заглушка Locator/Page: элементы отдаются по CSS-селектору."""

    def __init__(
        self,
        text: str = "",
        href: str = "",
        aria: str = "",
        visible: bool = True,
        mapping: dict[str, list["FakeElement"]] | None = None,
    ) -> None:
        self._text = text
        self._href = href
        self._aria = aria
        self._visible = visible
        self._mapping = mapping or {}

    @property
    def first(self) -> "FakeElement":
        return self

    def count(self) -> int:
        return 1

    def is_visible(self) -> bool:
        return self._visible

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str:
        if name == "href":
            return self._href
        if name == "aria-label":
            return self._aria
        return ""

    def locator(self, selector: str) -> FakeList:
        return FakeList(self._mapping.get(selector, []))


def card(
    *,
    title: str,
    company: str,
    href: str,
    salary: str = "",
    no_salary: bool = False,
    responded: bool = False,
) -> FakeElement:
    mapping: dict[str, list[FakeElement]] = {
        ".vacancy-card__title-link": [FakeElement(title)],
        ".vacancy-card__company a.link-comp": [FakeElement(company)],
        "a.vacancy-card__backdrop-link": [FakeElement(href=href)],
    }
    if salary:
        mapping[".vacancy-card__salary"] = [FakeElement(salary)]
    if no_salary:
        mapping[".vacancy-card__meta--noSalary"] = [FakeElement()]
    text = "Вы откликнулись" if responded else ""
    return FakeElement(text=text, mapping=mapping)


def test_build_search_url_pages() -> None:
    assert build_search_url(0) == "https://career.habr.com/vacancies?type=suitable"
    assert build_search_url(2) == "https://career.habr.com/vacancies?type=suitable&page=3"


def test_parse_total_handles_spaces() -> None:
    assert parse_total("Найдено 168 вакансий, которые подходят именно вам") == 168
    assert parse_total("Найдено\u00a01 234 вакансии") == 1234
    assert parse_total("вакансий нет") is None


def test_pages_plan_by_total() -> None:
    assert pages_plan(168) == 7
    assert pages_plan(10) == 1
    assert pages_plan(None) == HABR_MAX_PAGES


def test_vacancy_id() -> None:
    assert vacancy_id("https://career.habr.com/vacancies/1000167337") == "1000167337"
    assert vacancy_id("https://career.habr.com/vacancies?type=suitable") is None


def test_collect_cards_reads_fields() -> None:
    page = FakeElement(
        mapping={
            CARD_SELECTOR: [
                card(
                    title="Java-разработчик",
                    company="Ozon",
                    href="/vacancies/1",
                    salary="от 100 000 ₽",
                ),
                card(
                    title="QA инженер",
                    company="VK",
                    href="/vacancies/2?from=list",
                    no_salary=True,
                ),
            ]
        }
    )

    cards = collect_cards(page)  # type: ignore[arg-type]

    assert [item.title for item in cards] == ["Java-разработчик", "QA инженер"]
    assert cards[0].url == "https://career.habr.com/vacancies/1"
    assert cards[0].company == "Ozon"
    assert cards[0].salary == "от 100 000 ₽"
    assert cards[1].url == "https://career.habr.com/vacancies/2"
    assert cards[1].salary == ""


def test_read_total_uses_search_total() -> None:
    page: Any = FakeElement(
        mapping={TOTAL_SELECTOR: [FakeElement("Найдено 42 вакансии, которые подходят")]}
    )
    assert read_total(page) == 42

    empty: Any = FakeElement()
    assert read_total(empty) is None


def test_collect_cards_marks_already_responded() -> None:
    """Карточка с «Вы откликнулись» помечается: открывать её не нужно."""
    page = FakeElement(
        mapping={
            CARD_SELECTOR: [
                card(title="Java", company="Ozon", href="/vacancies/1", responded=True),
                card(title="QA", company="VK", href="/vacancies/2"),
            ]
        }
    )

    cards = collect_cards(page)  # type: ignore[arg-type]

    assert cards[0].already_responded is True
    assert cards[1].already_responded is False


def response_row(title: str, company: str, href: str, status: str) -> FakeElement:
    """Строка таблицы откликов: три ячейки, как на career.habr.com."""
    return FakeElement(
        mapping={
            "td": [
                FakeElement(
                    mapping={
                        ".title a": [FakeElement(title, href=href)],
                        ".comapny a": [FakeElement(company)],
                    }
                ),
                FakeElement(),
                FakeElement(mapping={".status": [FakeElement(status)]}),
            ]
        }
    )


def test_response_status_maps_habr_labels() -> None:
    assert response_status("Отказ") == "rejected"
    assert response_status("Приглашение") == "invite"
    assert response_status("Приглашение на собеседование") == "invite"
    assert response_status("Оффер") == "offer"
    assert response_status("Прочитано") == "sent"
    assert response_status("Не прочитано") == "sent"
    assert response_status("Неизвестно") == ""


def test_collect_responses_reads_rows() -> None:
    page = FakeElement(
        mapping={
            RESPONSES_ROW_SELECTOR: [
                FakeElement(),
                response_row("Java", "Ozon", "/vacancies/1", "Отказ"),
                response_row("QA", "VK", "/vacancies/2?from=list", "Не прочитано"),
            ]
        }
    )

    responses = collect_responses(page)  # type: ignore[arg-type]

    assert [item.url for item in responses] == [
        "https://career.habr.com/vacancies/1",
        "https://career.habr.com/vacancies/2",
    ]
    assert [item.title for item in responses] == ["Java", "QA"]
    assert [item.company for item in responses] == ["Ozon", "VK"]
    assert [item.status for item in responses] == ["rejected", "sent"]
