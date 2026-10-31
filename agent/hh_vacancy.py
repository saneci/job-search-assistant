"""Страница вакансии hh.ru: разбор данных, проверки и отправка отклика."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Sequence
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .browser import NAVIGATION_TIMEOUT_MS, CaptchaError, PageLoadError, detect_captcha
from .models import REJECTED_REASON, VacancyCard, VacancyDetails

TITLE_SELECTORS = ("h1[data-qa='vacancy-title']", "h1.bloko-header-1", "h1")
DESCRIPTION_SELECTORS = (
    "[data-qa='vacancy-description']",
    "div.vacancy-description",
    "[data-qa='vacancy-description-content']",
)
COMPANY_SELECTORS = (
    "[data-qa='vacancy-company-name']",
    "a[data-qa='vacancy-company-name']",
    "[data-qa='vacancy-company-name'] a",
)
SALARY_SELECTORS = (
    "[data-qa='vacancy-salary']",
    "[data-qa='vacancy-compensation']",
)

RESPOND_BUTTON_SELECTORS = (
    "a[data-qa='vacancy-response-link-top']",
    "button[data-qa='vacancy-response-link-top']",
    "a[data-qa='vacancy-response-link']",
)
SUBMIT_SELECTORS = (
    "[data-qa='vacancy-response-submit-popup']",
    "[data-qa='vacancy-response-submit']",
)
LETTER_TOGGLE_SELECTORS = ("[data-qa='vacancy-response-letter-toggle']",)
LETTER_TEXTAREA_SELECTORS = (
    "textarea[data-qa='vacancy-response-popup-form-letter-input']",
    "textarea[data-qa='vacancy-response-letter-input']",
    "textarea[name='letter']",
    "textarea",
)
MODAL_SELECTORS = (
    "[data-qa='modal-content']",
    "[role='dialog'][aria-modal='true']",
    "[role='dialog']",
)

# Информационные сообщения формы отклика. hh.ru объясняет в них, почему отклик
# нельзя отправить (например, резюме скрыто от работодателей).
NOTICE_SELECTORS = (
    "[data-qa='vacancy-response-popup-form-notice']",
    "[data-qa='vacancy-response-notice']",
    "[data-qa*='visibility']",
    "[data-qa*='hint']",
    "[data-qa*='notice']",
    "[data-qa*='alert']",
    "[role='alert']",
)
# Сообщения, которые видно на странице после неудачной отправки.
PAGE_NOTICE_SELECTORS = (
    "[data-qa*='error']",
    "[data-qa*='notice']",
    "[data-qa*='alert']",
    "[data-qa*='notification']",
    "[role='alert']",
    "div[class*='notification']",
    "div[class*='toast']",
)
NOTICE_MIN_LENGTH = 20
NOTICE_MAX_LENGTH = 400
# Интерфейсные подписи формы: это не причина, их в отчёт не пишем.
UI_TEXT_RE = re.compile(
    r"^(отклик на вакансию|сопроводительное письмо|откликнуться|сгенерировать|"
    r"видно всем работодателям|видно только выбранным работодателям)\s*$",
    re.IGNORECASE,
)
UI_CLOSEST_SELECTOR = (
    "button, [role='button'], h1, h2, h3, h4, h5, h6, [role='heading'], "
    "label, select, option, [role='combobox'], [role='listbox'], [role='option'], "
    "[contenteditable], textarea, [data-qa='vacancy-description'], .vacancy-description"
)
# Признаки объяснения причины: по ним отличаем сообщение от названия резюме.
CAUSE_TEXT_RE = re.compile(
    r"чтобы|нельзя|невозможн|поменяйте|измените|заполните|укажите|"
    r"требует|необходим|должн|доступн|видимост|отклик",
    re.IGNORECASE,
)
# Текст контейнера формы: если он попал целиком, это обёртка, а не причина.
CONTAINER_TEXT_RE = re.compile(
    r"сопроводительное письмо|отклик на вакансию|сгенерировать",
    re.IGNORECASE,
)
# Только прямые текстовые узлы элемента: обёртка с вложенными блоками даёт пусто.
DIRECT_TEXT_SCRIPT = (
    "el => Array.from(el.childNodes)"
    ".filter(node => node.nodeType === 3)"
    ".map(node => node.textContent || '')"
    ".join(' ')"
    ".replace(/\\s+/g, ' ')"
    ".trim()"
)

STATUS_SENT = "sent"
STATUS_ALREADY_RESPONDED = "already_responded"
# Текущего резюме нет в форме отклика: им уже откликались, пришёл отказ.
STATUS_REJECTED = "rejected"
STATUS_CAPTCHA = "captcha"
STATUS_NO_FORM = "no_form"
STATUS_UNKNOWN = "unknown"

# Состояния самой страницы вакансии. Это не «блокеры» теста/анкеты: такие
# вакансии не откладываем, а помечаем пройденными (skipped или rejected).
PAGE_BLOCKED = "blocked"
# Экран «Вам недоступна эта вакансия»: резюме заблокировано или доступ закрыт.
PAGE_UNAVAILABLE = "unavailable"
PAGE_INVITED = "invited"
PAGE_REJECTED = "rejected"
# Кнопка «Отклик другим резюме»: по вакансии уже есть отклик другого резюме.
PAGE_OTHER_RESUME = "other_resume"
# Состояния страницы, при которых вакансию пропускаем сразу после открытия.
PAGE_STATES = (PAGE_BLOCKED, PAGE_UNAVAILABLE, PAGE_INVITED, PAGE_REJECTED, PAGE_OTHER_RESUME)
BLOCKED_TITLE = "У вас нет доступа к этой вакансии"
UNAVAILABLE_TITLE = "Вам недоступна эта вакансия"
INVITED_TITLE = "Вас пригласили"
REJECTED_TITLE = "Вам отказали"

RESPOND_RE = re.compile(r"^\s*откликнуться\s*$", re.IGNORECASE)
# Кнопка «Отклик другим резюме»: отклик по вакансии уже отправлен другим резюме.
OTHER_RESUME_RE = re.compile(r"^\s*отклик другим резюме\s*$", re.IGNORECASE)
OTHER_RESUME_SELECTORS = (
    "[data-qa='vacancy-response-link-other-resume']",
    "[data-qa*='other-resume']",
    "[data-qa*='different-resume']",
)
LETTER_TOGGLE_RE = re.compile(r"добавить сопроводительное", re.IGNORECASE)
TEST_ACTION_RE = re.compile(
    r"пройти тест|начать тест|пройти тестирование|запустить тест", re.IGNORECASE
)
QUESTIONNAIRE_ACTION_RE = re.compile(
    r"заполнить анкет|пройти анкет|ответить на вопросы|ответьте на вопросы|начать опрос",
    re.IGNORECASE,
)
EXTERNAL_ACTION_RE = re.compile(r"на сайте работодателя|перейти на сайт", re.IGNORECASE)

# Признаки теста/анкеты в форме отклика (описание вакансии сюда не попадает).
QUESTIONNAIRE_TEXT_RE = re.compile(r"анкет|опросник|ответь(те)? на вопросы", re.IGNORECASE)
TEST_TEXT_RE = re.compile(
    r"\bтест(?:ы|а|ов)?\b|\bтестирован\w*|\bтестовое задание", re.IGNORECASE
)
EXTERNAL_TEXT_RE = re.compile(
    r"сайт[еа]? работодателя|внешн(?:яя|ей|юю) (?:форм|анкет)", re.IGNORECASE
)

QUESTIONNAIRE_SELECTORS = (
    "[data-qa='vacancy-response-questionnaire']",
    "[data-qa*='questionnaire']",
    "a[href*='questionnaire']",
)
TEST_SELECTORS = (
    "[data-qa='vacancy-response-test']",
    "[data-qa='vacancy-test']",
    "[data-qa*='-test']",
)

# Текст формы отклика: модалка, заголовки, кнопки и блоки с data-qa="*response*".
RESPONSE_TEXT_SELECTORS = (
    "[data-qa='modal-content']",
    "[role='dialog']",
    "h1",
    "h2",
    "h3",
    "button",
    "[role='button']",
    "[data-qa*='response']",
)

SUCCESS_TEXTS = (
    "Отклик отправлен",
    "Отклик успешно отправлен",
    "Резюме отправлено",
    "Вы откликнулись",
)

RESPONSE_UI_WAIT_SECONDS = 10
RESPONSE_RESULT_WAIT_SECONDS = 15

# Сколько ждём готовность страницы вакансии: заголовок или экран-состояние.
PAGE_READY_WAIT_SECONDS = 15
# Как часто проверяем готовность: браузер отвечает быстро, пауза нужна, чтобы
# не крутить проверки вхолостую.
PAGE_READY_POLL_MS = 500

# После вставки письма hh.ru перерисовывает форму: если кликнуть «Откликнуться»
# сразу, React ещё не применил текст, и нажатие уходит в пустоту.
LETTER_SETTLE_MS = 1000

# Выпадающий список резюме в форме отклика: нативный select или кастомный список.
# Порядок важен: сначала точные признаки, затем общий data-qa с «resume».
RESUME_TRIGGER_SELECTORS = (
    "[data-qa='resume-title']",
    "[data-qa*='select-resume']",
    "[data-qa*='resume-select']",
    "button[data-qa*='resume']",
    "[data-qa*='resume']",
    "[role='combobox']",
)
RESUME_SELECT_SELECTORS = (
    "select[data-qa*='resume']",
    "select[name*='resume']",
    "[data-qa*='select-resume'] select",
    "[role='combobox'] select",
    "select",
)
RESUME_OPTION_SELECTORS = (
    "[role='option']",
    "[data-qa*='resume-option']",
    "option",
)
# Сколько ждём появления опций после открытия кастомного списка.
RESUME_OPTIONS_WAIT_MS = 1000
RESUME_OPTIONS_POLL_MS = 100
# Сколько элементов одного селектора просматриваем при поиске видимого.
VISIBLE_SCAN_LIMIT = 20

# Резюме выбирается внутри модалки, поэтому имя задаёт канал на запуск:
# сигнатура respond остаётся прежней, а повтор с другим резюме создаёт новый канал.
_resume_name = ""


class ResumeNotFoundError(RuntimeError):
    """В форме отклика нет резюме с настроенным названием."""

    def __init__(self, resume: str, options: Sequence[str] = ()) -> None:
        super().__init__(f"в форме отклика нет резюме «{resume}»")
        self.resume = resume
        self.options = tuple(options)


def set_resume_name(name: str) -> None:
    """Задаёт резюме, которое агент выбирает в форме отклика."""
    global _resume_name
    _resume_name = (name or "").strip()


def resume_name() -> str:
    """Резюме, которое агент выбирает в форме отклика."""
    return _resume_name


def _resume_words(text: str) -> str:
    """Нормализация названия резюме для сравнения."""
    return " ".join((text or "").lower().replace("ё", "е").split())


def match_resume(options: Sequence[str], name: str) -> str | None:
    """Название опции, совпадающее с резюме: регистр и пробелы не важны."""
    wanted = _resume_words(name)
    for option in options:
        if _resume_words(option) == wanted:
            return option
    return None


def _option_texts(select: Locator) -> list[str]:
    """Названия опций нативного select: они не считаются видимыми до открытия."""
    try:
        options = select.locator("option")
        total = min(options.count(), 50)
    except PlaywrightError:
        return []
    texts: list[str] = []
    for index in range(total):
        try:
            text = (options.nth(index).inner_text() or "").strip()
        except PlaywrightError:
            continue
        if text:
            texts.append(text)
    return texts


def _native_resume_select(
    scope: Locator | Page, name: str = ""
) -> tuple[Locator | None, list[str]]:
    """Нативный select с резюме и его опции; без него — (None, []).

    Если в форме несколько select, предпочитаем тот, где есть нужное имя:
    название резюме надёжнее порядка элементов.
    """
    fallback: tuple[Locator, list[str]] | None = None
    for selector in RESUME_SELECT_SELECTORS:
        locator = scope.locator(selector)
        try:
            total = min(locator.count(), 5)
        except PlaywrightError:
            continue
        for index in range(total):
            select = locator.nth(index)
            if not _is_visible(select):
                continue
            texts = _option_texts(select)
            if not texts:
                continue
            if fallback is None:
                fallback = (select, texts)
            if name and match_resume(texts, name) is not None:
                return select, texts
    if name:
        # Нужного имени нет: это может быть не резюме, а другой select формы.
        return None, fallback[1] if fallback is not None else []
    return fallback if fallback is not None else (None, [])


def _resume_roots(scope: Locator | Page) -> list[Locator | Page]:
    """Где искать элементы списка резюме: форма отклика и страница.

    hh.ru рисует попап списка в портале `[data-qa='drop-base']` прямо в body,
    поэтому внутри модалки опций нет: после формы смотрим всю страницу.
    """
    roots: list[Locator | Page] = [scope]
    page = getattr(scope, "page", None)
    if page is not None and page is not scope:
        roots.append(page)
    return roots


def _option_texts_in_root(root: Locator | Page) -> list[str]:
    """Видимые названия опций одного корня."""
    for selector in RESUME_OPTION_SELECTORS:
        locator = root.locator(selector)
        try:
            total = min(locator.count(), 50)
        except PlaywrightError:
            continue
        texts: list[str] = []
        for index in range(total):
            item = locator.nth(index)
            if not _is_visible(item):
                continue
            try:
                text = (item.inner_text() or "").strip()
            except PlaywrightError:
                continue
            if text:
                texts.append(text)
        if texts:
            return texts
    return []


def _visible_resume_options(scope: Locator | Page) -> list[str]:
    """Видимые опции кастомного списка резюме: форма и портал страницы."""
    for root in _resume_roots(scope):
        texts = _option_texts_in_root(root)
        if texts:
            return texts
    return []


def _wait_for_resume_options(scope: Locator | Page) -> list[str]:
    """Ждёт опции после открытия кастомного списка: анимация и подгрузка."""
    deadline = time.monotonic() + RESUME_OPTIONS_WAIT_MS / 1000
    while True:
        options = _visible_resume_options(scope)
        if options or time.monotonic() >= deadline:
            return options
        time.sleep(RESUME_OPTIONS_POLL_MS / 1000)


def _find_resume_option(scope: Locator | Page, text: str) -> Locator | None:
    """Видимая опция с нужным названием: регистр и пробелы не важны."""
    wanted = _resume_words(text)
    for root in _resume_roots(scope):
        option = _find_option_in_root(root, wanted)
        if option is not None:
            return option
    return None


def _find_option_in_root(root: Locator | Page, wanted: str) -> Locator | None:
    """Опция с нужным названием в одном корне."""
    for selector in RESUME_OPTION_SELECTORS:
        locator = root.locator(selector)
        try:
            total = min(locator.count(), 50)
        except PlaywrightError:
            continue
        for index in range(total):
            item = locator.nth(index)
            if not _is_visible(item):
                continue
            try:
                value = (item.inner_text() or "").strip()
            except PlaywrightError:
                continue
            if _resume_words(value) == wanted:
                return item
    return None


def resume_options(scope: Locator | Page) -> list[str]:
    """Названия резюме из выпадающего списка формы отклика.

    Нативный select отдаёт опции без открытия списка, кастомный — только
    видимые опции: перед их чтением список нужно открыть.
    """
    _select, options = _native_resume_select(scope)
    if options:
        return options
    return _visible_resume_options(scope)


def select_resume(scope: Locator | Page, name: str) -> None:
    """Выбирает резюме по названию; без совпадения — ResumeNotFoundError."""
    select, options = _native_resume_select(scope, name)
    if select is not None:
        matched = match_resume(options, name)
        if matched is None:
            raise ResumeNotFoundError(name, options)
        try:
            select.select_option(label=matched)
        except PlaywrightError as exc:
            raise ResumeNotFoundError(name, options) from exc
        return

    # Кастомный список: опции появляются только после клика по триггеру.
    native_options = options
    options = _visible_resume_options(scope)
    if not options:
        trigger = _first_visible(scope, RESUME_TRIGGER_SELECTORS)
        if trigger is not None:
            try:
                trigger.click()
            except PlaywrightError:
                trigger = None
            if trigger is not None:
                options = _wait_for_resume_options(scope)

    matched = match_resume(options, name)
    if matched is None:
        raise ResumeNotFoundError(name, options or native_options)
    option = _find_resume_option(scope, matched)
    if option is None:
        raise ResumeNotFoundError(name, options or native_options)
    try:
        option.click()
    except PlaywrightError as exc:
        raise ResumeNotFoundError(name, options or native_options) from exc


class VacancyParseError(RuntimeError):
    """Не удалось разобрать страницу вакансии."""


@dataclass(frozen=True)
class ResponseResult:
    """Итог попытки отклика: статус и причина, которую показал hh.ru.

    `reason` — дословный текст информационного сообщения hh.ru (например,
    требование изменить видимость резюме). Пустая строка значит, что сайт
    причину не объяснил, и в отчёт идёт общий текст для статуса.
    """

    status: str
    reason: str = ""


def open_vacancy(page: Page, card: VacancyCard) -> VacancyDetails:
    """Открывает страницу вакансии и извлекает данные."""
    try:
        page.goto(card.url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise PageLoadError(
            f"страница вакансии не загрузилась за {NAVIGATION_TIMEOUT_MS // 1000} с: {card.url}"
        ) from exc
    page.wait_for_timeout(1500)
    if detect_captcha(page):
        raise CaptchaError(f"hh.ru показал капчу на странице вакансии: {card.url}")

    # Ждём готовность страницы: заголовок вакансии или экран-состояние (нет
    # доступа, приглашение, чужой отклик). Что появилось раньше, то и решает:
    # пропуск не простаивает в таймауте.
    state = _wait_for_page_ready(page)
    if state is not None:
        return _page_state_details(card, state)

    title = _first_text(page, TITLE_SELECTORS) or card.title
    company = _first_text(page, COMPANY_SELECTORS) or card.company
    salary = _first_text(page, SALARY_SELECTORS) or card.salary
    if salary and "не указан" in salary.lower():
        salary = ""

    company_name = company.splitlines()[0].strip() if company else ""
    blocker = detect_page_blocker(page)
    if blocker in PAGE_STATES:
        # На такой странице нет ни описания, ни формы отклика: вакансию
        # пропускаем сразу, без разбора и без обращения к LLM.
        return _page_state_details(card, blocker)

    description = _first_text(page, DESCRIPTION_SELECTORS)
    if not description:
        raise VacancyParseError(f"Не удалось извлечь описание вакансии: {card.url}")

    return VacancyDetails(
        url=card.url,
        title=title,
        company=company_name,
        salary=salary,
        description=description,
        already_responded=_is_already_responded(page),
        blocker=blocker,
    )


def _page_state_details(card: VacancyCard, state: str) -> VacancyDetails:
    """Запись вакансии, которую надо сразу пропустить: данных страницы не читаем.

    На экране состояния нет ни названия вакансии, ни описания, поэтому в отчёт
    идут название и компания из карточки выдачи.
    """
    return VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary=card.salary,
        description="",
        blocker=state,
    )


def detect_page_blocker(page: Page) -> str | None:
    """Ищет на странице вакансии повод не откликаться автоматически.

    Кроме теста, анкеты и внешней формы ловим состояния самой страницы:
    вакансия больше недоступна («У вас нет доступа к этой вакансии»,
    «Вам недоступна эта вакансия»), работодатель уже пригласил откликнуться
    («Вас пригласили») и отклик по вакансии уже отправлен другим резюме
    (кнопка «Отклик другим резюме»). Это не отложенные вакансии, а пройденные:
    отклик там не нужен.
    """
    state = _detect_page_state(page)
    if state is not None:
        return state
    if _has_selector(page, QUESTIONNAIRE_SELECTORS) or _has_role_name(page, QUESTIONNAIRE_ACTION_RE):
        return "questionnaire"
    if _has_selector(page, TEST_SELECTORS) or _has_role_name(page, TEST_ACTION_RE):
        return "test"
    if _has_role_name(page, EXTERNAL_ACTION_RE):
        return "external_form"
    return None


def _wait_for_page_ready(page: Page) -> str | None:
    """Ждёт заголовок вакансии или экран-состояние — что появится раньше.

    Возвращает состояние страницы, при котором отклик не нужен, иначе None.
    Заголовок обычно появляется сразу, а приглашение и отказ могут отрисоваться
    позже: проверяем оба признака в одном ожидании, чтобы вакансия с готовым
    приглашением или отказом пропускалась сразу, а не после таймаута.
    """
    deadline = time.monotonic() + PAGE_READY_WAIT_SECONDS
    while True:
        state = _detect_page_state(page)
        if state is not None:
            return state
        if _has_selector(page, TITLE_SELECTORS):
            return None
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(PAGE_READY_POLL_MS)


def _detect_page_state(page: Page) -> str | None:
    """Состояние самой страницы, при котором отклик уже не нужен."""
    if _has_exact_text(page, BLOCKED_TITLE):
        return PAGE_BLOCKED
    if _has_text(page, UNAVAILABLE_TITLE):
        # Резюме заблокировано или вакансия закрыта для просмотра.
        return PAGE_UNAVAILABLE
    if _has_text_outside_description(page, INVITED_TITLE):
        return PAGE_INVITED
    other_resume = _has_other_resume_action(page)
    rejected = _has_text_outside_description(page, REJECTED_TITLE)
    if rejected and other_resume:
        # «Вам отказали» вместе с «Отклик другим резюме»: чей это отказ, решает
        # список резюме в форме отклика, поэтому вакансию не пропускаем.
        return None
    if other_resume:
        # Отклик по вакансии уже есть у другого резюме, отказа нет: пропускаем.
        return PAGE_OTHER_RESUME
    if rejected:
        # Работодатель отказал по отклику текущего резюме: статус rejected.
        return PAGE_REJECTED
    return None


def detect_response_blocker(scope: Locator | Page) -> str | None:
    """Ищет признаки теста/анкеты/внешней формы в форме отклика."""
    if _has_selector(scope, QUESTIONNAIRE_SELECTORS):
        return "questionnaire"
    if _has_selector(scope, TEST_SELECTORS):
        return "test"

    text = _response_text(scope)
    if QUESTIONNAIRE_TEXT_RE.search(text):
        return "questionnaire"
    if TEST_TEXT_RE.search(text):
        return "test"
    if EXTERNAL_TEXT_RE.search(text):
        return "external_form"

    if not isinstance(scope, Page) and _has_external_link(scope):
        return "external_form"
    return None


def _response_text(scope: Locator | Page) -> str:
    """Текст интерфейса формы отклика: заголовки, кнопки, модалка (без описания вакансии)."""
    parts: list[str] = []
    for selector in RESPONSE_TEXT_SELECTORS:
        try:
            items = scope.locator(selector)
            total = min(items.count(), 30)
        except PlaywrightError:
            continue
        for index in range(total):
            try:
                item = items.nth(index)
                if not item.is_visible():
                    continue
                if selector in ("h1", "h2", "h3") and _inside_description(item):
                    continue
                text = item.inner_text().strip()
            except PlaywrightError:
                continue
            if text:
                parts.append(text)
    return "\n".join(dict.fromkeys(parts))


def _inside_description(locator: Locator) -> bool:
    """Заголовок находится внутри описания вакансии — это не форма отклика."""
    try:
        return bool(
            locator.evaluate(
                "el => !!el.closest(\"[data-qa='vacancy-description'], .vacancy-description\")"
            )
        )
    except PlaywrightError:
        return False


def respond(
    page: Page,
    letter: str,
    captcha_waiter: Callable[[], bool] | None = None,
) -> ResponseResult:
    """Отправляет отклик с сопроводительным письмом.

    `captcha_waiter` вызывается, когда hh.ru просит капчу: приложение только ждёт
    ручного решения и продолжает, если человек её прошёл.

    Вместе со статусом возвращаем причину, которую hh.ru показал в форме:
    без неё непонятно, почему отклик не ушёл (например, резюме скрыто).
    """
    if detect_captcha(page) and not _solve_captcha(captcha_waiter):
        return _result(STATUS_CAPTCHA)

    button = _find_action(page, RESPOND_BUTTON_SELECTORS, RESPOND_RE)
    other_resume = False
    if button is None:
        # Кнопка «Отклик другим резюме»: по вакансии уже есть отклик другого
        # резюме. Открываем форму и смотрим список резюме — есть ли текущее.
        button = _find_action(page, OTHER_RESUME_SELECTORS, OTHER_RESUME_RE)
        other_resume = button is not None
    if button is None:
        if _is_already_responded(page):
            return _result(STATUS_ALREADY_RESPONDED)
        return _result(STATUS_NO_FORM, page)

    try:
        button.click()
    except PlaywrightError:
        return _result(STATUS_UNKNOWN, page)

    ui = _wait_for_response_ui(page, captcha_waiter)
    if ui == STATUS_CAPTCHA:
        return _result(STATUS_CAPTCHA)
    if ui in ("test", "questionnaire", "external_form"):
        reason = response_notice(page)
        _dismiss(page)
        return _result(ui, reason=reason)
    if ui != "ready":
        reason = response_notice(page)
        _dismiss(page)
        return _result(STATUS_NO_FORM, reason=reason)

    scope = _response_scope(page)
    blocker = detect_response_blocker(scope)
    if blocker:
        reason = response_notice(page)
        _dismiss(page)
        return _result(blocker, reason=reason)

    # Резюме выбираем до письма: hh.ru привязывает отклик к выбранному резюме.
    if _resume_name:
        try:
            select_resume(scope, _resume_name)
        except ResumeNotFoundError:
            if not other_resume:
                raise
            # Форму открыли кнопкой «Отклик другим резюме», а текущего резюме в
            # списке нет: им уже откликались, значит по нему пришёл отказ.
            _dismiss(page)
            return _result(STATUS_REJECTED, reason=REJECTED_REASON)

    # Письмо вставляем, если форма позволяет (по ТЗ).
    if _fill_letter(page, scope, letter):
        page.wait_for_timeout(LETTER_SETTLE_MS)

    submit = _find_action(scope, SUBMIT_SELECTORS, RESPOND_RE)
    if submit is None:
        blocker = detect_response_blocker(scope)
        reason = response_notice(page)
        _dismiss(page)
        return _result(blocker or STATUS_NO_FORM, reason=reason)
    if not _is_enabled(submit):
        # Кнопка заблокирована (например, резюме скрыто): жать её бессмысленно,
        # а причину hh.ru пишет информационным сообщением в самой форме.
        blocker = detect_response_blocker(scope)
        reason = response_notice(page)
        _dismiss(page)
        return _result(blocker or STATUS_NO_FORM, reason=reason)

    try:
        submit.click()
    except PlaywrightError:
        return _result(STATUS_UNKNOWN, page)

    result = _wait_for_response_result(page, captcha_waiter)

    if result == STATUS_UNKNOWN and not _modal_hidden(page):
        # Форма осталась открытой — клик не сработал, пробуем ещё раз.
        retry = _find_action(scope, SUBMIT_SELECTORS, RESPOND_RE)
        if retry is not None and _is_enabled(retry):
            try:
                retry.click()
            except PlaywrightError:
                pass
            else:
                result = _wait_for_response_result(page, captcha_waiter)

    reason = response_notice(page) if result != STATUS_SENT else ""
    if result in ("test", "questionnaire", "external_form"):
        _dismiss(page)
    return _result(result, reason=reason)


def _result(status: str, page: Page | None = None, *, reason: str = "") -> ResponseResult:
    """Собирает итог отклика: статус и причину со страницы, если она есть."""
    if not reason and page is not None:
        reason = response_notice(page)
    return ResponseResult(status=status, reason=reason)


def response_notice(page: Page) -> str:
    """Возвращает информационное сообщение формы отклика — реальную причину.

    Сначала смотрим явные подсказки/уведомления, затем абзацы внутри модалки.
    Текст описания вакансии и подписи интерфейса не попадают: нужен только
    текст, которым hh.ru объясняет, почему отклик недоступен или не ушёл.
    """
    scope = _response_scope(page)
    if _modal_hidden(page):
        raw = _first_text(page, PAGE_NOTICE_SELECTORS)
        return _clean_notice(raw) if len(raw) <= NOTICE_MAX_LENGTH else ""
    raw = _first_text(scope, NOTICE_SELECTORS)
    if raw and len(raw) <= NOTICE_MAX_LENGTH and _looks_like_notice(_clean_notice(raw)):
        return _clean_notice(raw)
    return _notice_from_text_blocks(scope)


def _notice_from_text_blocks(scope: Locator | Page) -> str:
    """Ищет абзац-объяснение в модалке, пропуская кнопки, подписи и заголовки."""
    for selector in ("p", "div", "span"):
        try:
            items = scope.locator(selector)
            total = min(items.count(), 50)
        except PlaywrightError:
            continue
        for index in range(total):
            try:
                item = items.nth(index)
                if not item.is_visible() or _inside_ui(item):
                    continue
                raw = item.inner_text() if selector == "p" else _direct_text(item)
                raw = " ".join(raw.split())
            except PlaywrightError:
                continue
            # Длинный блок — это обёртка со всем содержимым формы, а не причина.
            if len(raw) > NOTICE_MAX_LENGTH:
                continue
            text = _clean_notice(raw)
            if not _looks_like_notice(text):
                continue
            if selector != "p" and not CAUSE_TEXT_RE.search(text):
                continue
            return text
    return ""


def _inside_ui(locator: Locator) -> bool:
    """Элемент — часть интерфейса формы, а не текст с причиной."""
    try:
        return bool(locator.evaluate(f"el => !!el.closest({UI_CLOSEST_SELECTOR!r})"))
    except PlaywrightError:
        return False


def _direct_text(locator: Locator) -> str:
    """Текст самого элемента без вложенных блоков."""
    try:
        text = locator.evaluate(DIRECT_TEXT_SCRIPT)
    except PlaywrightError:
        return ""
    return text if isinstance(text, str) else ""


def _clean_notice(text: str) -> str:
    """Приводит сообщение к одной строке пригодной длины."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) > NOTICE_MAX_LENGTH:
        cleaned = cleaned[:NOTICE_MAX_LENGTH].rstrip() + "…"
    return cleaned


