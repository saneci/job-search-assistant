"""Тесты отклика другим резюме на hh.ru (без браузера)."""

from __future__ import annotations

import re
from typing import Any

import pytest

import agent.hh_vacancy as hh_vacancy
from agent.hh_vacancy import (
    STATUS_OTHER_RESUME,
    STATUS_SENT,
    STATUS_UNKNOWN,
    _has_success,
    _is_already_responded,
    _wait_for_response_result,
    detect_page_blocker,
    respond,
)
from agent.models import OTHER_RESUME_REASON


class FakeItem:
    """Элемент страницы: текст, видимость и клик."""

    def __init__(self, text: str = "", *, visible: bool = True) -> None:
        self._text = text
        self._visible = visible
        self.clicks = 0

    @property
    def first(self) -> "FakeItem":
        return self

    def count(self) -> int:
        return 1

    def nth(self, index: int) -> "FakeItem":
        return self

    def is_visible(self) -> bool:
        return self._visible

    def is_enabled(self) -> bool:
        return True

    def inner_text(self) -> str:
        return self._text

    def click(self) -> None:
        self.clicks += 1

    def get_attribute(self, name: str) -> str:
        return ""

    def evaluate(self, script: str) -> Any:
        return False


class FakeList:
    """Список элементов: count/nth/first, как у Locator."""

    def __init__(self, items: list[FakeItem]) -> None:
        self._items = items

    @property
    def first(self) -> FakeItem:
        return self._items[0] if self._items else FakeItem(visible=False)

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> FakeItem:
        return self._items[index]


class FakePage:
    """Страница вакансии: тексты, роли и селекторы."""

    def __init__(
        self,
        *,
        texts: dict[str, list[FakeItem]] | None = None,
        roles: dict[str, list[FakeItem]] | None = None,
        selectors: dict[str, list[FakeItem]] | None = None,
        url: str = "https://hh.ru/vacancy/1",
        modal_closes_after: int | None = None,
    ) -> None:
        self.url = url
        self._texts = texts or {}
        self._roles = roles or {}
        self._selectors = selectors or {}
        self._modal_closes_after = modal_closes_after
        self._modal_reads = 0
        self.timeouts = 0

    def get_by_text(self, text: str, exact: bool = False) -> FakeList:
        wanted = text.casefold()
        found: list[FakeItem] = []
        for label, items in self._texts.items():
            label = label.casefold()
            hit = label == wanted if exact else wanted in label
            if hit:
                found.extend(items)
        return FakeList(found)

    def get_by_role(self, role: str, name: Any = None) -> FakeList:
        items = self._roles.get(role, [])
        if name is None:
            return FakeList(items)
        pattern = (
            name if isinstance(name, re.Pattern) else re.compile(str(name), re.IGNORECASE)
        )
        return FakeList([item for item in items if pattern.search(item.inner_text())])

    def locator(self, selector: str) -> FakeList:
        if selector == "[data-qa='modal-content']" and self._modal_closes_after is not None:
            # Форма закрывается после заданного числа чтений: hh.ru отвечает медленно.
            self._modal_reads += 1
            visible = self._modal_reads <= self._modal_closes_after
            return FakeList([FakeItem("Отклик на вакансию", visible=visible)])
        return FakeList(self._selectors.get(selector, []))

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.timeouts += 1


def test_other_resume_button_means_current_profile_is_not_answered() -> None:
    """«Отклик отправлен» другим резюме не делает вакансию пройденной.

    В файл дня такая вакансия идёт со статусом skipped и отдельной причиной.
    """
    page = FakePage(
        texts={"Отклик отправлен": [FakeItem("Отклик отправлен")]},
        roles={"button": [FakeItem("Отклик другим резюме")]},
    )

    assert _has_success(page)  # type: ignore[arg-type]
    assert not _is_already_responded(page)  # type: ignore[arg-type]
    assert detect_page_blocker(page) == "other_resume"  # type: ignore[arg-type]


def test_other_resume_button_skips_vacancy_without_success_text() -> None:
    """Одна кнопка «Отклик другим резюме» — уже есть отклик другого резюме."""
    page = FakePage(roles={"button": [FakeItem("Отклик другим резюме")]})

    assert detect_page_blocker(page) == "other_resume"  # type: ignore[arg-type]


