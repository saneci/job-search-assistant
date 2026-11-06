"""Основной цикл работы агента (раздел 3.4 требований)."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, Page

from . import habr_search
from .browser import (
    HH_HOME,
    BrowserError,
    BrowserSession,
    CaptchaError,
    PageLoadError,
    save_screenshot,
    wait_for_captcha_solved,
)
from .channel import HABR, HH, Channel, QueryLabels, channel_name_for_url, habr_channel
from .config import FIT_LABEL, NOT_FIT_LABEL, AppConfig
from .hh_search import (
    is_company_excluded,
    is_excluded,
    open_search_page,
    pages_plan,
    profile_key,
    vacancy_id,
    vacancy_keys,
)
from .hh_vacancy import (
    ResumeNotFoundError,
    STATUS_ALREADY_RESPONDED,
    STATUS_CAPTCHA,
    STATUS_NO_FORM,
    STATUS_REJECTED,
    STATUS_SENT,
    STATUS_UNKNOWN,
    TEST_FAILED,
    TEST_PASSED,
    ResponseForm,
    VacancyParseError,
    open_response_form,
    open_vacancy,
    respond,
    response_state,
    set_resume_name,
    start_test,
    submit_response,
    submit_test,
)
from .llm import LLMClient, LLMError, meets_threshold
from .logging_setup import RunLogger, prune_logs
from .models import (
    ALREADY_RESPONDED_REASON,
    ARCHIVE_REASON,
    ERROR_REPORT_REASON,
    INVITE_REASON,
    INVITED_OTHER_RESUME_REASON,
    NOT_FIT_REPORT_REASON,
    OFFER_REASON,
    OTHER_RESUME_REASON,
    REJECTED_REASON,
    SENT_REPORT_REASON,
    UNAVAILABLE_REASON,
    STATUS_ERROR as OUTCOME_ERROR,
    STATUS_EXCLUDED as OUTCOME_EXCLUDED,
    STATUS_INVITE,
    STATUS_OFFER,
    STATUS_QUEUED as OUTCOME_QUEUED,
    STATUS_REJECTED as OUTCOME_REJECTED,
    STATUS_SENT as OUTCOME_SENT,
    STATUS_SKIPPED as OUTCOME_SKIPPED,
    RunError,
    SentVacancy,
    SkippedVacancy,
    VacancyCard,
    VacancyDetails,
)
from .notify import notify_captcha
from .prefilter import (
    NO_QUESTION_LABEL,
    VERDICT_MANUAL,
    VERDICT_REJECT,
    Answer,
    PrefilterResult,
    Question,
    evaluate,
    llm_answers,
    manual_hint,
    merge_answers,
    question_full_label,
    question_lines,
    question_label,
    unanswered_questions,
    without_rejects,
)
from .profile import Profile, load_profile
from .prompts import requires_project_links
from .processed import ProcessedError, ProcessedStore
from . import questionary as questionary_store
from .report import RunStats, build_report, send_email
from .resume import boost_resume


class SessionLostError(RuntimeError):
    """Сессия сайта перестала быть действительной во время работы."""


class ResumeNameError(RuntimeError):
    """Название резюме не настроено: отклик на hh.ru невозможен."""


# `--retry` без значения: статусы для повтора берём из retry.statuses.
RETRY_ALL = "all"

# Канал отчёта для вакансий, которые требуют отклика на сайте компании.
EXTERNAL_CHANNEL = "external"

# Вакансии, на которые нельзя откликнуться автоматически: что писать в отчёте за день.
QUEUE_REASON_TEXTS: dict[str, str] = {
    "empty_cover_letter": "Нужно заполнить сопроводительное письмо вручную",
    "project_links": "В вакансии требуется ссылка на проекты в письме",

    "test": "Для отклика на вакансию необходимо пройти тестирование",
    "questionnaire": "Для отклика на вакансию необходимо заполнить анкету",
    "prefilter_auto_fill_disabled": "Анкета работодателя заполнена автоматически — отклик за человеком",
    "external_form": "Отклик нужно отправить на сайте работодателя",
    "captcha": "hh.ru просит пройти капчу: откликнитесь вручную",
    "no_form": "hh.ru не показал форму отклика — проверьте вакансию вручную",
    "unknown": "hh.ru не подтвердил отправку отклика — проверьте вакансию вручную",
    "other_resume": "Уже отправлен отклик по другому резюме",
    "interrupted": "Запуск прерван во время обработки вакансии, проверьте вакансию вручную",
}

# Короткая причина для списка: подробности с формулировкой вопроса — в поле `message`.
QUESTIONARY_MANUAL_REASON = (
    "В анкете есть субъективные вопросы от работодателя, на которые требуется ручной ответ"
)

# Вводная часть обоснования Generation: вердикт и общие слова о совпадении.
# В отчёт идёт только причина, без этой «шапки».
_VERDICT_PREFIX_RE = re.compile(
    r"^\s*(?:вердикт\s*[:—–-]\s*)?(?:не\s+подходит|подходит)\s*[.:;—–-]?\s*",
    re.IGNORECASE,
)
_CONTRAST_RE = re.compile(
    r"(?:^|[.!?]\s+)\s*(?:но|однако|при этом|тем не менее|вместе с тем)\b"
    r"\s*[,:—–-]?\s*",
    re.IGNORECASE,
)
_INLINE_CONTRAST_RE = re.compile(r",\s*(?:но|однако)\s+", re.IGNORECASE)
_POSITIVE_FIRST_RE = re.compile(
    r"профил\w*\s+(?:хорошо\s+)?(?:совпадает|соответствует|подходит)|"
    r"ваканси\w*\s+(?:хорошо\s+)?(?:подходит|соответствует)|"
    r"опыт\w*\s+(?:хорошо\s+)?совпадает",
    re.IGNORECASE,
)
_TRAILING_VERDICT_RE = re.compile(
    r"\s*(?:поэтому\s+)?вердикт\s*[:—–-]?\s*не\s+подходит\.?\s*$",
    re.IGNORECASE,
)
MIN_MEANINGFUL_REASON = 12


_SENTENCE_END_RE = re.compile(r"^(.+?[.!?])(?:\s|$)")


def _first_sentence(text: str) -> str:
    """Первое предложение без завершающей точки: короткая причина отчёта."""
    compact = " ".join((text or "").split())
    match = _SENTENCE_END_RE.match(compact)
    sentence = match.group(1) if match else compact
    return sentence.rstrip(".!?").strip()


def _generation_reason_text(reason: str) -> str:
    """Оставляет от обоснования Generation саму причину, без вводного вердикта.

    Модель часто пишет «по Java профиль совпадает. Но для задачи нужен опыт X»:
    в отчёт нужна вторая часть — по ней видно, что именно не сошлось.
    Короткие технические ответы вроде «ok» причиной не считаются.
    """
    text = " ".join((reason or "").split())
    text = _TRAILING_VERDICT_RE.sub("", text)
    text = _VERDICT_PREFIX_RE.sub("", text)
    match = _CONTRAST_RE.search(text) or _INLINE_CONTRAST_RE.search(text)
    if match:
        text = text[match.end():]
    else:
        sentences = [part for part in re.split(r"(?<=[.!?])\s+", text) if part]
        if len(sentences) > 1 and _POSITIVE_FIRST_RE.search(sentences[0]):
            text = " ".join(sentences[1:])
    text = text.strip()
    if text:
        text = text.lstrip(",;:—- ") 
    if len(text) < MIN_MEANINGFUL_REASON:
        return ""
    return text[0].upper() + text[1:]


_PROJECT_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def _letter_has_project_link(letter: str, links: tuple[str, ...]) -> bool:
    """Есть ли в письме ссылка на проект: URL или ссылка из конфига."""
    if _PROJECT_URL_RE.search(letter or ""):
        return True
    text = (letter or "").lower().replace(" ", "")
    for link in links:
        normalized = link.strip().lower().rstrip("/")
        if not normalized:
            continue
        if normalized in text or re.sub(r"^https?://", "", normalized) in text:
            return True
    return False



# Состояния страницы вакансии, при которых отклик не нужен: код → (статус
# записи, причина в отчёте за день). Приглашение и недоступная вакансия —
# `skipped`, отказ работодателя — отдельный статус `rejected`. Отклик другого
# резюме сюда не входит: сначала проверяем, подходит ли вакансия, и только
# подходящую откладываем в очередь.
PAGE_STATUS_REASONS: dict[str, tuple[str, str]] = {
    "archived": (OUTCOME_SKIPPED, ARCHIVE_REASON),
    "invited": (OUTCOME_SKIPPED, INVITED_OTHER_RESUME_REASON),
    "blocked": (OUTCOME_SKIPPED, "У вас нет доступа к этой вакансии"),
    "unavailable": (OUTCOME_SKIPPED, UNAVAILABLE_REASON),
    "rejected": (OUTCOME_REJECTED, REJECTED_REASON),
}


class _VacancyLog:
    """Строки журнала по вакансии: «Название — Компания: событие».

    В JSONL номер вакансии идёт отдельным полем `vacancy_id` и не дублируется
    в тексте сообщения: его видно в поле, а не в каждой строке.
    """

    def __init__(
        self,
        logger: RunLogger,
        title: str,
        company: str = "",
        url: str = "",
    ) -> None:
        self._logger = logger
        self.title = title
        self.company = company
        self.label = f"{title} — {company}" if company else title
        self.id = vacancy_id(url) or ""

    def info(self, message: str) -> None:
        """Пишет сообщение в JSONL без номера, а в консоль — с номером вакансии."""
        extra = {"vacancy_id": self.id} if self.id else {}
        text = f"{self.label}: {message}"
        console = f"[{self.id}] {text}" if self.id else text
        self._logger.info(text, console_message=console, vacancy=self.label, **extra)

    def console(self, message: str) -> None:
        """Печатает подробность в консоль, не повторяя её в JSONL.

        Нужно для текстов, которые уже есть в отдельном событии (полный вердикт
        LLM): журнал не раздувается, а человек видит ту же строку.
        """
        text = f"{self.label}: {message}"
        console = f"[{self.id}] {text}" if self.id else text
        self._logger.console(console, vacancy_id=self.id)

    def mark_llm(self) -> None:
        """Включает вывод вакансии, которая дошла до вызова LLM.

        Накопленные до этого строки печатаются, а следующие выводятся сразу:
        без `--verbose` консоль показывает только вакансии, обработанные моделью.
        """
        activate = getattr(self._logger, "activate_vacancy", None)
        if activate is not None:
            activate(self.id)

    def event(self, name: str, **fields: object) -> None:
        extra = {"vacancy_id": self.id} if self.id else {}
        self._logger.event(name, vacancy=self.label, **extra, **fields)


def run(
    config: AppConfig,
    logger: RunLogger,
    retry: str | None = None,
    channel_name: str | None = None,
) -> tuple[RunStats, Path]:
    """Выполняет полный цикл: поиск (или повтор по отчётам), оценка, отклики и отчёт."""
    stats = RunStats(started_at=datetime.now(), run_id=logger.run_id)
    statuses = _retry_statuses(config, retry) if retry else ()
    profile = load_profile(config.data.paths.profile)

    config = with_channel(config, channel_name)
    channel = channel_for(config)
    processed = _open_report(config, logger, _profile_name(config, channel, profile))
    log_channel_choice(logger, channel, channel_name)
    _require_resume_name(config, logger, channel)
    state_path, profile_dir = session_settings(config, channel)

    with BrowserSession(config.browser, state_path, logger, profile_dir) as session:
        session.ensure_logged_in(channel.auth_check, channel.login_url, channel.site_label)
        page = session.new_page()
        session.close_blank_pages(page)
        try:
            if channel.supports_resume_boost:
                boost_resume(page, config, logger)
            with LLMClient(config.llm) as llm:
                if statuses:
                    _retry_vacancies(
                        page, config, profile, llm, stats, logger, processed, statuses, channel
                    )
                else:
                    _process_vacancies(
                        page, config, profile, llm, stats, logger, processed, channel
                    )
            if channel.name == HABR:
                _sync_habr_responses(
                    page, processed, logger, _profile_name(config, channel, profile)
                )
        except (ResumeNameError, ResumeNotFoundError):
            # Отклик без выбранного резюме невозможен: запуск завершается ошибкой.
            raise
        except KeyboardInterrupt:
            stats.stop_reason = "остановлено пользователем (Ctrl+C)"
            logger.event("interrupted")
            logger.info("Получена команда остановки, аккуратно завершаю работу...")
        except SessionLostError as exc:
            stats.stop_reason = str(exc)
            logger.event("session_lost", error=str(exc))
        except PlaywrightError as exc:
            stats.stop_reason = f"ошибка браузера: {exc}"
            logger.event("browser_error", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - отчёт и сессию сохраняем в любом случае
            stats.stop_reason = f"непредвиденная ошибка: {type(exc).__name__}: {exc}"
            logger.event("unexpected_error", error=f"{type(exc).__name__}: {exc}")
            logger.info(f"Непредвиденная ошибка: {type(exc).__name__}: {exc}")
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                session.save_state()
            except BrowserError:
                pass

    stats.finished_at = datetime.now()
    report_path = processed.path if processed.path.exists() else None
    stats.report_sent = processed.today_summary().get(OUTCOME_SENT, 0)

    vacancy_title, resume_name = _report_context(config, channel, stats)
    profile_name = _profile_name(config, channel, profile)
    report = build_report(
        stats,
        report_path,
        vacancy_title,
        resume_name,
        channel.site_label,
        profile_name,
    )
    logger.info("")
    logger.info_plain(
        build_report(
            stats,
            None,
            vacancy_title,
            resume_name,
            channel.site_label,
            profile_name,
        )
    )
    if report_path is not None:
        logger.info(f"Отчёт сохранён в файле: {report_path}")

    email_sent, email_message = _send_report_email(config, report, stats)
    logger.event("report_email", sent=email_sent, message=email_message)
    if not email_sent:
        logger.info(f"Email-отчёт не отправлен: {email_message}")

    logger.info(_final_message(stats, config))

    return stats, processed.path


def _require_resume_name(config: AppConfig, logger: RunLogger, channel: Channel) -> None:
    """Отклик на hh.ru невозможен без названия резюме: останавливаем запуск."""
    if channel.name == HH and not config.pipeline.resume.name.strip():
        logger.event("resume_name_is_empty", channel=channel.name)
        raise ResumeNameError(
            "не задано название резюме: заполните pipeline.resume.name в config.local.yaml"
        )


def _open_report(
    config: AppConfig, logger: RunLogger, legacy_profile: str = ""
) -> ProcessedStore:
    """Готовит отчёты: чистит старые журналы и читает историю вакансий.

    Дневные отчёты (`data/report`) не удаляем никогда: это история вакансий
    для аналитики. Срок хранения есть только у журналов и скриншотов.
    """
    removed = prune_logs(
        config.data.paths.logs_dir,
        config.data.logs_retention_days,
        keep=[logger.events_path],
    )
    if removed:
        logger.event(
            "logs_pruned",
            count=len(removed),
            days=config.data.logs_retention_days,
            files=removed,
        )
        logger.info(
            f"Удалил старые журналы: {len(removed)} "
            f"{_plural(len(removed), 'файл', 'файла', 'файлов')} "
            f"старше {config.data.logs_retention_days} дней."
        )
    processed = ProcessedStore(
        config.data.paths.report_dir,
        run_id=logger.run_id,
        legacy_profile=legacy_profile,
    )
    known = processed.load()
    logger.event("processed_loaded", count=known, path=str(processed.directory))
    return processed


def _retry_statuses(config: AppConfig, retry: str) -> tuple[str, ...]:
    """Статусы для повтора: набор из retry.statuses или явно указанный в CLI."""
    if retry == RETRY_ALL:
        return config.pipeline.retry.statuses
    return (retry,)


def _hh_channel(config: AppConfig) -> Channel:
    """hh.ru: обёртки над функциями модуля, чтобы тесты могли их подменять."""
    search = config.search
    # Резюме выбирается внутри формы отклика: канал передаёт имя один раз.
    set_resume_name(config.pipeline.resume.name)

    def labels(query: str) -> QueryLabels:
        return QueryLabels(
            start=f"по запросу «{query}»",
            subject=f"По запросу «{query}»",
            source=f"из результата запроса «{query}»",
            title=f"Запрос «{query}»",
        )

    return Channel(
        name=HH,
        site_label="hh.ru",
        login_url=HH_HOME,
        uses_persistent_profile=False,
        supports_resume_boost=True,
        per_page=search.per_page,
        queries=lambda: search.queries,
        query_label=lambda query: f"по запросу «{query}»",
        labels=labels,
        open_search_page=lambda page, query, index: open_search_page(
            page, search, query, index
        ),
        pages_plan=lambda total: pages_plan(total, search.per_page),
        vacancy_id=vacancy_id,
        open_vacancy=lambda page, card: open_vacancy(page, card),
        # pipeline вызывает отклик по имени: `captcha_waiter=...`. Имя параметра
        # в обёртке должно совпадать, иначе TypeError вместо ожидания капчи.
        respond=lambda page, letter, captcha_waiter: respond(
            page, letter, captcha_waiter=captcha_waiter
        ),
        response_state=lambda page: response_state(page),
        queue_reasons=QUEUE_REASON_TEXTS,
    )


def _channel_for(config: AppConfig, name: str | None = None) -> Channel:
    """Канал запуска: из опции CLI или из конфига (hh по умолчанию)."""
    selected = name or config.search.channel
    if selected == HABR:
        return habr_channel(config)
    return _hh_channel(config)


def channel_for(config: AppConfig, name: str | None = None) -> Channel:
    """Публичный выбор канала: CLI использует его для входа и запуска."""
    return _channel_for(config, name)


def log_channel_choice(logger: RunLogger, channel: Channel, name: str | None) -> None:
    """Пишет в лог, какой канал выбран и откуда: опция CLI или конфиг."""
    source = "flag" if name else "config"
    logger.event("channel_selected", channel=channel.name, source=source)
    logger.info(f"Канал запуска: {channel.site_label}")


def with_channel(config: AppConfig, name: str | None) -> AppConfig:
    """Конфиг с выбранным каналом: опция CLI сильнее `search.channel`.

    Модели заморожены, поэтому возвращается копия. Подменять канал в конфиге
    дешевле, чем протаскивать его через все шаги: выдача, страница вакансии
    и тексты причин уже читают канал из одного места.
    """
    if not name or name == config.search.channel:
        return config
    return config.model_copy(
        update={"search": config.search.model_copy(update={"channel": name})}
    )


def with_resume(config: AppConfig, name: str) -> AppConfig:
    """Конфиг с другим названием резюме: повтор идёт тем же резюме.

    Модели заморожены, поэтому возвращается копия. Имя резюме читают поиск,
    отклик и запись в отчёт — достаточно подменить его в одном месте.
    """
    if not name or name == config.pipeline.resume.name:
        return config
    resume = config.pipeline.resume.model_copy(update={"name": name})
    return config.model_copy(
        update={"pipeline": config.pipeline.model_copy(update={"resume": resume})}
    )


def _profile_name(
    config: AppConfig, channel: Channel, profile: Profile | None = None
) -> str:
    """Профиль для записи отчёта: у hh — резюме, у Habr — имя из `# PROFILE:`."""
    if channel.name == HH:
        return config.pipeline.resume.name
    return profile.name if profile is not None else ""


def session_settings(config: AppConfig, channel: Channel) -> tuple[Path, Path | None]:
    """Файл сессии и профиль браузера выбранного канала."""
    state_path = (
        config.data.paths.habr_session_state
        if channel.name == HABR
        else config.data.paths.session_state
    )
    profile_dir = config.data.paths.habr_profile if channel.uses_persistent_profile else None
    return state_path, profile_dir


def _expected_cards(channel: Channel, total: int | None, page_index: int) -> int:
    """Сколько карточек ждём на странице: размер страницы, на последней — остаток."""
    if not total:
        return channel.per_page
    rest = total - page_index * channel.per_page
    return max(0, min(channel.per_page, rest))


def _process_vacancies(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    stats: RunStats,
    logger: RunLogger,
    processed: ProcessedStore,
    channel: Channel | None = None,
) -> None:
    channel = channel or _channel_for(config)
    search = config.search
    profile_name = _profile_name(config, channel, profile)
    seen: set[str] = set()
    reviewed = 0

    for number, query in enumerate(channel.queries()):
        if _should_stop(config.pipeline.limit_per_run, stats, logger):
            break
        _check_new_day(logger, processed)
        if number and channel.supports_resume_boost:
            # Новый запрос: резюме поднимаем только если по кэшу уже пора.
            boost_resume(page, config, logger)

        labels = channel.labels(query)
        # Глубина: pages_limit из конфига, а если он не задан — считаем страницы
        # по числу найденных после открытия первой страницы этого запроса.
        logger.info(f"Начинаю поиск на {channel.site_label} {labels.start}...")
        pages = search.pages_limit
        page_index = 0
        while pages is None or page_index < pages:
            if _should_stop(config.pipeline.limit_per_run, stats, logger):
                break

            try:
                cards, total = channel.open_search_page(page, query, page_index)
            except CaptchaError as exc:
                stats.stop_reason = f"капча на странице поиска ({labels.start}, стр. {page_index + 1})"
                logger.event("captcha", query=query, page=page_index, url=page.url, error=str(exc))
                logger.info(
                    f"{channel.site_label} показал капчу на странице поиска, запуск остановлен."
                )
                _notify_captcha(
                    config,
                    logger,
                    note=(
                        f"Капча на странице поиска ({labels.start}, стр. {page_index + 1}). "
                        "Запуск остановлен — пройдите капчу вручную."
                    ),
                    url=page.url,
                    site=channel.site_label,
                )
                break
            except PageLoadError as exc:
                logger.event("search_page_error", query=query, page=page_index, error=str(exc))
                logger.info(f"Не удалось открыть поиск ({labels.start}): {exc}")
                break

            if not cards:
                logger.info(
                    f"Обработка результатов {labels.source}: "
                    f"на странице {page_index + 1} вакансий нет"
                )
                break

            if pages is None:
                pages = channel.pages_plan(total)
                logger.event(
                    "search_pages_plan",
                    query=query,
                    total=total,
                    per_page=channel.per_page,
                    pages=pages,
                )
                if total:
                    logger.info(
                        f"{labels.subject} найдено {total} "
                        f"{_plural(total, 'вакансию', 'вакансии', 'вакансий')}, "
                        f"разделяю обработку на {pages} "
                        f"{_plural(pages, 'страница', 'страницы', 'страниц')}"
                    )
                else:
                    logger.info(
                        f"{labels.title}: число найденных неизвестно, иду до первой "
                        f"пустой страницы (не больше {pages})"
                    )

            logger.info(
                f"Обработка {len(cards)} "
                f"{_plural(len(cards), 'вакансия', 'вакансии', 'вакансий')} "
                f"{labels.source}: страница {page_index + 1} из {pages}"
            )

            # hh.ru отдаёт карточки лениво: если догрузка не удалась, часть
            # вакансий страницы молча терялась бы — пишем это в лог.
            expected = _expected_cards(channel, total, page_index)
            if cards and len(cards) < expected:
                logger.event(
                    "search_page_short",
                    query=query,
                    page=page_index,
                    cards=len(cards),
                    expected=expected,
                )
                logger.info(
                    f"{channel.site_label} отдал {len(cards)} "
                    f"{_plural(len(cards), 'карточку', 'карточки', 'карточек')} "
                    f"вместо {expected}: часть вакансий страницы не догрузилась"
                )

            for card in cards:
                if _should_stop(config.pipeline.limit_per_run, stats, logger):
                    break
                _check_new_day(logger, processed)
                keys = vacancy_keys(card.title, card.company, card.url, profile_name)
                if any(key in seen for key in keys):
                    logger.event(
                        "skipped_duplicate",
                        url=card.url,
                        title=card.title,
                        company=card.company,
                    )
                    _VacancyLog(logger, card.title, card.company, card.url).info(
                        "дубликат в выдаче, пропускаю"
                    )
                    continue
                seen.update(keys)

                stats.reviewed += 1
                previous = processed.find(
                    card.title,
                    card.company,
                    card.url,
                    profile_name,
                )
                if previous:
                    logger.event(
                        "skipped_processed",
                        url=card.url,
                        title=card.title,
                        seen_on=previous,
                    )
                    _VacancyLog(logger, card.title, card.company, card.url).info(
                        f"уже обрабатывалась {previous}, пропускаю"
                    )
                    continue

                if card.already_responded:
                    # Отклик уже есть: карточку не открываем. Для новой
                    # вакансии запись создаётся один раз.
                    if _remember_card_already_responded(
                        processed, logger, card, profile_name, channel
                    ):
                        stats.already_sent.append(
                            SkippedVacancy(
                                title=card.title,
                                company=card.company,
                                url=card.url,
                                reason=ALREADY_RESPONDED_REASON,
                            )
                        )
                    continue

                reviewed += 1
                if reviewed > config.pipeline.max_vacancies:
                    stats.stop_reason = (
                        f"просмотрено максимум вакансий за запуск ({config.pipeline.max_vacancies})"
                    )
                    logger.info(stats.stop_reason)
                    break

                _process_and_remember(
                    page, config, profile, llm, stats, logger, processed, card, channel
                )
                _ensure_session_alive(page)

            _pause(config, logger, "переход к следующей странице поиска")
            page_index += 1


def _profile_mismatch_message(channel: Channel) -> str:
    """Почему повтор пропустил вакансию, записанную другим профилем.

    У hh профиль — это резюме, у Habr — имя из `# PROFILE:`. Запись текущего
    профиля уже есть, поэтому повторный отклик отправлять нельзя.
    """
    if channel.name == HH:
        return (
            "Запись вакансии сделана другим резюме и по текущему резюме запись уже есть: "
            "повтор пропущен, чтобы не отправить отклик второй раз"
        )
    return (
        "Запись вакансии сделана другим профилем и по текущему профилю запись уже есть: "
        "повтор пропущен, чтобы не отправить отклик второй раз"
    )


def _retry_vacancies(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    stats: RunStats,
    logger: RunLogger,
    processed: ProcessedStore,
    statuses: tuple[str, ...],
    channel: Channel | None = None,
) -> None:
    """Повтор без обхода выдачи: вакансии открываются по ссылкам из отчётов канала запуска."""
    channel = channel or _channel_for(config)
    profile_name = _profile_name(config, channel, profile)
    records = processed.pending(statuses)
    records_of_channel = [
        record
        for record in records
        if channel_name_for_url(str(record.get("url", ""))) == channel.name
    ]
    other_channels = len(records) - len(records_of_channel)
    logger.event(
        "retry_selected",
        statuses=list(statuses),
        channel=channel.name,
        count=len(records_of_channel),
    )
    if other_channels:
        logger.event(
            "retry_channel_mismatch",
            message=(
                "Записи другого канала не повторяются: пропущено "
                f"{other_channels} {_plural(other_channels, 'вакансия', 'вакансии', 'вакансий')}"
            ),
            channel=channel.name,
            count=other_channels,
        )
    if not records_of_channel:
        logger.info(
            "Повторов нет: в отчётах нет вакансий со статусами "
            + ", ".join(statuses)
            + " для выбранного канала."
        )
        return

    logger.info(
        f"Повторная обработка по отчётам: {len(records_of_channel)} "
        f"{_plural(len(records_of_channel), 'вакансия', 'вакансии', 'вакансий')} "
        f"({', '.join(statuses)}), поиск не используется."
    )
    for record in records_of_channel:
        if _should_stop(config.pipeline.limit_per_run, stats, logger):
            break
        _check_new_day(logger, processed)
        if stats.reviewed >= config.pipeline.max_vacancies:
            stats.stop_reason = (
                f"просмотрено максимум вакансий за запуск ({config.pipeline.max_vacancies})"
            )
            logger.info(stats.stop_reason)
            break
        card = VacancyCard(
            title=str(record.get("title", "")),
            company=str(record.get("company", "")),
            url=str(record.get("url", "")),
        )
        if not card.url:
            continue
        stats.reviewed += 1
        record_profile = str(record.get("profile") or "")
        if (
            record_profile
            and profile_key(record_profile) != profile_key(profile_name)
            and processed.find_record(
                card.title, card.company, card.url, profile_name
            )
        ):
            logger.event(
                "retry_profile_mismatch",
                message=_profile_mismatch_message(channel),
                url=card.url,
            )
            continue
        _process_and_remember(
            page,
            config,
            profile,
            llm,
            stats,
            logger,
            processed,
            card,
            channel,
        )
        _ensure_session_alive(page)


def _process_and_remember(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    stats: RunStats,
    logger: RunLogger,
    processed: ProcessedStore,
    card: VacancyCard,
    channel: Channel | None = None,
) -> None:
    """Обрабатывает вакансию и записывает результат в дневной отчёт."""
    channel = channel or _channel_for(config)
    counters = _counters(stats)
    skipped_before = len(stats.skipped)
    try:
        outcome_channel = _process_vacancy(
            page, config, profile, llm, stats, logger, card
        )
    except (KeyboardInterrupt, SessionLostError):
        _remember_interrupted(processed, logger, stats, card, config, channel, profile)
        raise
    _mark_invited_resume(
        processed, logger, config, channel, card, skipped_before, stats, profile
    )
    skipped = stats.skipped[skipped_before:]
    outcome = _outcome(counters, _counters(stats))
    if outcome:
        _remember_processing(
            processed,
            logger,
            card,
            outcome,
            _outcome_short_reason(stats, counters, outcome),
            _profile_name(config, channel, profile),
            channel=outcome_channel or channel.name,
            archived=_outcome_archived(skipped),
            questionary=stats.questionary.get(card.url, ()),
            message=_outcome_message(stats, counters, outcome),
            cover_letter=stats.cover_letters.get(card.url, ""),
        )


def _remember_interrupted(
    processed: ProcessedStore,
    logger: RunLogger,
    stats: RunStats,
    card: VacancyCard,
    config: AppConfig,
    channel: Channel,
    profile: Profile | None = None,
) -> None:
    """Страховка от потери вакансии при остановке запуска.

    Отклик мог уйти или не уйти, поэтому без записи в отчёт вакансия осталась бы
    без ручной проверки на сайте.
    """
    reason = QUEUE_REASON_TEXTS["interrupted"]
    _queue(stats, logger, card, "interrupted", reason=reason, channel=channel)
    _remember_processing(
        processed,
        logger,
        card,
        OUTCOME_QUEUED,
        reason,
        _profile_name(config, channel, profile),
        channel=channel.name,
    )


def _remember_card_already_responded(
    processed: ProcessedStore,
    logger: RunLogger,
    card: VacancyCard,
    profile_name: str,
    channel: Channel,
) -> bool:
    """Фиксирует карточку «Вы откликнулись» только для новой вакансии.

    Habr показывает такие карточки в ленте каждый запуск. Любую уже известную
    запись пропускаем целиком: карточка говорит только о факте отклика и не
    знает, чем он закончился, а статусы `rejected`, `offer`, `invite` и архив
    нельзя откатывать в `sent`. Для новой вакансии запись создаётся один раз.
    Возвращает True, если вакансию нужно учесть в счётчике «уже отправлено».
    """
    known = processed.find_record(card.title, card.company, card.url, profile_name)
    if known is not None:
        logger.event(
            "skipped_already_responded_known",
            url=card.url,
            title=card.title,
            company=card.company,
            status=str(known.get("status", "")),
        )
        _VacancyLog(logger, card.title, card.company, card.url).info(
            "уже есть отклик, вакансию пропускаю"
        )
        return False

    logger.event(
        "skipped_already_responded",
        url=card.url,
        title=card.title,
        company=card.company,
    )
    _VacancyLog(logger, card.title, card.company, card.url).info(
        "уже есть отклик, карточку не открываю"
    )
    _remember_processing(
        processed,
        logger,
        card,
        OUTCOME_SENT,
        ALREADY_RESPONDED_REASON,
        profile_name,
        channel=channel.name,
    )
    return True


def _sync_habr_responses(
    page: Page,
    processed: ProcessedStore,
    logger: RunLogger,
    profile: str = "",
) -> None:
    """Сверяет статусы уже отправленных откликов со страницей откликов Habr.

    `profile` — профиль записей канала: у Habr он один и хранится в записи как
    имя из `# PROFILE:`. Без него записи с профилем не находились, и сверка
    обновляла только старые копии без профиля.

    Habr показывает «Отказ», «Приглашение» и «Оффер» только на странице
    `career.habr.com/responses`. Сверка не создаёт записи и не трогает
    терминальные `rejected`, `offer` и архив. Остальные статусы страница
    уточняет, потому что знает, что отклик действительно существует. Запись
    переносится в сегодняшний отчёт только при реальной смене статуса:
    повторный «Отказ» или «Прочитано» файл не переписывает.
    """
    seen: set[str] = set()
    read = matched = updated = skipped = unknown = 0
    try:
        for page_index in range(habr_search.RESPONSES_MAX_PAGES):
            responses = habr_search.open_responses_page(page, page_index)
            fresh = [item for item in responses if item.url and item.url not in seen]
            if not fresh:
                break
            for response in fresh:
                seen.add(response.url)
                read += 1
                if not response.status:
                    unknown += 1
                    continue
                record = processed.find_record(
                    response.title,
                    response.company,
                    response.url,
                    profile,
                )
                if record is None:
                    skipped += 1
                    continue
                current = str(record.get("status", ""))
                if current in (OUTCOME_REJECTED, STATUS_OFFER) or record.get("archived"):
                    skipped += 1
                    continue
                matched += 1
                target = response.status
                if target == current or (current == STATUS_INVITE and target == OUTCOME_SENT):
                    continue
                reason = _response_reason(target)
                if target == OUTCOME_SENT and not reason:
                    reason = ALREADY_RESPONDED_REASON
                if not processed.update_status(
                    url=response.url,
                    title=response.title,
                    company=response.company,
                    status=target,
                    reason=reason,
                    profile=profile,
                ):
                    skipped += 1
                    continue
                updated += 1
                logger.event(
                    "habr_response_status",
                    url=response.url,
                    old=current,
                    new=target,
                )
    except (CaptchaError, PageLoadError, PlaywrightError) as exc:
        logger.event("habr_responses_error", error=str(exc))
        logger.info(f"Не удалось сверить отклики Habr: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - сверка не должна ломать запуск
        logger.event("habr_responses_error", error=f"{type(exc).__name__}: {exc}")
        logger.info(f"Не удалось сверить отклики Habr: {type(exc).__name__}: {exc}")
        return

    logger.event(
        "habr_responses_sync",
        read=read,
        matched=matched,
        updated=updated,
        skipped=skipped,
        unknown=unknown,
    )
    if read:
        logger.info(
            f"Сверка откликов Habr: прочитано {read}, обновлено {updated}, "
            f"пропущено {skipped}, неизвестных статусов {unknown}"
        )


def _response_reason(status: str) -> str:
    """Причина для статуса, который пришёл со страницы откликов Habr."""
    if status == OUTCOME_REJECTED:
        return REJECTED_REASON
    if status == STATUS_INVITE:
        return INVITE_REASON
    if status == STATUS_OFFER:
        return OFFER_REASON
    return ""


def _mark_invited_resume(
    processed: ProcessedStore,
    logger: RunLogger,
    config: AppConfig,
    channel: Channel,
    card: VacancyCard,
    skipped_before: int,
    stats: RunStats,
    profile: Profile | None = None,
) -> None:
    """Ставит `invite` записи резюме, по которому пришло приглашение на интервью.

    hh.ru показывает «Вас пригласили» на странице вакансии, не уточняя резюме.
    Приглашение приходит по уже отправленному отклику, поэтому статус меняется
    только у записей этой вакансии с другим резюме и статусом `sent`.
    Состояние «Вас пригласили» узнаём по причине пропуска, которую добавила
    обработка вакансии: `_process_vacancy` возвращает только канал отчёта.
    """
    skipped = stats.skipped[skipped_before:]
    if not any(item.reason == INVITED_OTHER_RESUME_REASON for item in skipped):
        return
    try:
        updated = processed.invite_other_resume(
            url=card.url,
            title=card.title,
            company=card.company,
            profile=_profile_name(config, channel, profile),
        )
    except ProcessedError as exc:
        logger.event("processed_error", url=card.url, error=str(exc))
        return
    if updated:
        # Названия резюме — данные пользователя: в журнал пишем только количество.
        logger.event("invited_other_resume", url=card.url, resumes=len(updated))


def _outcome_archived(skipped: list[SkippedVacancy]) -> bool:
    """Вакансия в архиве или с закрытым доступом: нужен признак в отчёте.

    `_process_vacancy` возвращает только канал отчёта, поэтому состояние
    страницы узнаём по записи пропуска — как в `_mark_invited_resume`.
    """
    return any(item.archived for item in skipped)


def _ensure_session_alive(page: Page) -> None:
    """Роняет запуск, если окно закрыто или сайт требует повторный вход."""
    if _page_closed(page):
        raise SessionLostError("окно браузера закрыто — запуск остановлен")
    if _is_login_page(page):
        raise SessionLostError("сайт требует повторный вход: сессия больше не действительна")


def _process_vacancy(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    stats: RunStats,
    logger: RunLogger,
    card: VacancyCard,
) -> str | None:
    """Обрабатывает вакансию; возвращает канал отчёта, если он не равен каналу поиска."""
    channel = _channel_for(config)

    def queue(*args: object, **kwargs: object) -> None:
        """Отложить вакансию: тексты причин — из выбранного канала."""
        _queue(*args, channel=channel, **kwargs)  # type: ignore[arg-type]

    log = _VacancyLog(logger, card.title, card.company, card.url)
    excluded = is_excluded(card.title, config.search.exclude_keywords)
    if excluded:
        logger.event(
            "excluded_keyword",
            url=card.url,
            title=card.title,
            keyword=excluded,
        )
        stats.excluded.append(
            SkippedVacancy(
                title=card.title,
                company=card.company,
                url=card.url,
                reason=f"Стоп-слово в названии: {excluded}",
            )
        )
        log.info(f"пропуск: стоп-слово «{excluded}» в названии")
        return

    excluded_company = is_company_excluded(card.company, config.search.exclude_companies)
    if excluded_company:
        logger.event(
            "excluded_company",
            url=card.url,
            title=card.title,
            company=card.company,
            keyword=excluded_company,
        )
        stats.excluded.append(
            SkippedVacancy(
                title=card.title,
                company=card.company,
                url=card.url,
                reason=f"Исключённая компания: {excluded_company}",
            )
        )
        log.info(f"пропуск: компания «{card.company}» в списке исключений")
        return

    log.info("открываю вакансию")
    try:
        details = channel.open_vacancy(page, card)
    except CaptchaError as exc:
        _save_problem_screenshot(page, config, logger, "captcha", url=card.url)
        queue(stats, logger, card, "captcha", f"капча: {exc}")
        stats.stop_reason = "капча на странице вакансии"
        _notify_captcha(
            config,
            logger,
            card,
            "Капча на странице вакансии. Запуск остановлен — откликнитесь вручную.",
            site=channel.site_label,
        )
        log.info(f"{channel.site_label} показал капчу на странице вакансии, запуск остановлен.")
        return
    except (VacancyParseError, PageLoadError, PlaywrightError) as exc:
        _record_error(stats, logger, page, config, card, exc)
        return

    excluded_company = is_company_excluded(details.company, config.search.exclude_companies)
    if excluded_company:
        logger.event(
            "excluded_company",
            url=details.url,
            title=details.title,
            company=details.company,
            keyword=excluded_company,
        )
        stats.excluded.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=f"Исключённая компания: {excluded_company}",
            )
        )
        log.info(f"пропуск: компания «{details.company}» в списке исключений")
        return

    if details.already_responded:
        # Отклик уже отправлен (возможно, руками): в файле дня — статус sent,
        # но лимит откликов запуска он не расходует.
        logger.event(
            "already_responded",
            url=details.url,
            title=details.title,
            status=OUTCOME_SENT,
        )
        stats.already_sent.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=ALREADY_RESPONDED_REASON,
            )
        )
        log.info("отклик уже отправлен, помечаю вакансию отправленной")
        return

    page_status = PAGE_STATUS_REASONS.get(details.blocker or "")
    other_resume = details.blocker == "other_resume"
    if page_status is not None:
        outcome, reason = page_status
        logger.event(
            "page_status",
            url=details.url,
            title=details.title,
            page=details.blocker,
            status=outcome,
            reason=reason,
        )
        target = stats.rejected if outcome == OUTCOME_REJECTED else stats.skipped
        target.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=reason,
                archived=details.archived,
            )
        )
        if outcome == OUTCOME_REJECTED:
            log.info("на отклик пришёл отказ, вакансия пройдена")
        else:
            log.info(f"пропуск: {reason.lower()}")
        return

    external_form = details.blocker == "external_form"
    habr_company_site = external_form and channel.name == HABR
    if details.blocker and not other_resume and not habr_company_site:
        queue(stats, logger, details, details.blocker, "признак на странице вакансии")
        return EXTERNAL_CHANNEL if external_form else None

    if (
        not other_resume
        and not habr_company_site
        and stats.sent_count >= config.pipeline.limit_per_run
    ):
        stats.stop_reason = f"достигнут лимит откликов за запуск ({config.pipeline.limit_per_run})"
        log.info(f"лимит откликов достигнут ({stats.sent_count}), вакансия не обрабатывается")
        return

    log.mark_llm()
    try:
        decision = llm.decide(profile, details)
    except LLMError as exc:
        _record_error(stats, logger, page, config, details, exc)
        return

    llm_reason = ""
    llm_reason_source = ""
    fits = meets_threshold(decision.probability, decision.threshold)
    verdict = FIT_LABEL if fits else NOT_FIT_LABEL
    if not fits:
        llm_reason = (
            f"вероятность {decision.probability:.2f} не выше порога {decision.threshold:.2f}"
        )
        llm_reason_source = "threshold"
        if config.llm.decision.explain_rejections:
            try:
                explanation = llm.explain_rejection(
                    profile, details, decision.probability, decision.threshold
                )
            except LLMError as exc:
                logger.event("rejection_explanation_error", url=details.url, error=str(exc))
            else:
                if explanation:
                    llm_reason = explanation
                    llm_reason_source = "model"

    log.event(
        "decision",
        url=details.url,
        title=details.title,
        probability=decision.probability,
        threshold=decision.threshold,
        fits=fits,
        llm_verdict=verdict,
        model_choice=decision.chosen,
        llm_reason=llm_reason,
        llm_reason_source=llm_reason_source,
        probabilities=decision.probabilities,
    )
    probabilities = ", ".join(
        f"{label}={value:.2f}" for label, value in decision.probabilities.items()
    )
    log.info(
        f"LLM-решение: «{verdict}» ({probabilities}, порог {decision.threshold:.2f})"
    )
    if llm_reason and config.llm.decision.explain_rejections:
        log.console(f"LLM-вердикт: {llm_reason}")

    if not fits:
        stats.skipped.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=llm_reason,
                probability=decision.probability,
                report_reason=NOT_FIT_REPORT_REASON,
                report_message=llm_reason,
            )
        )
        log.info(f"вакансия не подошла: {llm_reason}")
        return

    if other_resume:
        queue(
            stats,
            logger,
            details,
            "other_resume",
            "подходит, но отклик уже отправлен другим резюме",
            reason=OTHER_RESUME_REASON,
        )
        log.info(f"подходит, но уже есть отклик другого резюме: {OTHER_RESUME_REASON}")
        return

    captcha_waiter = _captcha_waiter(
        page, config, logger, details, site=channel.site_label
    )
    questionary: _Questionnaire | None = None
    if config.pipeline.prefilter.enabled and channel.name == HH:
        questionary = _knockout_prefilter(page, config, stats, logger, details, captcha_waiter)
        if questionary is None:
            return None

    pending = questionary.pending if questionary is not None else ()
    needs_project_links = requires_project_links(details)
    # Анкета уже решена профилем, а отправлять её нельзя: Generation не нужен.
    if questionary is not None and questionary.result is not None and not pending:
        if questionary.result.answers_to_fill() and not config.pipeline.prefilter.submit_auto_fill:
            queue(
                stats,
                logger,
                details,
                "prefilter_auto_fill_disabled",
                "ответы анкеты вычислены",
                reason="",
            )
            return None
    questions = {"questionnaire": question_lines(pending)} if pending else {}

    try:
        generation = llm.generate(
            profile,
            details,
            config.llm.cover_letter.max_length,
            signature=config.llm.cover_letter.signature,
            **questions,
        )
    except LLMError as exc:
        _record_error(stats, logger, page, config, details, exc)
        return

    if generation.verdict != "подходит":
        logger.event(
            "generation_verdict_mismatch",
            url=details.url,
            decision_probability=decision.probability,
            llm_verdict=generation.verdict,
            llm_reason=generation.reason,
        )

    if generation.cover_letter:
        stats.cover_letters[card.url] = generation.cover_letter

    if not generation.cover_letter:
        cause = _first_sentence(_generation_reason_text(generation.reason))
        queue(
            stats,
            logger,
            details,
            "empty_cover_letter",
            generation.reason,
            reason=cause,
            message=generation.reason,
        )
        return None

    if habr_company_site:
        queue(
            stats,
            logger,
            details,
            "external_form",
            "отклик на сайте компании",
            message=generation.reason,
        )
        return None

    if needs_project_links and not _letter_has_project_link(
        generation.cover_letter, config.llm.cover_letter.pet_projects
    ):
        if config.llm.cover_letter.pet_projects:
            reason = (
                "В вакансии требуется ссылка на проекты в письме, но ни одна ссылка "
                "из llm.cover_letter.pet-projects в письмо не попала"
            )
        else:
            reason = (
                "В вакансии требуется ссылка на проекты в письме, "
                "но llm.cover_letter.pet-projects не задан"
            )
        queue(
            stats,
            logger,
            details,
            "project_links",
            generation.reason,
            reason=reason,
            message=generation.reason,
        )
        return None

    log.event(
        "generation",
        url=details.url,
        llm_verdict=generation.verdict,
        llm_reason=generation.reason,
        llm_reason_source="model",
        letter_length=len(generation.cover_letter),
        greeting_added=generation.greeting_added,
        required_opening=generation.required_opening,
    )
    log.console(f"LLM-вердикт: {generation.reason or '—'}")
    if generation.greeting_added:
        if generation.required_opening:
            log.info(
                f"приветствие добавлено после обязательного начала "
                f"«{generation.required_opening}»"
            )
        else:
            log.info("в письме не было приветствия, добавил «Здравствуйте!»")

    _pause(config, logger, "перед отправкой отклика")
    answers = _questionary_answers(questionary, pending, generation.answers)
    if answers is None:
        partial = _questionary_partial(questionary, pending, generation.answers)
        if partial:
            _remember_questionary(stats, details, partial)
        short_reason = _pending_reason(pending, generation.answers)
        full_reason = _pending_reason(pending, generation.answers, full=True)
        queue(
            stats,
            logger,
            details,
            "questionnaire",
            "ответы анкеты",
            reason=short_reason,
            message="" if full_reason == short_reason else full_reason,
        )
        return None

    if answers:
        _remember_questionary(stats, details, answers)

    form = None
    if questionary is not None and questionary.result is not None:
        if answers and not config.pipeline.prefilter.submit_auto_fill:
            queue(
                stats,
                logger,
                details,
                "prefilter_auto_fill_disabled",
                "ответы анкеты вычислены",
                reason="",
            )
            return None
        form = ResponseForm(
            questionary.form.status,
            questionary.form.reason,
            questionary.form.questions,
            answers,
            questionary.form.other_resume,
            questionary.form.steps,
        )

    after_fill = (
        (lambda: _snapshot_questionary(page, config, logger, details)) if answers else None
    )

    try:
        if form is not None and questionary is not None and questionary.test:
            test_result = submit_test(
                page, form.answers, captcha_waiter, after_fill=after_fill
            )
            if test_result.status == TEST_PASSED:
                log.info("тест по вакансии пройден, отправляю отклик")
                result = channel.respond(
                    page, generation.cover_letter, captcha_waiter=captcha_waiter
                )
            else:
                result = test_result
        elif form is not None:
            result = submit_response(
                page,
                form,
                generation.cover_letter,
                captcha_waiter,
                form.answers,
                after_fill=after_fill,
            )
        else:
            result = channel.respond(
                page, generation.cover_letter, captcha_waiter=captcha_waiter
            )
    except ResumeNotFoundError as exc:
        logger.event(
            "resume_not_found",
            resume=exc.resume,
            url=card.url,
            title=card.title,
        )
        raise
    except PlaywrightError as exc:
        _record_error(stats, logger, page, config, details, exc)
        return None

    status = result.status
    log.event("respond", url=details.url, status=status)

    if status == STATUS_SENT:
        stats.sent.append(
            SentVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                probability=decision.probability,
                reason=generation.reason,
                report_reason=SENT_REPORT_REASON,
                report_message=generation.reason,
            )
        )
        log.info(f"отклик отправлен (всего {stats.sent_count} из {config.pipeline.limit_per_run})")
        _pause(config, logger, "после отправки отклика")
        return None

    if status == STATUS_REJECTED:
        log.event(
            "already_rejected",
            url=details.url,
            status=OUTCOME_REJECTED,
            reason=REJECTED_REASON,
        )
        stats.rejected.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=REJECTED_REASON,
                probability=decision.probability,
            )
        )
        log.info("по вакансии уже есть отказ по текущему резюме, вакансия пройдена")
        return None

    if status == STATUS_ALREADY_RESPONDED:
        stats.already_sent.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=ALREADY_RESPONDED_REASON,
                probability=decision.probability,
            )
        )
        log.info(f"{channel.site_label} уже принял отклик, помечаю вакансию отправленной")
        return None

    if status == STATUS_CAPTCHA:
        _save_problem_screenshot(page, config, logger, "captcha", url=details.url)
        queue(stats, logger, details, "captcha", "капча в форме отклика")
        stats.stop_reason = "Капча при отправке отклика — откликнитесь вручную"
        _notify_captcha_stop(
            config, logger, details, captcha_waiter, site=channel.site_label
        )
        log.info(f"{channel.site_label} показал капчу при отправке отклика, запуск остановлен.")
        return None

    if status == TEST_FAILED:
        queue(
            stats,
            logger,
            details,
            "test",
            "тест по вакансии",
            reason="Тест по вакансии не пройден — откликнитесь вручную",
        )
        return None

    if status in ("test", "questionnaire", "external_form"):
        queue(stats, logger, details, status, "в форме отклика")
        return None

    if status == STATUS_NO_FORM:
        _save_problem_screenshot(page, config, logger, "respond-no-form", url=details.url)
        diagnostic = _respond_diagnostic(result.steps)
        logger.event(
            "respond_state",
            url=details.url,
            vacancy_id=vacancy_id(details.url),
            status=status,
            steps=dict(result.steps),
            **channel.response_state(page),
        )
        queue(
            stats,
            logger,
            details,
            "no_form",
            "не найдена форма отклика",
            reason=result.reason,
            message=diagnostic,
        )
        return None

    if status == STATUS_UNKNOWN:
        _save_problem_screenshot(page, config, logger, "respond-unknown", url=details.url)
        logger.event(
            "respond_state",
            url=details.url,
            vacancy_id=vacancy_id(details.url),
            status=status,
            steps=dict(result.steps),
            **channel.response_state(page),
        )
    queue(
        stats,
        logger,
        details,
        "unknown",
        f"статус отклика: {status or STATUS_UNKNOWN}",
        reason=result.reason,
        message=_respond_diagnostic(result.steps),
    )


def _questionary_partial(
    questionary: _Questionnaire | None,
    pending: Sequence[Question],
    values: Mapping[int, str],
) -> tuple[Answer, ...]:
    """Ответы, которые уже удалось вычислить: профиль и ответы модели."""
    if questionary is None or questionary.result is None:
        return ()
    return merge_answers(questionary.result, llm_answers(pending, values))


def _questionary_answers(
    questionary: _Questionnaire | None,
    pending: Sequence[Question],
    values: Mapping[int, str],
) -> tuple[Answer, ...] | None:
    """Ответы для формы: профиль первым, ответы модели на остаток.

    `None` — на часть вопросов не ответил ни профиль, ни модель: такую анкету
    отправлять нельзя, вакансия уходит в ручные. Модель не переопределяет
    профиль: расхождение означало бы выдумку.
    """
    answers = _questionary_partial(questionary, pending, values)
    answered = {answer.question.name or answer.question.text for answer in answers}
    for question in pending:
        if (question.name or question.text) not in answered:
            return None
    return answers


def _pending_reason(
    pending: Sequence[Question],
    values: Mapping[int, str],
    *,
    full: bool = False,
) -> str:
    """Причина ручного ответа: короткая для списка, подробная для окна деталей.

    `full=True` возвращает полный текст с формулировкой вопроса и числом без
    ответа. Короткий текст одинаков для субъективных анкет: человеку в списке
    достаточно понять, что анкету нужно доделать вручную.
    """
    unanswered = [
        question
        for number, question in enumerate(pending, 1)
        if not str(values.get(number, "") or "").strip()
    ]
    if not unanswered:
        return "Анкета работодателя: часть вопросов осталась без ответа, ответьте вручную"
    labels = (
        [question_full_label(question) for question in unanswered]
        if full
        else [question_label(question) for question in unanswered]
    )
    readable = [label for label in labels if label != NO_QUESTION_LABEL]
    if not readable:
        return (
            "Анкета работодателя: не удалось прочитать формулировки вопросов. "
            f"Без ответа {len(unanswered)} из {len(pending)}"
        )
    if full:
        return (
            f"Анкета работодателя: вопрос «{readable[0]}» субъективен и требует ручного ответа. "
            f"Без ответа {len(unanswered)} из {len(pending)}"
        )
    return QUESTIONARY_MANUAL_REASON


def _remember_questionary(
    stats: RunStats,
    details: VacancyDetails,
    answers: Sequence[Answer],
) -> None:
    """Держит ответы для записи в отчёт дня: блок `questionary` пишет пайплайн.

    Ответы попадают в отчёт и когда отклик остался за человеком: по файлу дня
    видно, что именно агент собирался отправить работодателю.
    """
    entries = [
        {"question": question_label(answer.question), "answer": answer.value}
        for answer in answers
        if answer.filled
    ]
    if entries:
        stats.questionary[details.url] = entries


def _snapshot_questionary(
    page: Page,
    config: AppConfig,
    logger: RunLogger,
    details: VacancyDetails,
) -> None:
    """Снимает заполненную форму анкеты или теста до отправки.

    Снимок — доказательство того, что ушло работодателю. Сбой съёмки не должен
    ломать отклик: он только попадает в журнал отдельным событием.
    """
    saved = questionary_store.save_screenshot(
        config.data.paths.questionary_dir, page, url=details.url
    )
    if saved is None:
        logger.event("questionary_screenshot_error", url=details.url)
        return
    vid = vacancy_id(details.url)
    extra = {"vacancy_id": vid} if vid else {}
    logger.event("questionary_screenshot", url=details.url, path=str(saved), **extra)


@dataclass(frozen=True)
class _Questionnaire:
    """Форма отклика и вердикт префильтра по её анкете.

    `result` пуст, если анкеты нет или форма не открылась: дальше работает
    обычный путь отклика. Непустой `result` — вопросы, ответы профиля и то,
    что осталось решить модели.
    """

    form: ResponseForm
    result: PrefilterResult | None = None
    # Форма оказалась тестом по вакансии: ответы идут в тест, не в анкету.
    test: bool = False

    @property
    def pending(self) -> tuple[Question, ...]:
        """Вопросы без ответа из профиля: их закрывает Generation-вызов."""
        return unanswered_questions(self.result) if self.result is not None else ()


def _knockout_prefilter(
    page: Page,
    config: AppConfig,
    stats: RunStats,
    logger: RunLogger,
    details: VacancyDetails,
    captcha_waiter: Callable[[], bool] | None = None,
) -> _Questionnaire | None:
    """Открывает форму отклика и разбирает анкету до Generation.

    Возвращает открытую форму и вердикт префильтра. `None` — вакансия уже
    закрыта: анкета гарантированно ведёт к отказу (`skipped`) или форма
    не открылась. Вопросы, на которые профиль не ответил, остаются в вердикте:
    их закрывает тот же Generation-вызов, что и письмо.

    Форма, которая не открылась (тест, внешняя анкета, капча), возвращается как
    есть: её статус разбирает общий код после Generation, как и раньше.
    """
    log = _VacancyLog(logger, details.title, details.company, details.url)
    is_test = False
    try:
        form = open_response_form(page, captcha_waiter, ignore_questionnaire=True)
    except PlaywrightError as exc:
        _record_error(stats, logger, page, config, details, exc)
        return None
    if form.status == "test":
        # Тест по вакансии — отдельный экран со своими вопросами и результатом.
        form = start_test(page, captcha_waiter)
        is_test = form.ready
    if not form.ready:
        # Почему префильтр не сработал — видно в журнале, а не только по статусу.
        log.event("response_form", url=details.url, status=form.status, reason=form.reason)
        log.info(f"форма отклика не открылась: {form.status}")
        return _Questionnaire(form)
    if not form.questions:
        return _Questionnaire(form, test=is_test)

    result = evaluate(form.questions, config.pipeline.prefilter.answers)
    if is_test:
        # В тесте отрицательный ответ не отсечка: он лишь снижает балл.
        result = without_rejects(result)
    log.event(
        "prefilter",
        url=details.url,
        verdict=result.verdict,
        questions=len(form.questions),
        clusters=list(result.clusters),
        unknown=result.unknown,
        reason=result.reason,
    )
    if result.verdict == VERDICT_MANUAL:
        # Подробности остаются в дневном отчёте: в консоли достаточно итога.
        log.info(manual_hint(result))
    else:
        log.info(f"анкета работодателя: вопросов {len(form.questions)}, вердикт «{result.verdict}»")

    if result.verdict == VERDICT_REJECT:
        stats.skipped.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=result.reason,
            )
        )
        log.info(f"пропуск: {result.reason.lower()}")
        return None
    return _Questionnaire(form, result, test=is_test)


def _queue(
    stats: RunStats,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails,
    code: str,
    note: str = "",
    *,
    reason: str = "",
    message: str = "",
    channel: Channel | None = None,
) -> None:
    """Отмечает вакансию как отложенную: отклик нужно отправить вручную.

    `reason` — короткая причина для списка и журнала. `message` — полный текст
    с формулировками вопросов: он показывается в окне деталей и не обрезается.
    Общие тексты причин берутся из канала: у hh.ru и Habr они разные.
    """
    reasons = channel.queue_reasons if channel is not None else QUEUE_REASON_TEXTS
    text = reason.strip() or reasons.get(code, reasons.get("unknown", ""))
    message_text = message.strip()
    log = _VacancyLog(logger, card.title, card.company, card.url)
    entry: dict[str, object] = {
        "url": card.url,
        "title": card.title,
        "company": card.company,
        "reason": text,
        "queued_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if message_text:
        entry["message"] = message_text
    stats.queued.append(entry)
    extra = {"message": message_text} if message_text else {}
    log.event("queued", url=card.url, title=card.title, code=code, reason=text, note=note, **extra)
    log.console(f"отложено, нужен ручной отклик: {text}")


class _CaptchaWaiter:
    """Ожидание капчи в форме отклика; письмо о капче уходит один раз.

    Человеку достаточно одного сигнала: если он не успел, об остановке запуска
    скажет отчёт, а таймаут фиксируется в JSONL (`captcha_timeout`).
    """

    def __init__(
        self,
        page: Page,
        config: AppConfig,
        logger: RunLogger,
        card: VacancyCard | VacancyDetails | None = None,
        site: str = "hh.ru",
    ) -> None:
        self._page = page
        self._config = config
        self._logger = logger
        self._card = card
        self._site = site
        self._log = (
            _VacancyLog(logger, card.title, card.company, card.url) if card is not None else None
        )
        self.seconds = config.browser.captcha_wait_seconds
        self.notified = False

    def _event(self, name: str, **fields: object) -> None:
        if self._log is not None:
            self._log.event(name, url=self._page.url, **fields)
        else:
            self._logger.event(name, url=self._page.url, **fields)

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(message)
        else:
            self._logger.info(message)

    def __call__(self) -> bool:
        seconds = self.seconds
        self._event("captcha_wait", seconds=seconds)
        self._info(
            f"{self._site} просит пройти капчу: введите текст с картинки в открытом окне "
            f"браузера и нажмите «Отправить», жду до {seconds} с."
        )
        if not self.notified:
            _notify_captcha(
                self._config,
                self._logger,
                self._card,
                f"Капча в форме отклика: жду решения до {seconds} с. "
                "Введите текст с картинки в открытом окне браузера и нажмите «Отправить».",
                url=self._page.url,
                site=self._site,
            )
            self.notified = True
        solved = wait_for_captcha_solved(self._page, seconds)
        self._event("captcha_solved" if solved else "captcha_timeout", seconds=seconds)
        if solved:
            self._info("Капча пройдена, продолжаю отклик.")
        else:
            self._info("Капча не пройдена за отведённое время, отклик отложен.")
        return solved


def _captcha_waiter(
    page: Page,
    config: AppConfig,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails | None = None,
    site: str = "hh.ru",
) -> _CaptchaWaiter | None:
    """Ждёт, пока человек решит капчу в открытом окне браузера.

    В headless-режиме окна нет и ждать некому — возвращаем None, чтобы ответ
    ушёл в отложенные с понятной причиной.
    """
    if config.browser.captcha_wait_seconds <= 0 or config.browser.headless:
        return None
    return _CaptchaWaiter(page, config, logger, card, site)


def _notify_captcha_stop(
    config: AppConfig,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails | None,
    waiter: _CaptchaWaiter | None,
    site: str = "hh.ru",
) -> None:
    """Письмо «капча остановила запуск» — только если о ней ещё не писали."""
    if waiter is not None and waiter.notified:
        return
    _notify_captcha(
        config,
        logger,
        card,
        "Капча в форме отклика. Запуск остановлен — откликнитесь вручную.",
        site=site,
    )


def _notify_captcha(
    config: AppConfig,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails | None = None,
    note: str = "",
    url: str = "",
    site: str = "hh.ru",
) -> None:
    """Письмо о капче: best-effort, сбой отправки не останавливает запуск.

    В письме только вакансия и ссылка — ни профиля, ни текста письма, ни контактов.
    """
    if card is not None:
        title, company = card.title, card.company
        url = url or card.url
    else:
        title, company = "", ""
    try:
        sent, message = notify_captcha(
            config.notifications.email,
            config.notifications.captcha,
            title=title,
            company=company,
            url=url,
            note=note,
            site=site,
        )
    except Exception as exc:  # noqa: BLE001 - уведомление не должно ронять запуск
        sent, message = False, f"{type(exc).__name__}: {exc}"
    vid = vacancy_id(url)
    extra = {"vacancy_id": vid} if vid else {}
    logger.event("email_notify", kind="captcha", sent=sent, message=message, **extra)
    if not sent:
        text = f"Письмо о капче не отправлено: {message}"
        label = f"{title} — {company}" if company else title
        console = f"[{vid}] {label}: {text}" if vid and label else None
        logger.info(text, console_message=console, **extra)


def _respond_diagnostic(steps: Mapping[str, str]) -> str:
    """Пошаговое описание попытки отклика для окна деталей отчёта.

    В таблице отчёта остаётся короткая причина, а здесь видно, на каком шаге
    встал отклик: форма, резюме, анкета, письмо, кнопка отправки, ожидание
    подтверждения. По этому тексту понятно, что именно сломалось.
    """
    parts = [f"{name}: {value}" for name, value in steps.items()]
    if not parts:
        return ""
    text = "; ".join(parts)
    return text[:1].upper() + text[1:] + "."


def _save_problem_screenshot(
    page: Page,
    config: AppConfig,
    logger: RunLogger,
    kind: str,
    *,
    url: str = "",
) -> None:
    """Сохраняет скриншот проблемного состояния — по нему видно, что показал сайт.

    `url` привязывает снимок к вакансии: без `vacancy_id` непонятно, к какой
    карточке он относится. Диагностика не должна ронять запуск: любые сбои
    здесь только логируются.
    """
    vid = vacancy_id(url)
    extra = {"vacancy_id": vid} if vid else {}
    path = config.data.paths.logs_dir / f"{logger.run_id}-{kind}.png"
    try:
        saved = save_screenshot(page, path)
        if saved:
            logger.event("screenshot", path=str(saved), kind=kind, **extra)
        else:
            logger.event("screenshot_error", kind=kind, **extra)
    except Exception as exc:  # noqa: BLE001 - отчёт важнее скриншота
        logger.info(f"Не удалось сохранить скриншот ({kind}): {exc}", **extra)


def _record_error(
    stats: RunStats,
    logger: RunLogger,
    page: Page,
    config: AppConfig,
    card: VacancyCard | VacancyDetails,
    exc: BaseException,
) -> None:
    message = f"{type(exc).__name__}: {exc}"
    log = _VacancyLog(logger, card.title, card.company, card.url)
    stats.errors.append(RunError(url=card.url, title=card.title, message=message))
    log.event("vacancy_error", url=card.url, title=card.title, error=message)
    log.console(f"ошибка: {message}")
    screenshot = config.data.paths.logs_dir / f"{logger.run_id}-error-{len(stats.errors)}.png"
    if save_screenshot(page, screenshot):
        vid = vacancy_id(card.url)
        extra = {"vacancy_id": vid} if vid else {}
        logger.event("screenshot", url=card.url, path=str(screenshot), **extra)


def _send_report_email(config: AppConfig, report: str, stats: RunStats) -> tuple[bool, str]:
    """Письмо с отчётом: свой раздел notifications.report (вкл/адрес)."""
    settings = config.notifications.report
    if not settings.enabled:
        return False, "письма с отчётом отключены (notifications.report.enabled: false)"
    return send_email(
        config.notifications.email,
        f"Отчёт за {stats.started_at:%Y-%m-%d %H:%M}",
        report,
        recipient=settings.target(config.notifications.email),
    )


def _single_vacancy_title(stats: RunStats) -> str:
    """Название вакансии, если запуск обработал ровно одну."""
    titles: list[str] = []
    for items in (
        stats.sent,
        stats.skipped,
        stats.excluded,
        stats.rejected,
        stats.already_sent,
    ):
        titles.extend(str(getattr(item, "title", "")).strip() for item in items)
    titles.extend(str(item.get("title", "")).strip() for item in stats.queued)
    titles.extend(str(getattr(item, "title", "")).strip() for item in stats.errors)
    unique = [title for title in dict.fromkeys(titles) if title]
    return unique[0] if len(unique) == 1 else ""


def _report_context(
    config: AppConfig,
    channel: Channel,
    stats: RunStats,
) -> tuple[str, str]:
    """Что назвать в заголовке отчёта: резюме hh или вакансию Habr.

    Запуск по одной вакансии получает контекст. У hh резюме выбирается
    в отклике, поэтому в заголовке важнее его название. У Habr резюме hh
    нет: остаётся название вакансии.
    """
    title = _single_vacancy_title(stats)
    if not title:
        return "", ""
    resume = _profile_name(config, channel)
    if resume:
        return "", resume
    return title, ""


def _final_message(stats: RunStats, config: AppConfig) -> str:
    """Итоговое сообщение о завершении работы."""
    message = (
        f"Работа приложения завершена, отправлено откликов: "
        f"{len(stats.sent)} из {config.pipeline.limit_per_run}."
    )
    if stats.stop_reason:
        message += f" Причина остановки: {stats.stop_reason}."
    message += f" Сессия сохранена в {config.data.paths.session_state}."
    return message


def _counters(stats: RunStats) -> tuple[int, int, int, int, int, int, int]:
    """Счётчики результата: отправлено, пропущено, в очередь, ошибки, исключённые,
    отказы, уже отправленные ранее.
    """
    return (
        stats.sent_count,
        len(stats.skipped),
        len(stats.queued),
        len(stats.errors),
        len(stats.excluded),
        len(stats.rejected),
        len(stats.already_sent),
    )


def _outcome(
    before: tuple[int, int, int, int, int, int, int],
    after: tuple[int, int, int, int, int, int, int],
) -> str | None:
    """Результат обработки вакансии по изменению счётчиков.

    None — вакансия не обрабатывалась (например, сработал лимит откликов),
    такие вакансии не запоминаем, чтобы пройти их в следующем запуске.
    """
    if after[0] > before[0]:
        return OUTCOME_SENT
    if after[3] > before[3]:
        return OUTCOME_ERROR
    if after[4] > before[4]:
        return OUTCOME_EXCLUDED
    if after[5] > before[5]:
        return OUTCOME_REJECTED
    if after[6] > before[6]:
        # Отклик уже был отправлен: в файле дня это статус sent.
        return OUTCOME_SENT
    if after[2] > before[2]:
        return OUTCOME_QUEUED
    if after[1] > before[1]:
        return OUTCOME_SKIPPED
    return None


def _outcome_reason(
    stats: RunStats,
    before: tuple[int, int, int, int, int, int, int],
    outcome: str,
) -> str:
    """Причина результата для файла дня: из последней записи по этому статусу."""
    if outcome == OUTCOME_SENT:
        # Сначала настоящий отклик: причина — последнее предложение письма.
        # Затем уже отправленный ранее: причина остаётся «отклик уже отправлен».
        if len(stats.sent) > before[0]:
            return stats.sent[-1].reason
        if len(stats.already_sent) > before[6]:
            return stats.already_sent[-1].reason
    if outcome == OUTCOME_SKIPPED and len(stats.skipped) > before[1]:
        return stats.skipped[-1].reason
    if outcome == OUTCOME_QUEUED and len(stats.queued) > before[2]:
        return str(stats.queued[-1].get("reason", ""))
    if outcome == OUTCOME_EXCLUDED and len(stats.excluded) > before[4]:
        return stats.excluded[-1].reason
    if outcome == OUTCOME_REJECTED and len(stats.rejected) > before[5]:
        return stats.rejected[-1].reason
    if outcome == OUTCOME_ERROR and len(stats.errors) > before[3]:
        return stats.errors[-1].message
    return ""


def _outcome_short_reason(
    stats: RunStats,
    before: tuple[int, int, int, int, int, int, int],
    outcome: str,
) -> str:
    """Короткая причина для таблицы отчёта.

    Полный текст (ответ LLM, описание сбоя) уходит в `message` и открывается
    в окне деталей: колонка «Причина» остаётся плотной и сравнимой.
    """
    if outcome == OUTCOME_SENT and len(stats.sent) > before[0]:
        item = stats.sent[-1]
        return item.report_reason or item.reason
    if outcome == OUTCOME_SKIPPED and len(stats.skipped) > before[1]:
        item = stats.skipped[-1]
        return item.report_reason or item.reason
    if outcome == OUTCOME_ERROR and len(stats.errors) > before[3]:
        return ERROR_REPORT_REASON
    return _outcome_reason(stats, before, outcome)


def _outcome_message(
    stats: RunStats,
    before: tuple[int, int, int, int, int, int, int],
    outcome: str | None,
) -> str:
    """Подробный текст причины для окна деталей: полный, без обрезки."""
    if outcome == OUTCOME_SENT and len(stats.sent) > before[0]:
        return stats.sent[-1].report_message
    if outcome == OUTCOME_SKIPPED and len(stats.skipped) > before[1]:
        return stats.skipped[-1].report_message
    if outcome == OUTCOME_ERROR and len(stats.errors) > before[3]:
        return stats.errors[-1].message
    if outcome == OUTCOME_QUEUED and len(stats.queued) > before[2]:
        return str(stats.queued[-1].get("message", ""))
    return ""


def _remember_processing(
    processed: ProcessedStore,
    logger: RunLogger,
    card: VacancyCard,
    outcome: str,
    reason: str = "",
    profile: str | None = None,
    channel: str = "",
    archived: bool = False,
    questionary: Sequence[Mapping[str, str]] = (),
    message: str = "",
    cover_letter: str = "",
) -> None:
    """Записывает вакансию в дневной отчёт, чтобы не проходить её снова.

    Для пропусков, отложенных и упавших вакансий пишется причина — по файлу
    дня видно, почему вакансию не взяли и что имеет смысл повторить.
    `message` — полный текст причины для окна деталей.
    `cover_letter` — сопроводительное письмо для окна деталей.
    """
    if (
        outcome
        in (OUTCOME_SKIPPED, OUTCOME_QUEUED, OUTCOME_EXCLUDED, OUTCOME_ERROR, OUTCOME_REJECTED)
        and not reason
    ):
        logger.event("reason_missing", url=card.url, title=card.title, status=outcome)
    try:
        if archived and processed.mark_archived(
            url=card.url,
            title=card.title,
            company=card.company,
            profile=profile or "",
        ):
            # Вакансия уже есть в отчётах: статус и причину не меняем,
            # иначе разойдутся метрики и сводка. Новый только признак архива.
            logger.event("archived_known_vacancy", url=card.url, status=outcome)
            return
        processed.record(
            url=card.url,
            title=card.title,
            company=card.company,
            status=outcome,
            reason=reason,
            profile=profile,
            channel=channel,
            archived=archived,
            questionary=questionary,
            message=message,
            cover_letter=cover_letter,
        )
    except ProcessedError as exc:
        vid = vacancy_id(card.url)
        extra = {"vacancy_id": vid} if vid else {}
        logger.event("processed_error", url=card.url, error=str(exc), **extra)
        text = f"Не удалось сохранить память об обработанной вакансии: {exc}"
        label = f"{card.title} — {card.company}" if card.company else card.title
        console = f"[{vid}] {label}: {text}" if vid and label else None
        logger.info(text, console_message=console, **extra)


def _check_new_day(logger: RunLogger, processed: ProcessedStore) -> None:
    """Пишет в новые файлы журнала и отчёта, если дата сменилась во время запуска.

    Журнал и дневной отчёт — файлы даты, поэтому после полуночи переключаемся
    на новые файлы, иначе записи попадут во вчерашние.
    """
    today = date.today()
    rotated = processed.roll_to(today)
    rotated = logger.switch_day(today.strftime("%Y-%m-%d")) or rotated
    if not rotated:
        return
    logger.event("new_day", date=today.isoformat(), report=str(processed.path))
    logger.info(f"Наступила новая дата ({today:%d.%m.%Y}), пишу в новые файлы отчёта и журнала.")


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русская форма слова для числа: 1 страница, 2 страницы, 5 страниц."""
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    tail = count % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def _should_stop(limit_per_run: int, stats: RunStats, logger: RunLogger) -> bool:
    if stats.stop_reason:
        return True
    if stats.sent_count >= limit_per_run:
        stats.stop_reason = f"достигнут лимит откликов за запуск ({limit_per_run})"
        logger.info(stats.stop_reason)
        return True
    return False


def _pause(config: AppConfig, logger: RunLogger, reason: str) -> None:
    seconds = config.pipeline.delay_between_actions.next()
    if seconds <= 0:
        return
    logger.event("pause", seconds=round(seconds, 2), reason=reason)
    time.sleep(seconds)


def _is_login_page(page: Page) -> bool:
    try:
        url = page.url.lower()
    except PlaywrightError:
        return False
    return (
        "account/login" in url
        or url.rstrip("/").endswith("/login")
        or "account.habr.com" in url
        or "/ident/" in url
    )


def _page_closed(page: Page) -> bool:
    try:
        return page.is_closed()
    except PlaywrightError:
        return True