def _looks_like_notice(text: str) -> bool:
    """Похоже ли сообщение на объяснение причины, а не на подпись формы."""
    if len(text) < NOTICE_MIN_LENGTH or UI_TEXT_RE.match(text):
        return False
    if CONTAINER_TEXT_RE.search(text):
        return False
    return any(char.isalpha() for char in text)


def _solve_captcha(captcha_waiter: Callable[[], bool] | None) -> bool:
    """Ждёт ручного решения капчи; без обработчика ждать некому."""
    return bool(captcha_waiter and captcha_waiter())


def _is_enabled(locator: Locator) -> bool:
    try:
        return locator.is_enabled()
    except PlaywrightError:
        return False


def _wait_for_response_ui(
    page: Page,
    captcha_waiter: Callable[[], bool] | None = None,
    seconds: int = RESPONSE_UI_WAIT_SECONDS,
) -> str:
    """Ждёт форму отклика. Возвращает 'ready', статус или причину блокировки."""
    deadline = time.monotonic() + seconds
    while True:
        page.wait_for_timeout(500)
        if detect_captcha(page):
            if not _solve_captcha(captcha_waiter):
                return STATUS_CAPTCHA
            # После ручного шага отсчитываем время ожидания заново.
            deadline = time.monotonic() + seconds
            continue

        scope = _response_scope(page)
        blocker = detect_response_blocker(scope)
        if blocker:
            return blocker
        if _has_response_form(page, scope):
            return "ready"
        if time.monotonic() >= deadline:
            return "none"


