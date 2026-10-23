"""Основной цикл работы агента (раздел 3.4 требований)."""

from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, Page

from .browser import (
    BrowserError,
    BrowserSession,
    CaptchaError,
    PageLoadError,
    save_screenshot,
    wait_for_captcha_solved,
)
from .config import FIT_LABEL, NOT_FIT_LABEL, AppConfig
from .hh_search import (
    is_company_excluded,
    is_excluded,
    open_search_page,
    pages_plan,
    vacancy_id,
    vacancy_keys,
)
from .hh_vacancy import (
    STATUS_ALREADY_RESPONDED,
    STATUS_CAPTCHA,
    STATUS_NO_FORM,
    STATUS_SENT,
    STATUS_UNKNOWN,
    VacancyParseError,
    open_vacancy,
    respond,
    response_state,
)
from .llm import LLMClient, LLMError
from .logging_setup import RunLogger
from .models import (
    ALREADY_RESPONDED_REASON,
    REJECTED_REASON,
    STATUS_ERROR as OUTCOME_ERROR,
    STATUS_EXCLUDED as OUTCOME_EXCLUDED,
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
from .processed import ProcessedError, ProcessedStore
from .report import RunStats, build_report, send_email
from .resume import boost_resume


class SessionLostError(RuntimeError):
    """Сессия hh.ru перестала быть действительной во время работы."""


# `--retry` без значения: статусы для повтора берём из retry.statuses.
RETRY_ALL = "all"

# Вакансии, на которые нельзя откликнуться автоматически: что писать в отчёте за день.
QUEUE_REASON_TEXTS: dict[str, str] = {
    "empty_cover_letter": "Нужно заполнить сопроводительное письмо вручную",
    "test": "Для отклика на вакансию необходимо пройти тестирование",
    "questionnaire": "Для отклика на вакансию необходимо заполнить анкету",
    "external_form": "Отклик нужно отправить на сайте работодателя",
    "captcha": "hh.ru просит пройти капчу: откликнитесь вручную",
    "unknown": "hh.ru не подтвердил отправку отклика — проверьте вакансию вручную",
}


# Состояния страницы вакансии, при которых отклик не нужен: код → (статус
# записи, причина в отчёте за день). Приглашение и недоступная вакансия —
# `skipped`, отказ работодателя — отдельный статус `rejected`.
PAGE_STATUS_REASONS: dict[str, tuple[str, str]] = {
    "invited": (OUTCOME_SKIPPED, "Вас пригласили на эту вакансию"),
    "blocked": (OUTCOME_SKIPPED, "У вас нет доступа к этой вакансии"),
    "rejected": (OUTCOME_REJECTED, REJECTED_REASON),
}


class _VacancyLog:
    """Строки журнала по вакансии: «[id] Название — Компания: событие».

    В JSONL поле `vacancy` остаётся без номера, номер идёт отдельным полем
    `vacancy_id` — так отчёт читаем, а данные не дублируются.
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
        self.prefix = f"[{self.id}] {self.label}" if self.id else self.label

    def info(self, message: str) -> None:
        self._logger.info(f"{self.prefix}: {message}")

    def event(self, name: str, **fields: object) -> None:
        extra = {"vacancy_id": self.id} if self.id else {}
        self._logger.event(name, vacancy=self.label, **extra, **fields)


def run(
    config: AppConfig,
    logger: RunLogger,
    retry: str | None = None,
) -> tuple[RunStats, Path]:
    """Выполняет полный цикл: поиск (или повтор по отчётам), оценка, отклики и отчёт."""
    stats = RunStats(started_at=datetime.now())
    statuses = _retry_statuses(config, retry) if retry else ()
    processed = _open_report(config, logger)

    profile = load_profile(config.profile_path)

    with BrowserSession(config.browser, config.state_path, logger) as session:
        session.ensure_logged_in()
        page = session.new_page()
        try:
            boost_resume(page, config, logger)
            with LLMClient(config.llm) as llm:
                if statuses:
                    _retry_vacancies(
                        page, config, profile, llm, stats, logger, processed, statuses
                    )
                else:
                    _process_vacancies(page, config, profile, llm, stats, logger, processed)
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


def _open_report(config: AppConfig, logger: RunLogger) -> ProcessedStore:
    """Готовит дневные отчёты: удаляет старые файлы и читает историю вакансий."""
    processed = ProcessedStore(config.report_dir)
    removed = processed.prune(config.processed_retention_days)
    if removed:
        logger.event(
            "processed_pruned",
            count=len(removed),
            days=config.processed_retention_days,
            files=removed,
        )
    known = processed.load()
    logger.event("processed_loaded", count=known, path=str(processed.directory))
    if processed.dropped_copies:
        # В старых файлах оставались копии записи, которая уже есть в свежем:
        # одна вакансия — одна запись, копии сняли при загрузке истории.
        logger.event("processed_copies_dropped", count=processed.dropped_copies)
        logger.info(
            f"Убрал копии записей из старых файлов отчёта: {processed.dropped_copies}. "
            "Вакансия остаётся только в файле последней обработки."
        )
    return processed


def _retry_statuses(config: AppConfig, retry: str) -> tuple[str, ...]:
    """Статусы для повтора: набор из retry.statuses или явно указанный в CLI."""
    if retry == RETRY_ALL:
        return config.retry.statuses
    return (retry,)


def _process_vacancies(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    stats: RunStats,
    logger: RunLogger,
    processed: ProcessedStore,
) -> None:
    search = config.search
    seen: set[str] = set()
    reviewed = 0

    for number, query in enumerate(search.queries):
        if _should_stop(search.limit_per_run, stats, logger):
            break
        _check_new_day(logger, processed)
        if number:
            # Новый запрос: резюме поднимаем только если по кэшу уже пора.
            boost_resume(page, config, logger)

        # Глубина: pages_limit из конфига, а если он не задан — считаем страницы
        # по числу найденных после открытия первой страницы этого запроса.
        logger.info(f"Начинаю поиск на hh.ru по запросу «{query}»...")
        pages = search.pages_limit
        page_index = 0
        while pages is None or page_index < pages:
            if _should_stop(search.limit_per_run, stats, logger):
                break

            try:
                cards, total = open_search_page(page, search, query, page_index)
            except CaptchaError as exc:
                stats.stop_reason = f"капча на странице поиска ({query}, стр. {page_index + 1})"
                logger.event("captcha", query=query, page=page_index, url=page.url, error=str(exc))
                logger.info("hh.ru показал капчу на странице поиска, запуск остановлен.")
                _notify_captcha(
                    config,
                    logger,
                    note=(
                        f"Капча на странице поиска ({query}, стр. {page_index + 1}). "
                        "Запуск остановлен — пройдите капчу вручную."
                    ),
                    url=page.url,
                )
                break
            except PageLoadError as exc:
                logger.event("search_page_error", query=query, page=page_index, error=str(exc))
                logger.info(f"Не удалось открыть поиск ({query}): {exc}")
                break

            if not cards:
                logger.info(
                    f"Обработка результатов запроса «{query}»: "
                    f"на странице {page_index + 1} вакансий нет"
                )
                break

            if pages is None:
                pages = pages_plan(total, search.per_page)
                logger.event(
                    "search_pages_plan",
                    query=query,
                    total=total,
                    per_page=search.per_page,
                    pages=pages,
                )
                if total:
                    logger.info(
                        f"По запросу «{query}» найдено {total} "
                        f"{_plural(total, 'вакансию', 'вакансии', 'вакансий')}, "
                        f"разделяю обработку на {pages} "
                        f"{_plural(pages, 'страница', 'страницы', 'страниц')}"
                    )
                else:
                    logger.info(
                        f"Запрос «{query}»: число найденных неизвестно, иду до первой "
                        f"пустой страницы (не больше {pages})"
                    )

            logger.info(
                f"Обработка {len(cards)} "
                f"{_plural(len(cards), 'вакансия', 'вакансии', 'вакансий')} "
                f"из результата запроса «{query}»: страница {page_index + 1} из {pages}"
            )

            for card in cards:
                if _should_stop(search.limit_per_run, stats, logger):
                    break
                _check_new_day(logger, processed)
                keys = vacancy_keys(card.title, card.company, card.url)
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
                previous = processed.find(card.title, card.company, card.url)
                if previous:
                    logger.event(
                        "skipped_processed",
                        url=card.url,
                        title=card.title,
                        processed_at=previous,
                    )
                    _VacancyLog(logger, card.title, card.company, card.url).info(
                        f"уже обрабатывалась {previous}, пропускаю"
                    )
                    continue

                reviewed += 1
                if reviewed > search.max_vacancies:
                    stats.stop_reason = (
                        f"просмотрено максимум вакансий за запуск ({search.max_vacancies})"
                    )
                    logger.info(stats.stop_reason)
                    break

                _process_and_remember(
                    page, config, profile, llm, stats, logger, processed, card
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
) -> None:
    """Повторяет вакансии из дневных отчётов по прямой ссылке, без поиска.

    Выдача hh.ru каждый раз одна и та же, поэтому идти по ней ради пары
    вакансий дорого: берём ссылки из отчётов и открываем их сразу.
    """
    records = processed.pending(statuses)
    logger.event("retry_selected", statuses=list(statuses), count=len(records))
    if not records:
        logger.info(
            "Повторов нет: в отчётах нет вакансий со статусами " + ", ".join(statuses) + "."
        )
        return

    logger.info(
        f"Повторная обработка по отчётам: {len(records)} вакансий "
        f"({', '.join(statuses)}), поиск не используется."
    )
    for record in records:
        if _should_stop(config.search.limit_per_run, stats, logger):
            break
        _check_new_day(logger, processed)
        if stats.reviewed >= config.search.max_vacancies:
            stats.stop_reason = (
                f"просмотрено максимум вакансий за запуск ({config.search.max_vacancies})"
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
        _process_and_remember(page, config, profile, llm, stats, logger, processed, card)
        _ensure_session_alive(page)
        _pause(config, logger, "перед следующей вакансией из отчёта")


def _process_and_remember(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    stats: RunStats,
    logger: RunLogger,
    processed: ProcessedStore,
    card: VacancyCard,
) -> None:
    """Обрабатывает вакансию и записывает результат в дневной отчёт."""
    counters = _counters(stats)
    _process_vacancy(page, config, profile, llm, stats, logger, card)
    outcome = _outcome(counters, _counters(stats))
    if outcome:
        _remember_processing(
            processed,
            logger,
            card,
            outcome,
            _outcome_reason(stats, counters, outcome),
        )


def _ensure_session_alive(page: Page) -> None:
    """Роняет запуск, если окно закрыто или hh.ru требует повторный вход."""
    if _page_closed(page):
        raise SessionLostError("окно браузера закрыто — запуск остановлен")
    if _is_login_page(page):
        raise SessionLostError("hh.ru требует повторный вход: сессия больше не действительна")


def _process_vacancy(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    stats: RunStats,
    logger: RunLogger,
    card: VacancyCard,
) -> None:
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
        details = open_vacancy(page, card)
    except CaptchaError as exc:
        _save_problem_screenshot(page, config, logger, "captcha")
        _queue(stats, logger, card, "captcha", f"капча: {exc}")
        stats.stop_reason = "капча на странице вакансии"
        _notify_captcha(
            config,
            logger,
            card,
            "Капча на странице вакансии. Запуск остановлен — откликнитесь вручную.",
        )
        logger.info("hh.ru показал капчу на странице вакансии, запуск остановлен.")
        return
    except (VacancyParseError, PageLoadError, PlaywrightError) as exc:
        _record_error(stats, logger, page, config, card, exc)
        return

    _pause(config, logger, "после открытия вакансии")

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
            )
        )
        if outcome == OUTCOME_REJECTED:
            log.info("на отклик пришёл отказ, вакансия пройдена")
        else:
            log.info(f"пропуск: {reason.lower()}")
        return

    if details.blocker:
        _queue(stats, logger, details, details.blocker, "признак на странице вакансии")
        return

    if stats.sent_count >= config.search.limit_per_run:
        stats.stop_reason = f"достигнут лимит откликов за запуск ({config.search.limit_per_run})"
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
    verdict = FIT_LABEL if decision.fits else NOT_FIT_LABEL
    if not decision.fits:
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
        fits=decision.fits,
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

    if not decision.fits:
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

    try:
        generation = llm.generate(
            profile,
            details,
            config.cover_letter_max_length,
            signature=config.cover_letter_signature,
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
        _queue(stats, logger, details, "empty_cover_letter", generation.reason)
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

    captcha_waiter = _captcha_waiter(page, config, logger, details)
    try:
        status = respond(page, generation.cover_letter, captcha_waiter=captcha_waiter)
    except PlaywrightError as exc:
        _record_error(stats, logger, page, config, details, exc)
        return

    log.event("respond", url=details.url, status=status)

    if status == STATUS_SENT:
        stats.sent.append(
            SentVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                probability=decision.probability,
            )
        )
        log.info(f"отклик отправлен (всего {stats.sent_count} из {config.search.limit_per_run})")
        _pause(config, logger, "после отправки отклика")
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
        log.info("hh.ru уже принял отклик, помечаю вакансию отправленной")
        return

    if status == STATUS_CAPTCHA:
        _save_problem_screenshot(page, config, logger, "captcha")
        _queue(stats, logger, details, "captcha", "капча в форме отклика")
        stats.stop_reason = "Капча при отправке отклика — откликнитесь вручную"
        _notify_captcha_stop(config, logger, details, captcha_waiter)
        logger.info("hh.ru показал капчу при отправке отклика, запуск остановлен.")
        return

    if status in ("test", "questionnaire", "external_form"):
        _queue(stats, logger, details, status, "в форме отклика")
        return

    if status == STATUS_NO_FORM:
        _save_problem_screenshot(page, config, logger, "respond-no-form")
        logger.event("respond_state", url=details.url, status=status, **response_state(page))
        _queue(stats, logger, details, "unknown", "не найдена форма отклика")
        return

    if status == STATUS_UNKNOWN:
        _save_problem_screenshot(page, config, logger, "respond-unknown")
        # Что именно показал hh.ru — без письма и персональных данных.
        logger.event("respond_state", url=details.url, status=status, **response_state(page))
    _queue(stats, logger, details, "unknown", f"статус отклика: {status or STATUS_UNKNOWN}")


def _queue(
    stats: RunStats,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails,
    code: str,
    note: str = "",
) -> None:
    """Отмечает вакансию как отложенную: отклик нужно отправить вручную."""
    reason = QUEUE_REASON_TEXTS.get(code, QUEUE_REASON_TEXTS["unknown"])
    log = _VacancyLog(logger, card.title, card.company, card.url)
    stats.queued.append(
        {
            "url": card.url,
            "title": card.title,
            "company": card.company,
            "reason": reason,
            "queued_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    log.event("queued", url=card.url, title=card.title, code=code, reason=reason, note=note)
    log.info(f"отложено, нужен ручной отклик: {reason.lower()}")


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
    ) -> None:
        self._page = page
        self._config = config
        self._logger = logger
        self._card = card
        self.seconds = config.browser.captcha_wait_seconds
        self.notified = False

    def __call__(self) -> bool:
        seconds = self.seconds
        self._logger.event("captcha_wait", seconds=seconds, url=self._page.url)
        self._logger.info(
            "hh.ru просит пройти капчу: введите текст с картинки в открытом окне браузера "
            f"и нажмите «Отправить», жду до {seconds} с."
        )
        if not self.notified:
            _notify_captcha(
                self._config,
                self._logger,
                self._card,
                f"Капча в форме отклика: жду решения до {seconds} с. "
                "Введите текст с картинки в открытом окне браузера и нажмите «Отправить».",
                url=self._page.url,
            )
            self.notified = True
        solved = wait_for_captcha_solved(self._page, seconds)
        self._logger.event("captcha_solved" if solved else "captcha_timeout", seconds=seconds)
        if solved:
            self._logger.info("Капча пройдена, продолжаю отклик.")
        else:
            self._logger.info("Капча не пройдена за отведённое время, отклик отложен.")
        return solved


def _captcha_waiter(
    page: Page,
    config: AppConfig,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails | None = None,
) -> _CaptchaWaiter | None:
    """Ждёт, пока человек решит капчу в открытом окне браузера.

    В headless-режиме окна нет и ждать некому — возвращаем None, чтобы ответ
    ушёл в отложенные с понятной причиной.
    """
    if config.browser.captcha_wait_seconds <= 0 or config.browser.headless:
        return None
    return _CaptchaWaiter(page, config, logger, card)


def _notify_captcha_stop(
    config: AppConfig,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails | None,
    waiter: _CaptchaWaiter | None,
) -> None:
    """Письмо «капча остановила запуск» — только если о ней ещё не писали."""
    if waiter is not None and waiter.notified:
        return
    _notify_captcha(
        config,
        logger,
        card,
        "Капча в форме отклика. Запуск остановлен — откликнитесь вручную.",
    )


def _notify_captcha(
    config: AppConfig,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails | None = None,
    note: str = "",
    url: str = "",
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
            config.email,
            config.captcha_notify,
            title=title,
            company=company,
            url=url,
            note=note,
        )
    except Exception as exc:  # noqa: BLE001 - уведомление не должно ронять запуск
        sent, message = False, f"{type(exc).__name__}: {exc}"
    logger.event("email_notify", kind="captcha", sent=sent, message=message)
    if not sent:
        logger.info(f"Письмо о капче не отправлено: {message}")


def _save_problem_screenshot(
    page: Page,
    config: AppConfig,
    logger: RunLogger,
    kind: str,
) -> None:
    """Сохраняет скриншот проблемного состояния — по нему видно, что показал hh.ru.

    Диагностика не должна ронять запуск: любые сбои здесь только логируются.
    """
    path = config.logs_dir / f"{logger.run_id}-{kind}.png"
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

    screenshot = config.logs_dir / f"{logger.run_id}-error-{len(stats.errors)}.png"
    if save_screenshot(page, screenshot):
        logger.event("screenshot", url=card.url, path=str(screenshot))


def _send_report_email(config: AppConfig, report: str, stats: RunStats) -> tuple[bool, str]:
    """Письмо с отчётом: свой раздел notifications.report (вкл/адрес)."""
    settings = config.report_notify
    if not settings.enabled:
        return False, "письма с отчётом отключены (notifications.report.enabled: false)"
    return send_email(
        config.email,
        f"Отчёт за {stats.started_at:%Y-%m-%d %H:%M}",
        report,
        recipient=settings.target(config.email),
    )


def _final_message(stats: RunStats, config: AppConfig) -> str:
    """Итоговое сообщение о завершении работы."""
    message = (
        f"Работа приложения завершена, отправлено откликов: "
        f"{len(stats.sent)} из {config.search.limit_per_run}."
    )
    if stats.stop_reason:
        message += f" Причина остановки: {stats.stop_reason}."
    message += f" Сессия сохранена в {config.state_path}."
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
    if outcome == OUTCOME_SENT and len(stats.already_sent) > before[6]:
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
        processed.record(
            url=card.url,
            title=card.title,
            company=card.company,
            status=outcome,
            reason=reason,
        )
    except ProcessedError as exc:
        logger.event("processed_error", url=card.url, error=str(exc))
        logger.info(f"Не удалось сохранить память об обработанной вакансии: {exc}")


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
    seconds = config.search.delay.next()
    if seconds <= 0:
        return
    logger.event("pause", seconds=round(seconds, 2), reason=reason)
    time.sleep(seconds)


def _is_login_page(page: Page) -> bool:
    try:
        url = page.url.lower()
    except PlaywrightError:
        return False
    return "account/login" in url or url.rstrip("/").endswith("/login")


def _page_closed(page: Page) -> bool:
    try:
        return page.is_closed()
    except PlaywrightError:
        return True
