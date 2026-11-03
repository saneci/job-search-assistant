"""Основной цикл работы агента (раздел 3.4 требований)."""

from __future__ import annotations

import re
import time
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
    VacancyParseError,
    open_vacancy,
    respond,
    response_state,
    set_resume_name,
)
from .llm import LLMClient, LLMError, meets_threshold
from .logging_setup import RunLogger, prune_logs
from .models import (
    ALREADY_RESPONDED_REASON,
    ARCHIVE_REASON,
    INVITE_REASON,
    INVITED_OTHER_RESUME_REASON,
    OFFER_REASON,
    OTHER_RESUME_REASON,
    REJECTED_REASON,
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
from .profile import Profile, load_profile
from .prompts import requires_project_links
from .processed import ProcessedError, ProcessedStore
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
# Такие отклики автоматически не отправляются: вакансия ждёт ручной обработки.
EXTERNAL_REASON = "Отклик на сайте компании"

# Вакансии, на которые нельзя откликнуться автоматически: что писать в отчёте за день.
QUEUE_REASON_TEXTS: dict[str, str] = {
    "empty_cover_letter": "Нужно заполнить сопроводительное письмо вручную",
    "project_links": "В вакансии требуется ссылка на проекты в письме",

    "test": "Для отклика на вакансию необходимо пройти тестирование",
    "questionnaire": "Для отклика на вакансию необходимо заполнить анкету",
    "external_form": "Отклик нужно отправить на сайте работодателя",
    "captcha": "hh.ru просит пройти капчу: откликнитесь вручную",
    "no_form": "hh.ru не показал форму отклика — проверьте вакансию вручную",
    "unknown": "hh.ru не подтвердил отправку отклика — проверьте вакансию вручную",
    "other_resume": "Уже отправлен отклик по другому резюме",
    "interrupted": "Запуск прерван во время обработки вакансии, проверьте вакансию вручную",
}

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
    processed = _open_report(config, logger)

    profile = load_profile(config.data.paths.profile)

    config = with_channel(config, channel_name)
    channel = channel_for(config)
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
                _sync_habr_responses(page, processed, logger)
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

    report = build_report(stats, report_path)
    logger.info("")
    logger.info_plain(build_report(stats, None))
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


def _open_report(config: AppConfig, logger: RunLogger) -> ProcessedStore:
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
    processed = ProcessedStore(config.data.paths.report_dir, run_id=logger.run_id)
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


def _profile_name(config: AppConfig, channel: Channel) -> str:
    """Резюме для записи отчёта: у hh — из конфига, у habr резюме hh нет."""
    return config.pipeline.resume.name if channel.name == HH else ""


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
    profile_name = _profile_name(config, channel)
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

                if card.external_response:
                    # Автоматической отправки нет: вакансия должна остаться
                    # в очереди, иначе ручной отклик потеряется среди пропусков.
                    logger.event("queued_external", url=card.url, title=card.title)
                    _queue(
                        stats,
                        logger,
                        card,
                        "external_form",
                        "отклик на сайте компании",
                        reason=EXTERNAL_REASON,
                        channel=channel,
                    )
                    _remember_processing(
                        processed,
                        logger,
                        card,
                        OUTCOME_QUEUED,
                        EXTERNAL_REASON,
                        profile_name,
                        channel=EXTERNAL_CHANNEL,
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
    profile_name = _profile_name(config, channel)
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
            channel.name == HH
            and record_profile
            and profile_key(record_profile) != profile_key(profile_name)
            and processed.find_record(
                card.title, card.company, card.url, profile_name
            )
        ):
            logger.event(
                "retry_profile_mismatch",
                message=(
                    "Запись вакансии сделана другим резюме и по текущему резюме запись уже есть: "
                    "повтор пропущен, чтобы не отправить отклик второй раз"
                ),
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
        _remember_interrupted(processed, logger, stats, card, config, channel)
        raise
    _mark_invited_resume(processed, logger, config, channel, card, skipped_before, stats)
    skipped = stats.skipped[skipped_before:]
    outcome = _outcome(counters, _counters(stats))
    if outcome:
        _remember_processing(
            processed,
            logger,
            card,
            outcome,
            _outcome_reason(stats, counters, outcome),
            _profile_name(config, channel),
            channel=outcome_channel or channel.name,
            archived=_outcome_archived(skipped),
        )


def _remember_interrupted(
    processed: ProcessedStore,
    logger: RunLogger,
    stats: RunStats,
    card: VacancyCard,
    config: AppConfig,
    channel: Channel,
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
        _profile_name(config, channel),
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
) -> None:
    """Сверяет статусы уже отправленных откликов со страницей откликов Habr.

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
                    "",
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
            profile=_profile_name(config, channel),
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
        _save_problem_screenshot(page, config, logger, "captcha")
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

    if details.blocker and not other_resume:
        queue(stats, logger, details, details.blocker, "признак на странице вакансии")
        # Отклик на сайте компании — отдельный канал отчёта, а не канал поиска.
        return EXTERNAL_CHANNEL if details.blocker == "external_form" else None

    if not other_resume and stats.sent_count >= config.pipeline.limit_per_run:
        stats.stop_reason = f"достигнут лимит откликов за запуск ({config.pipeline.limit_per_run})"
        log.info(f"лимит откликов достигнут ({stats.sent_count}), вакансия не обрабатывается")
        return

    try:
        decision = llm.decide(profile, details)
    except LLMError as exc:
        _record_error(stats, logger, page, config, details, exc)
        return

    llm_reason = ""
    llm_reason_source = ""
    # Вердикт приложения: Decide API даёт вероятности, порог применяем мы.
    # Ровно порог — не «выше порога»: такая вакансия пропускается.
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
        log.info(f"LLM-вердикт: {llm_reason}")

    if not fits:
        stats.skipped.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=llm_reason,
                probability=decision.probability,
            )
        )
        log.info(f"вакансия не подошла: {llm_reason}")
        return

    if other_resume:
        # Вакансия подходит, но отклик по ней уже ушёл другим резюме. Не спамим
        # работодателя: ждём ответа по другому резюме, поэтому статус queued.
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

    needs_project_links = requires_project_links(details)

    try:
        generation = llm.generate(
            profile,
            details,
            config.llm.cover_letter.max_length,
            signature=config.llm.cover_letter.signature,
        )
    except LLMError as exc:
        _record_error(stats, logger, page, config, details, exc)
        return

    if generation.verdict != "подходит":
        # Приоритет у Decision-вызова, расхождение только фиксируем в логе.
        logger.event(
            "generation_verdict_mismatch",
            url=details.url,
            decision_probability=decision.probability,
            llm_verdict=generation.verdict,
            llm_reason=generation.reason,
        )

    if not generation.cover_letter:
        cause = _generation_reason_text(generation.reason)
        queue(stats, logger, details, "empty_cover_letter", generation.reason, reason=cause)
        return


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
        queue(stats, logger, details, "project_links", generation.reason, reason=reason)
        return

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
    log.info(f"LLM-вердикт: {generation.reason or '—'}")
    if generation.greeting_added:
        if generation.required_opening:
            log.info(
                f"приветствие добавлено после обязательного начала "
                f"«{generation.required_opening}»"
            )
        else:
            log.info("в письме не было приветствия, добавил «Здравствуйте!»")

    # Пауза перед действием на сайте: только перед отправкой отклика. Вакансии,
    # которые не подошли по оценке LLM, её не ждут.
    _pause(config, logger, "перед отправкой отклика")

    captcha_waiter = _captcha_waiter(
        page, config, logger, details, site=channel.site_label
    )
    try:
        result = channel.respond(page, generation.cover_letter, captcha_waiter=captcha_waiter)
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
        return

    status = result.status
    log.event("respond", url=details.url, status=status, reason=result.reason)

    if status == STATUS_SENT:
        stats.sent.append(
            SentVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                probability=decision.probability,
                reason=generation.reason,
            )
        )
        log.info(f"отклик отправлен (всего {stats.sent_count} из {config.pipeline.limit_per_run})")
        _pause(config, logger, "после отправки отклика")
        return

    if status == STATUS_REJECTED:
        # Текущего резюме нет в форме отклика: им уже откликались, значит по
        # вакансии пришёл отказ. Лимит откликов не тратим.
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
        return

    if status == STATUS_ALREADY_RESPONDED:
        # Форма отклика уже закрыта: отклик ушёл раньше (нашим прошлым запуском
        # или руками). В файл дня — статус sent с причиной, лимит не тратим.
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
        return

    if status == STATUS_CAPTCHA:
        _save_problem_screenshot(page, config, logger, "captcha")
        queue(stats, logger, details, "captcha", "капча в форме отклика")
        stats.stop_reason = "Капча при отправке отклика — откликнитесь вручную"
        _notify_captcha_stop(
            config, logger, details, captcha_waiter, site=channel.site_label
        )
        log.info(f"{channel.site_label} показал капчу при отправке отклика, запуск остановлен.")
        return

    if status in ("test", "questionnaire", "external_form"):
        queue(stats, logger, details, status, "в форме отклика")
        return

    if status == STATUS_NO_FORM:
        _save_problem_screenshot(page, config, logger, "respond-no-form")
        logger.event(
            "respond_state",
            url=details.url,
            vacancy_id=vacancy_id(details.url),
            status=status,
            reason=result.reason,
            **channel.response_state(page),
        )
        queue(
            stats,
            logger,
            details,
            "no_form",
            "не найдена форма отклика",
            reason=result.reason,
        )
        return

    if status == STATUS_UNKNOWN:
        _save_problem_screenshot(page, config, logger, "respond-unknown")
        # Что именно показал сайт — без письма и персональных данных.
        logger.event(
            "respond_state",
            url=details.url,
            vacancy_id=vacancy_id(details.url),
            status=status,
            reason=result.reason,
            **channel.response_state(page),
        )
    queue(
        stats,
        logger,
        details,
        "unknown",
        f"статус отклика: {status or STATUS_UNKNOWN}",
        reason=result.reason,
    )


def _queue(
    stats: RunStats,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails,
    code: str,
    note: str = "",
    *,
    reason: str = "",
    channel: Channel | None = None,
) -> None:
    """Отмечает вакансию как отложенную: отклик нужно отправить вручную.

    `reason` — реальная причина, которую показал сайт. Она важнее общей
    формулировки для кода: если сайт объяснил отказ, в отчёт идёт его текст.
    Общие тексты берутся из канала: у hh.ru и Habr они разные.
    """
    reasons = channel.queue_reasons if channel is not None else QUEUE_REASON_TEXTS
    text = reason.strip() or reasons.get(code, reasons.get("unknown", ""))
    log = _VacancyLog(logger, card.title, card.company, card.url)
    stats.queued.append(
        {
            "url": card.url,
            "title": card.title,
            "company": card.company,
            "reason": text,
            "queued_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    log.event("queued", url=card.url, title=card.title, code=code, reason=text, note=note)
    log.info(f"отложено, нужен ручной отклик: {text}")


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


def _save_problem_screenshot(
    page: Page,
    config: AppConfig,
    logger: RunLogger,
    kind: str,
) -> None:
    """Сохраняет скриншот проблемного состояния — по нему видно, что показал сайт.

    Диагностика не должна ронять запуск: любые сбои здесь только логируются.
    """
    path = config.data.paths.logs_dir / f"{logger.run_id}-{kind}.png"
    try:
        saved = save_screenshot(page, path)
        if saved:
            logger.event("screenshot", path=str(saved), kind=kind)
        else:
            logger.event("screenshot_error", kind=kind)
    except Exception as exc:  # noqa: BLE001 - отчёт важнее скриншота
        logger.info(f"Не удалось сохранить скриншот ({kind}): {exc}")


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
    log.info(f"ошибка: {message}")

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
        f"Отчёт за {stats.started_at:%Y-%m-%d %H:%M} по запросу {config.pipeline.resume.name}",
        report,
        recipient=settings.target(config.notifications.email),
    )


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


def _remember_processing(
    processed: ProcessedStore,
    logger: RunLogger,
    card: VacancyCard,
    outcome: str,
    reason: str = "",
    profile: str | None = None,
    channel: str = "",
    archived: bool = False,
) -> None:
    """Записывает вакансию в дневной отчёт, чтобы не проходить её снова.

    Для пропусков, отложенных и упавших вакансий пишется причина — по файлу
    дня видно, почему вакансию не взяли и что имеет смысл повторить.
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