def _wait_for_response_result(
    page: Page,
    captcha_waiter: Callable[[], bool] | None = None,
    seconds: int = RESPONSE_RESULT_WAIT_SECONDS,
) -> str:
    """Ждёт результат отправки. Возвращает sent / причину блокировки / unknown."""
    deadline = time.monotonic() + seconds
    while True:
        page.wait_for_timeout(1000)
        if detect_captcha(page):
            if not _solve_captcha(captcha_waiter):
                return STATUS_CAPTCHA
            deadline = time.monotonic() + seconds
            continue

        scope = _response_scope(page)
        # Сначала проверяем явный успех: заголовки описания вакансии не должны
        # давать ложное срабатывание на словах «тест»/«анкета». Старый текст об
        # отклике другого резюме подтверждением не считается, пока открыта форма:
        # ждём её закрытия до конца таймаута, hh.ru отвечает медленно.
        stale = _has_other_resume_action(page) and not _modal_hidden(page)
        if _has_success(page) and not stale:
            return STATUS_SENT
        blocker = detect_response_blocker(scope)
        if blocker:
            return blocker
        if time.monotonic() >= deadline:
            return STATUS_UNKNOWN


def _has_response_form(page: Page, scope: Locator | Page) -> bool:
    """Появилась ли форма отклика (модалка, страница отклика, textarea)."""
    if not _modal_hidden(page):
        return True
    if "vacancy_response" in page.url.lower():
        return True
    if _first_visible(scope, LETTER_TEXTAREA_SELECTORS) is not None:
        return True
    return _find_action(scope, LETTER_TOGGLE_SELECTORS, LETTER_TOGGLE_RE) is not None


