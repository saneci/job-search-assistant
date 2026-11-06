"""Тесты определения тестов/анкет в форме отклика hh.ru (без браузера)."""

from __future__ import annotations

from typing import Any

import pytest

from agent.hh_vacancy import (
    STATUS_UNKNOWN,
    ResumeNotFoundError,
    ResponseForm,
    ResponseResult,
    _has_success,
    _result,
    detect_page_blocker,
    detect_response_blocker,
    match_resume,
    open_vacancy,
    response_notice,
    response_state,
    resume_name,
    resume_options,
    select_resume,
    set_resume_name,
    submit_response,
)
from agent.models import SEND_FAILED_REASON, VacancyCard


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
    """Заглушка Locator/Page, отдающая элементы по CSS-селектору и по роли."""

    def __init__(
        self,
        mapping: dict[str, list[FakeElement]],
        roles: dict[str, list[FakeElement]] | None = None,
    ) -> None:
        self._mapping = mapping
        self._roles = roles or {}

    def locator(self, selector: str) -> "FakeList":
        return FakeList(self._mapping.get(selector, []))

    def get_by_role(self, role: str, name: Any = None) -> "FakeList":
        elements = self._roles.get(role, [])
        if name is not None and hasattr(name, "search"):
            elements = [item for item in elements if name.search(item.inner_text())]
        return FakeList(elements)


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


def test_detects_test_screen_by_link() -> None:
    """Кнопка теста у hh.ru бывает ссылкой Bloko: роль ссылки тоже признак."""
    scope = FakeScope({}, roles={"link": [FakeElement("Пройти тест")]})

    assert detect_response_blocker(scope) == "test"  # type: ignore[arg-type]


def test_does_not_treat_plain_link_as_test() -> None:
    """Обычная ссылка формы тестом не считается."""
    scope = FakeScope({}, roles={"link": [FakeElement("Сопроводительное письмо")]})

    assert detect_response_blocker(scope) is None  # type: ignore[arg-type]


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
    """Страница-заглушка: поиск по тексту, ролям и локаторам."""

    def __init__(
        self,
        mapping: dict[str, list[FakeElement]] | None = None,
        *,
        roles: dict[str, list[FakeElement]] | None = None,
        selectors: dict[str, list[FakeElement]] | None = None,
    ) -> None:
        self.url = "https://hh.ru/vacancy/1"
        self._mapping = mapping or {}
        self._roles = roles or {}
        self._selectors = selectors or {}
        self.selector_waits = 0

    def goto(self, url: str, **kwargs: Any) -> None:
        return None

    def wait_for_timeout(self, milliseconds: int) -> None:
        return None

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        self.selector_waits += 1

    def get_by_text(self, text: str, exact: bool = False) -> FakeTextList:
        if exact:
            return FakeTextList(self._mapping.get(text, []))
        found: list[FakeElement] = []
        for label, items in self._mapping.items():
            if text in label:
                found.extend(items)
        return FakeTextList(found)

    def get_by_role(self, role: str, name: Any = None) -> FakeTextList:
        return FakeTextList(self._roles.get(role, []))

    def locator(self, selector: str) -> FakeTextList:
        return FakeTextList(self._selectors.get(selector, []))


def test_detects_blocked_vacancy() -> None:
    """«У вас нет доступа к этой вакансии» — вакансию пропускаем."""
    page = FakeTextPage(
        {"У вас нет доступа к этой вакансии": [FakeElement("У вас нет доступа к этой вакансии")]}
    )

    assert detect_page_blocker(page) == "blocked"  # type: ignore[arg-type]


def test_detects_unavailable_vacancy() -> None:
    """«Вам недоступна эта вакансия» — резюме заблокировано, вакансию пропускаем."""
    page = FakeTextPage(
        {"Вам недоступна эта вакансия": [FakeElement("Вам недоступна эта вакансия")]}
    )

    assert detect_page_blocker(page) == "unavailable"  # type: ignore[arg-type]


