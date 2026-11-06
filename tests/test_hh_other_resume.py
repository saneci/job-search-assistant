"""Тесты отклика другим резюме на hh.ru (без браузера)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError

import agent.hh_vacancy as hh_vacancy
from agent.hh_vacancy import (
    STATUS_NO_FORM,
    STATUS_REJECTED,
    STATUS_SENT,
    STATUS_UNKNOWN,
    _has_success,
    _is_already_responded,
    _wait_for_response_result,
    detect_page_blocker,
    open_response_form,
    respond,
    submit_response,
)
from agent.models import REJECTED_REASON, SEND_FAILED_REASON


@pytest.fixture(autouse=True)
def reset_resume_name():
    """Имя резюме — глобальное состояние модуля: не переносим его между тестами."""
    hh_vacancy.set_resume_name("")
    yield
    hh_vacancy.set_resume_name("")


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

    def click(self, timeout: int | None = None) -> None:
        del timeout
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


def test_rejected_with_other_resume_button_is_not_terminal() -> None:
    """«Вам отказали» + «Отклик другим резюме»: решает список резюме в форме.

    Поэтому вакансию не пропускаем по состоянию страницы: сначала смотрим, есть
    ли текущее резюме в списке.
    """
    page = FakePage(
        texts={"Вам отказали": [FakeItem("Вам отказали")]},
        roles={"button": [FakeItem("Отклик другим резюме")]},
    )

    assert detect_page_blocker(page) is None  # type: ignore[arg-type]


def test_rejected_without_other_resume_stays_terminal() -> None:
    """Без кнопки «Отклик другим резюме» отказ остаётся терминальным."""
    page = FakePage(texts={"Вам отказали": [FakeItem("Вам отказали")]})

    assert detect_page_blocker(page) == "rejected"  # type: ignore[arg-type]


def test_respond_clicks_other_resume_button(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет «Откликнуться», но есть «Отклик другим резюме»: кнопка нажимается."""
    page = FakePage()
    modal = FakePage()
    other = FakeItem("Отклик другим резюме")
    submit = FakeItem("Откликнуться")

    def fake_find(scope: Any, selectors: Any, pattern: Any, **kwargs: Any) -> Any:
        if scope is modal:
            return submit
        if pattern is hh_vacancy.OTHER_RESUME_RE:
            return other
        return None

    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "_find_action", fake_find)
    monkeypatch.setattr(hh_vacancy, "_wait_for_response_ui", lambda page, waiter: "ready")
    monkeypatch.setattr(hh_vacancy, "_response_scope", lambda page: modal)
    monkeypatch.setattr(hh_vacancy, "detect_response_blocker", lambda scope: None)
    monkeypatch.setattr(
        hh_vacancy, "_fill_letter", lambda page, scope, letter: (False, "")
    )
    monkeypatch.setattr(hh_vacancy, "_wait_for_response_result", lambda page, waiter: STATUS_SENT)

    result = respond(page, "Здравствуйте!")  # type: ignore[arg-type]

    assert other.clicks == 1
    assert submit.clicks == 1
    assert result.status == STATUS_SENT


def test_absent_resume_after_other_resume_means_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Текущего резюме нет в списке формы — им уже откликались, статус rejected."""
    page = FakePage()
    modal = FakePage()
    other = FakeItem("Отклик другим резюме")

    def fake_find(scope: Any, selectors: Any, pattern: Any, **kwargs: Any) -> Any:
        del scope, selectors
        if pattern is hh_vacancy.OTHER_RESUME_RE:
            return other
        return None

    def fake_select(scope: Any, name: str) -> None:
        del scope
        raise hh_vacancy.ResumeNotFoundError(name, ("Java Developer", "Team Lead"))

    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "_find_action", fake_find)
    monkeypatch.setattr(hh_vacancy, "_wait_for_response_ui", lambda page, waiter: "ready")
    monkeypatch.setattr(hh_vacancy, "_response_scope", lambda page: modal)
    monkeypatch.setattr(hh_vacancy, "detect_response_blocker", lambda scope: None)
    monkeypatch.setattr(hh_vacancy, "select_resume", fake_select)
    monkeypatch.setattr(hh_vacancy, "_dismiss", lambda page: None)
    hh_vacancy.set_resume_name("Team Lead")
    try:
        result = respond(page, "Здравствуйте!")  # type: ignore[arg-type]
    finally:
        hh_vacancy.set_resume_name("")

    assert other.clicks == 1
    assert result.status == STATUS_REJECTED
    assert result.reason == REJECTED_REASON


def test_missing_resume_list_is_queued_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Список резюме не открылся: это сбой сайта, а не отказ работодателя."""
    page = FakePage()
    modal = FakePage()
    other = FakeItem("Отклик другим резюме")

    def fake_find(scope: Any, selectors: Any, pattern: Any, **kwargs: Any) -> Any:
        del scope, selectors
        if pattern is hh_vacancy.OTHER_RESUME_RE:
            return other
        return None

    def fake_select(scope: Any, name: str) -> None:
        del scope
        raise hh_vacancy.ResumeNotFoundError(name)

    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "_find_action", fake_find)
    monkeypatch.setattr(hh_vacancy, "_wait_for_response_ui", lambda page, waiter: "ready")
    monkeypatch.setattr(hh_vacancy, "_response_scope", lambda page: modal)
    monkeypatch.setattr(hh_vacancy, "detect_response_blocker", lambda scope: None)
    monkeypatch.setattr(hh_vacancy, "select_resume", fake_select)
    monkeypatch.setattr(hh_vacancy, "_dismiss", lambda page: None)
    hh_vacancy.set_resume_name("Java Developer")
    try:
        result = respond(page, "Здравствуйте!")  # type: ignore[arg-type]
    finally:
        hh_vacancy.set_resume_name("")

    assert result.status == STATUS_UNKNOWN
    assert result.steps["резюме"] == "список резюме не открылся"


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