def _fill_letter(page: Page, scope: Locator | Page, letter: str) -> bool:
    textarea = _first_visible(scope, LETTER_TEXTAREA_SELECTORS)
    if textarea is None:
        toggle = _find_action(scope, LETTER_TOGGLE_SELECTORS, LETTER_TOGGLE_RE)
        if toggle is not None:
            try:
                toggle.click()
                page.wait_for_timeout(1000)
            except PlaywrightError:
                pass
            textarea = _first_visible(scope, LETTER_TEXTAREA_SELECTORS)
    if textarea is None:
        return False
    try:
        textarea.click()
        textarea.fill(letter)
        textarea.dispatch_event("input")
        textarea.dispatch_event("change")
        return True
    except PlaywrightError:
        return False


def _response_scope(page: Page) -> Locator | Page:
    for selector in MODAL_SELECTORS:
        locator = page.locator(selector).first
        if _is_visible(locator):
            return locator
    return page


def _modal_hidden(page: Page) -> bool:
    return not any(_is_visible(page.locator(selector).first) for selector in MODAL_SELECTORS)


def _dismiss(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
    except PlaywrightError:
        pass
    if _modal_hidden(page):
        return
    close = page.locator("[data-qa='modal-close']").first
    if _is_visible(close):
        try:
            close.click()
        except PlaywrightError:
            pass


def _has_success(page: Page) -> bool:
    """Есть ли на странице явное подтверждение отправки отклика.

    Проверяем все совпадения, а не только первое: hh.ru держит в DOM скрытые
    заготовки с тем же текстом, и первый узел может быть невидимым — именно
    из-за этого отправленный отклик раньше считался «неизвестным».
    """
    for text in SUCCESS_TEXTS:
        try:
            matches = page.get_by_text(text, exact=False)
            total = min(matches.count(), 10)
        except PlaywrightError:
            continue
        for index in range(total):
            if _is_visible(matches.nth(index)):
                return True
    return False


def response_state(page: Page) -> dict[str, object]:
    """Состояние формы отклика для лога: адрес, текст формы, длина письма.

    Сопроводительное письмо в лог не пишется — только его длина. Если модалки
    нет, текст страницы не собираем: это шум, а не диагностика.
    """
    state: dict[str, object] = {"page_url": page.url}
    if _modal_hidden(page):
        return state

    scope = _response_scope(page)
    form_text = _response_text(scope)
    if form_text:
        state["form_text"] = form_text[:400]
    textarea = _first_visible(scope, LETTER_TEXTAREA_SELECTORS)
    if textarea is not None:
        try:
            state["letter_length"] = len(textarea.input_value() or "")
        except PlaywrightError:
            pass
    return state


def _is_already_responded(page: Page) -> bool:
    """Отклик уже отправлен именно текущим резюме.

    Кнопка «Отклик другим резюме» означает отклик другого профиля: такую
    вакансию агент пропускает со статусом `skipped` (состояние страницы
    `other_resume`), а не отправляет отклик текущим резюме.
    """
    if _has_other_resume_action(page):
        return False
    return _has_success(page)


def _has_other_resume_action(page: Page) -> bool:
    """Видна ли кнопка «Отклик другим резюме».

    Кнопка появляется, когда отклик уже отправлен другим резюме. Текст
    проверяется отдельно: data-qa на hh.ru меняется, подпись стабильнее.
    """
    for selector in OTHER_RESUME_SELECTORS:
        try:
            items = page.locator(selector)
            total = min(items.count(), 5)
        except (PlaywrightError, AttributeError):
            continue
        for index in range(total):
            item = items.nth(index)
            if not _is_visible(item):
                continue
            try:
                text = " ".join((item.inner_text() or "").split())
            except (PlaywrightError, AttributeError):
                continue
            if OTHER_RESUME_RE.match(text):
                return True
    for role in ("button", "link"):
        try:
            items = page.get_by_role(role, name=OTHER_RESUME_RE)
            total = min(items.count(), 10)
        except (PlaywrightError, AttributeError):
            continue
        for index in range(total):
            if _is_visible(items.nth(index)):
                return True
    return False


def _find_action(
    scope: Locator | Page,
    selectors: Sequence[str],
    name_pattern: re.Pattern[str],
) -> Locator | None:
    for selector in selectors:
        locator = scope.locator(selector).first
        if _is_visible(locator):
            return locator
    for role in ("button", "link"):
        try:
            locator = scope.get_by_role(role, name=name_pattern).first
        except PlaywrightError:
            continue
        if _is_visible(locator):
            return locator
    return None


def _first_visible(scope: Locator | Page, selectors: Sequence[str]) -> Locator | None:
    """Первый видимый элемент по селекторам.

    Перебираем все совпадения: hh.ru держит перед нужным элементом скрытые
    заготовки (например, `hidden-resume-warning`), и `.first` давал промах.
    """
    for selector in selectors:
        locator = scope.locator(selector)
        try:
            total = min(locator.count(), VISIBLE_SCAN_LIMIT)
        except (PlaywrightError, AttributeError):
            continue
        for index in range(total):
            try:
                item = locator.nth(index)
            except (PlaywrightError, AttributeError):
                break
            if _is_visible(item):
                return item
    return None


def _first_text(scope: Locator | Page, selectors: Sequence[str]) -> str:
    locator = _first_visible(scope, selectors)
    if locator is None:
        return ""
    try:
        return locator.inner_text().strip()
    except PlaywrightError:
        return ""


def _has_exact_text(scope: Locator | Page, text: str) -> bool:
    """Есть ли видимый элемент с точно таким текстом (без учёта соседних)."""
    try:
        locator = scope.get_by_text(text, exact=True)
        count = min(locator.count(), 10)
    except PlaywrightError:
        return False
    for index in range(count):
        if _is_visible(locator.nth(index)):
            return True
    return False


def _has_text_outside_description(page: Page, text: str) -> bool:
    """Есть ли видимая фраза вне описания вакансии.

    Описание может содержать «Вам отказали» или «Вас пригласили» как часть
    текста вакансии, поэтому его не учитываем: состояние ищем только в
    интерфейсе страницы.
    """
    try:
        locator = page.get_by_text(text)
        count = min(locator.count(), 10)
    except PlaywrightError:
        return False
    for index in range(count):
        item = locator.nth(index)
        if not _is_visible(item):
            continue
        if _inside_description(item):
            continue
        return True
    return False


def _has_text(scope: Locator | Page, text: str) -> bool:
    """Есть ли видимый элемент, в тексте которого встречается эта фраза.

    hh.ru может показать заголовок вместе с пояснением в одном блоке, поэтому
    экран «Вам недоступна эта вакансия» ищем по вхождению, а не по равенству.
    """
    try:
        locator = scope.get_by_text(text)
        count = min(locator.count(), 10)
    except PlaywrightError:
        return False
    for index in range(count):
        if _is_visible(locator.nth(index)):
            return True
    return False


def _is_visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.is_visible()
    except (PlaywrightError, AttributeError):
        return False


def _has_selector(scope: Locator | Page, selectors: Sequence[str]) -> bool:
    for selector in selectors:
        try:
            if scope.locator(selector).count() > 0:
                return True
        except PlaywrightError:
            continue
    return False


def _has_role_name(scope: Locator | Page, name_pattern: re.Pattern[str]) -> bool:
    for role in ("button", "link"):
        try:
            if scope.get_by_role(role, name=name_pattern).count() > 0:
                return True
        except PlaywrightError:
            continue
    return False


def _has_external_link(scope: Locator | Page) -> bool:
    try:
        links = scope.locator("a[href^='http']")
        total = min(links.count(), 50)
    except PlaywrightError:
        return False
    for index in range(total):
        try:
            href = links.nth(index).get_attribute("href") or ""
        except PlaywrightError:
            continue
        host = urlparse(href).netloc.lower()
        if host and "hh.ru" not in host and "headhunter" not in host:
            return True
    return False