def test_detects_unavailable_vacancy_in_explanation_block() -> None:
    """Заголовок вместе с пояснением в одном блоке — тоже недоступная вакансия."""
    text = "Вам недоступна эта вакансия. Войдите как пользователь, у которого есть доступ."
    page = FakeTextPage({text: [FakeElement(text)]})

    assert detect_page_blocker(page) == "unavailable"  # type: ignore[arg-type]


def test_detects_archived_vacancy() -> None:
    """«Вакансия в архиве» — работодатель закрыл вакансию, отклик невозможен."""
    page = FakeTextPage({"Вакансия в архиве": [FakeElement("Вакансия в архиве")]})

    assert detect_page_blocker(page) == "archived"  # type: ignore[arg-type]


def test_archived_vacancy_is_marked_in_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """Архивная вакансия попадает в отчёт с признаком archived."""
    page = FakeTextPage({"Вакансия в архиве": [FakeElement("Вакансия в архиве")]})
    card = VacancyCard(
        title="Team lead data platform",
        company="Ozon",
        url="https://hh.ru/vacancy/137925661",
    )
    monkeypatch.setattr("agent.hh_vacancy.detect_captcha", lambda page: False)

    details = open_vacancy(page, card)  # type: ignore[arg-type]

    assert details.blocker == "archived"
    assert details.archived is True


def test_unavailable_vacancy_is_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    """Закрытый доступ к вакансии — тоже архив: откликнуться нельзя."""
    page = FakeTextPage(
        {"Вам недоступна эта вакансия": [FakeElement("Вам недоступна эта вакансия")]}
    )
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/1")
    monkeypatch.setattr("agent.hh_vacancy.detect_captcha", lambda page: False)

    details = open_vacancy(page, card)  # type: ignore[arg-type]

    assert details.archived is True


def test_open_vacancy_skips_state_before_waiting_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Страницу-состояние пропускаем до ожидания заголовка: не тратим таймаут."""
    page = FakeTextPage(
        {"Вам недоступна эта вакансия": [FakeElement("Вам недоступна эта вакансия")]}
    )
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/1")
    monkeypatch.setattr("agent.hh_vacancy.detect_captcha", lambda page: False)

    details = open_vacancy(page, card)  # type: ignore[arg-type]

    assert details.blocker == "unavailable"
    # Данные для отчёта берутся из карточки: на экране состояния их нет.
    assert details.title == "Java"
    assert details.company == "Ozon"
    assert details.description == ""
    assert page.selector_waits == 0


def test_open_vacancy_reads_description_for_other_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Для «Отклик другим резюме» описание читается: вакансию нужно оценить."""
    page = FakeTextPage(
        mapping={"Отклик отправлен": [FakeElement("Отклик отправлен")]},
        roles={"button": [FakeElement("Отклик другим резюме")]},
        selectors={
            "h1.page-title__title": [FakeElement("Java Developer")],
            "[data-qa='vacancy-description']": [FakeElement("Java, Spring, PostgreSQL")],
        },
    )
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/139016311")
    monkeypatch.setattr("agent.hh_vacancy.detect_captcha", lambda page: False)

    details = open_vacancy(page, card)  # type: ignore[arg-type]

    assert details.blocker == "other_resume"
    assert details.description == "Java, Spring, PostgreSQL"


def test_rejected_text_in_description_is_ignored() -> None:
    """«Вам отказали» внутри описания вакансии — не состояние страницы."""
    text = "Если Вам отказали, попробуйте откликнуться другим резюме."
    page = FakeTextPage({text: [FakeElement(text, in_description=True)]})

    assert detect_page_blocker(page) is None  # type: ignore[arg-type]


def test_rejected_badge_with_extra_words_is_detected() -> None:
    """Отказ, отрисованный вместе с датой в одном блоке, — тоже отказ."""
    text = "Вам отказали 12 октября"
    page = FakeTextPage({text: [FakeElement(text)]})

    assert detect_page_blocker(page) == "rejected"  # type: ignore[arg-type]


def test_invited_text_in_description_is_ignored() -> None:
    """«Вас пригласили» внутри описания вакансии — не приглашение."""
    text = "Если Вас пригласили на собеседование, напишите нам."
    page = FakeTextPage({text: [FakeElement(text, in_description=True)]})

    assert detect_page_blocker(page) is None  # type: ignore[arg-type]


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