def test_success_without_other_resume_button_is_answered() -> None:
    """Без кнопки «Отклик другим резюме» отклик считается отправленным."""
    page = FakePage(texts={"Отклик отправлен": [FakeItem("Отклик отправлен")]})

    assert _is_already_responded(page)  # type: ignore[arg-type]


def test_hidden_other_resume_button_is_ignored() -> None:
    """Скрытая кнопка не даёт ложной надежды откликнуться повторно."""
    page = FakePage(
        texts={"Отклик отправлен": [FakeItem("Отклик отправлен")]},
        roles={"button": [FakeItem("Отклик другим резюме", visible=False)]},
    )

    assert _is_already_responded(page)  # type: ignore[arg-type]


def test_rejected_by_other_resume_is_skipped() -> None:
    """«Вам отказали» по другому резюме: вакансию пропускаем, а не откликаемся."""
    page = FakePage(
        texts={"Вам отказали": [FakeItem("Вам отказали")]},
        roles={"button": [FakeItem("Отклик другим резюме")]},
    )

    assert detect_page_blocker(page) == "other_resume"  # type: ignore[arg-type]


def test_rejected_without_other_resume_stays_terminal() -> None:
    """Без кнопки «Отклик другим резюме» отказ остаётся терминальным."""
    page = FakePage(texts={"Вам отказали": [FakeItem("Вам отказали")]})

    assert detect_page_blocker(page) == "rejected"  # type: ignore[arg-type]


def test_respond_skips_vacancy_with_other_resume_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Есть «Отклик другим резюме»: отклик не отправляем, вакансию пропускаем."""
    page = FakePage()

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("форма отклика не должна открываться")

    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "_find_action", lambda scope, selectors, pattern: None)
    monkeypatch.setattr(hh_vacancy, "_has_other_resume_action", lambda page: True)
    monkeypatch.setattr(hh_vacancy, "_wait_for_response_ui", unexpected)

    result = respond(page, "Здравствуйте!")  # type: ignore[arg-type]

    assert result.status == STATUS_OTHER_RESUME
    assert result.reason == OTHER_RESUME_REASON


def test_stale_success_does_not_confirm_our_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Старый текст об отклике другого резюме не считается нашей отправкой."""
    page = FakePage(
        texts={"Отклик отправлен": [FakeItem("Отклик отправлен")]},
        roles={"button": [FakeItem("Отклик другим резюме")]},
        selectors={"[data-qa='modal-content']": [FakeItem("Отклик на вакансию")]},
    )
    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "detect_response_blocker", lambda scope: None)

    result = _wait_for_response_result(page, None, seconds=0)

    assert result == STATUS_UNKNOWN


def test_stale_success_counts_after_form_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """После закрытия формы старый текст об успехе подтверждает отправку."""
    page = FakePage(
        texts={"Отклик отправлен": [FakeItem("Отклик отправлен")]},
        roles={"button": [FakeItem("Отклик другим резюме")]},
    )
    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "detect_response_blocker", lambda scope: None)

    result = _wait_for_response_result(page, None, seconds=0)

    assert result == STATUS_SENT


def test_waits_for_slow_form_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """hh.ru тормозит: ждём закрытия формы и подтверждаем отклик, а не ставим queued."""
    page = FakePage(
        texts={"Отклик отправлен": [FakeItem("Отклик отправлен")]},
        roles={"button": [FakeItem("Отклик другим резюме")]},
        modal_closes_after=2,
    )
    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "detect_response_blocker", lambda scope: None)

    result = _wait_for_response_result(page, None, seconds=30)

    assert result == STATUS_SENT
    # Первый опрос форма ещё открыта, второй — уже закрыта.
    assert page.timeouts >= 2


def test_fresh_success_is_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без старого текста успех подтверждается как раньше."""
    page = FakePage(
        texts={"Отклик отправлен": [FakeItem("Отклик отправлен")]},
        selectors={"[data-qa='modal-content']": [FakeItem("Отклик на вакансию")]},
    )
    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "detect_response_blocker", lambda scope: None)

    result = _wait_for_response_result(page, None, seconds=0)

    assert result == STATUS_SENT