class OtherVacancyItem(FakeItem):
    """Кнопка карточки другой вакансии из блока «Вам подойдут эти вакансии»."""

    def evaluate(self, script: str) -> Any:
        del script
        return False


class OwnVacancyItem(FakeItem):
    """Кнопка открытой вакансии: рядом есть её заголовок."""

    def evaluate(self, script: str) -> Any:
        del script
        return True


def test_find_action_skips_other_vacancy_cards() -> None:
    """Кнопка «Откликнуться» из рекомендаций отбрасывается: отклик был бы чужим."""
    foreign = OtherVacancyItem("Откликнуться")
    own = OwnVacancyItem("Откликнуться")
    page = FakePage(roles={"button": [foreign, own]})

    action = hh_vacancy._find_action(  # type: ignore[arg-type]
        page, (), hh_vacancy.RESPOND_RE, only_current_vacancy=True
    )

    assert action is own


def test_find_action_without_own_button_returns_none() -> None:
    """Только чужие карточки: кликать не по чему, вакансия уходит в ручные."""
    page = FakePage(roles={"button": [OtherVacancyItem("Откликнуться")]})

    action = hh_vacancy._find_action(  # type: ignore[arg-type]
        page, (), hh_vacancy.RESPOND_RE, only_current_vacancy=True
    )

    assert action is None


class QaItem(FakeItem):
    """Кнопка с `data-qa`: по префиксу видно, чья это карточка."""

    def __init__(self, text: str, qa: str) -> None:
        super().__init__(text)
        self.qa = qa

    def get_attribute(self, name: str) -> str:
        return self.qa if name == "data-qa" else ""


def test_own_respond_button_kept_by_data_qa() -> None:
    """Кнопка вакансии с префиксом `vacancy-response` не отбрасывается."""
    own = QaItem("Откликнуться", "vacancy-response-link-top-again")
    page = FakePage(roles={"button": [own]})

    action = hh_vacancy._find_action(  # type: ignore[arg-type]
        page, (), hh_vacancy.RESPOND_RE, only_current_vacancy=True
    )

    assert action is own


def test_serp_respond_button_is_skipped() -> None:
    """Кнопка карточки выдачи и рекомендаций с префиксом `vacancy-serp` отбрасывается."""
    foreign = QaItem("Откликнуться", "vacancy-serp__vacancy_response")
    page = FakePage(roles={"button": [foreign]})

    action = hh_vacancy._find_action(  # type: ignore[arg-type]
        page, (), hh_vacancy.RESPOND_RE, only_current_vacancy=True
    )

    assert action is None


class BrokenClickItem(FakeItem):
    """Кнопка, клик по которой падает: hh.ru перерисовывает разметку."""

    def __init__(self, text: str = "", error: str = "element is not stable") -> None:
        super().__init__(text)
        self.error = error

    def click(self, timeout: int | None = None) -> None:
        del timeout
        self.clicks += 1
        raise PlaywrightError(self.error)


def test_failed_click_does_not_cancel_opened_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Клик упал, но форма открылась: отклик продолжается, ошибка попадает в шаги."""
    page = FakePage()
    button = BrokenClickItem("Откликнуться", error="element is not stable")

    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "_find_action", lambda scope, selectors, pattern, **kwargs: button)
    monkeypatch.setattr(hh_vacancy, "_wait_for_response_ui", lambda page, waiter: "ready")
    monkeypatch.setattr(hh_vacancy, "_response_scope", lambda page: page)

    form = open_response_form(page)  # type: ignore[arg-type]

    assert form.ready
    assert button.clicks == 2
    assert form.steps["кнопка"] == "«Откликнуться»"
    assert "element is not stable" in form.steps["клик"]


def test_failed_click_without_form_explains_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Клик упал и форма не открылась: причина и шаги не теряются."""
    page = FakePage()
    button = BrokenClickItem("Откликнуться", error="element is not attached")

    monkeypatch.setattr(hh_vacancy, "detect_captcha", lambda page: False)
    monkeypatch.setattr(hh_vacancy, "_find_action", lambda scope, selectors, pattern, **kwargs: button)
    monkeypatch.setattr(hh_vacancy, "_wait_for_response_ui", lambda page, waiter: "none")
    monkeypatch.setattr(hh_vacancy, "response_notice", lambda page: "")
    monkeypatch.setattr(hh_vacancy, "_dismiss", lambda page: None)

    form = open_response_form(page)  # type: ignore[arg-type]
    result = submit_response(page, form, "Здравствуйте!")  # type: ignore[arg-type]

    assert result.status == STATUS_NO_FORM
    assert result.reason == SEND_FAILED_REASON
    assert result.steps["форма"] == "не открылась за отведённое время"
    assert "element is not attached" in result.steps["клик"]