def test_response_notice_never_returns_visibility_hint() -> None:
    """Подсказка о видимости резюме не попадает в причину никогда."""
    notice = (
        "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме на "
        "«Видно всем работодателям, зарегистрированным на hh.ru»"
    )
    page = FakeNoticePage(FakeNoticeModal({"p": [FakeElement(notice)]}))

    assert response_notice(page) == ""  # type: ignore[arg-type]


def test_response_notice_skips_visibility_hint_in_explicit_block() -> None:
    """Подсказка о видимости в явном блоке notice тоже не причина."""
    notice = (
        "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме на "
        "«Видно всем работодателям, зарегистрированным на hh.ru»"
    )
    page = FakeNoticePage(
        FakeNoticeModal({"[data-qa='vacancy-response-popup-form-notice']": [FakeElement(notice)]})
    )

    assert response_notice(page) == ""  # type: ignore[arg-type]


def test_response_notice_hides_visibility_hint_outside_modal() -> None:
    """Подсказка на странице после закрытия модалки тоже не причина."""
    notice = (
        "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме на "
        "«Видно всем работодателям, зарегистрированным на hh.ru»"
    )

    class Page:
        url = "https://hh.ru/vacancy/1"

        def locator(self, selector: str) -> FakeNoticeList:
            if "notice" in selector:
                return FakeNoticeList([FakeElement(notice)])
            return FakeNoticeList([])

        def get_by_text(self, text: str, exact: bool = False) -> FakeTextList:
            return FakeTextList([])

        def get_by_role(self, role: str, name: Any = None) -> FakeTextList:
            return FakeTextList([])

    assert response_notice(Page()) == ""  # type: ignore[arg-type]


def test_result_uses_generic_reason_for_failed_send() -> None:
    """Если сайт молчит, отчёт не остаётся без причины."""
    assert _result(STATUS_UNKNOWN).reason == SEND_FAILED_REASON
    assert _result(STATUS_UNKNOWN, reason="Причина сайта").reason == "Причина сайта"


def test_result_keeps_failed_steps() -> None:
    """Шаги диагностики остаются в итоге: по ним видно, где встал отклик."""
    steps = {"письмо": "не добавлено: поле письма не появилось"}

    result = _result(STATUS_UNKNOWN, steps=steps)

    assert result.status == STATUS_UNKNOWN
    assert dict(result.steps) == steps


def test_not_ready_form_keeps_steps_in_result() -> None:
    """Форма не открылась: шаги открытия попадают в итог, а не теряются.

    Пустые `steps` в журнале означали, что непонятно, нажалась ли кнопка
    и что ответил сайт.
    """
    form = ResponseForm(
        STATUS_UNKNOWN,
        steps={"кнопка": "«Откликнуться»", "клик": "не удался: element is not stable"},
    )

    result = submit_response(object(), form, "Здравствуйте!")  # type: ignore[arg-type]

    assert result.status == STATUS_UNKNOWN
    assert result.reason == SEND_FAILED_REASON
    assert dict(result.steps) == {
        "кнопка": "«Откликнуться»",
        "клик": "не удался: element is not stable",
    }


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


def test_unit_tests_in_questions_are_not_a_test_screen() -> None:
    """«JUnit-тесты» и «покрытие тестами» в вопросах — не экран теста."""
    scope = FakeScope(
        {
            "[data-qa='modal-content']": [
                FakeElement(
                    "Есть ли у вас опыт написания JUnit-тестов?\n"
                    "Есть ли у вас опыт обеспечения покрытия тестами?"
                )
            ],
            "button": [FakeElement("Откликнуться")],
        }
    )

    assert detect_response_blocker(scope) is None  # type: ignore[arg-type]


def test_test_word_in_question_text_is_not_a_test_screen() -> None:
    """«Тестирование» в формулировке вопроса не делает форму экраном теста."""
    scope = FakeScope(
        {
            "[data-qa='modal-content']": [
                FakeElement("Есть ли у вас опыт тестирования и покрытия тестами?")
            ],
            "button": [FakeElement("Отправить")],
        }
    )

    assert detect_response_blocker(scope) is None  # type: ignore[arg-type]
