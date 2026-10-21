"""Основной цикл работы агента (раздел 3.4 требований)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, Page

from .browser import BrowserSession, CaptchaError, save_screenshot
from .config import AppConfig
from .hh_search import is_excluded, open_search_page
from .hh_vacancy import (
    STATUS_ALREADY_RESPONDED,
    STATUS_CAPTCHA,
    STATUS_NO_FORM,
    STATUS_SENT,
    STATUS_UNKNOWN,
    VacancyParseError,
    open_vacancy,
    respond,
)
from .llm import LLMClient, LLMError
from .logging_setup import RunLogger
from .models import RunError, SentVacancy, SkippedVacancy, VacancyCard, VacancyDetails
from .profile import Profile, load_profile
from .queue import REASON_TITLES, QueueError, QueueStore
from .report import RunStats, build_details, build_report, send_email, write_details


class SessionLostError(RuntimeError):
    """Сессия hh.ru перестала быть действительной во время работы."""


def run(config: AppConfig, logger: RunLogger) -> tuple[RunStats, Path]:
    """Выполняет полный цикл: поиск, оценка, отклики и отчёт."""
    stats = RunStats(started_at=datetime.now())
    details_path = config.logs_dir / f"{logger.run_id}.json"

    profile = load_profile(config.profile_path)
    queue = QueueStore(config.queue_path)

    with BrowserSession(config.browser, config.state_path, logger) as session:
        session.ensure_logged_in()
        page = session.new_page()
        try:
            with LLMClient(config.llm) as llm:
                _process_vacancies(page, config, profile, llm, queue, stats, logger)
        except SessionLostError as exc:
            stats.stop_reason = str(exc)
            logger.event("session_lost", error=str(exc))
        finally:
            page.close()

    stats.finished_at = datetime.now()
    write_details(details_path, build_details(stats, logger.run_id))

    report = build_report(stats, details_path)
    logger.info("")
    logger.info(report)

    email_sent, email_message = send_email(
        config.email,
        f"hh-agent: отчёт за {stats.started_at:%Y-%m-%d %H:%M}",
        report,
    )
    logger.event("report_email", sent=email_sent, message=email_message)
    if not email_sent:
        logger.info(f"Email-отчёт не отправлен: {email_message}")

    return stats, details_path


def _process_vacancies(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    queue: QueueStore,
    stats: RunStats,
    logger: RunLogger,
) -> None:
    search = config.search
    seen: set[str] = set()
    reviewed = 0

    for query in search.queries:
        if _should_stop(search.daily_limit, stats, logger):
            break

        for page_index in range(search.pages_limit):
            if _should_stop(search.daily_limit, stats, logger):
                break

            try:
                cards = open_search_page(page, search, query, page_index)
            except CaptchaError as exc:
                stats.stop_reason = f"капча на странице поиска ({query}, стр. {page_index + 1})"
                logger.event("captcha", query=query, page=page_index, url=page.url, error=str(exc))
                logger.info("hh.ru показал капчу на странице поиска, запуск остановлен.")
                break

            if not cards:
                logger.info(f"Запрос «{query}», страница {page_index + 1}: вакансий не найдено.")
                break

            logger.info(f"Запрос «{query}», страница {page_index + 1}: найдено {len(cards)} вакансий.")

            for card in cards:
                if _should_stop(search.daily_limit, stats, logger):
                    break
                if card.url in seen:
                    continue
                seen.add(card.url)

                reviewed += 1
                if reviewed > search.max_vacancies:
                    stats.stop_reason = (
                        f"просмотрено максимум вакансий за запуск ({search.max_vacancies})"
                    )
                    logger.info(stats.stop_reason)
                    break

                _process_vacancy(page, config, profile, llm, queue, stats, logger, card)

                if _is_login_page(page):
                    raise SessionLostError(
                        "hh.ru требует повторный вход: сессия больше не действительна"
                    )

            _pause(config, logger, "переход к следующей странице поиска")


def _process_vacancy(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    queue: QueueStore,
    stats: RunStats,
    logger: RunLogger,
    card: VacancyCard,
) -> None:
    excluded = is_excluded(card.title, config.search.exclude_keywords)
    if excluded:
        logger.event(
            "skipped_exclude_keyword",
            url=card.url,
            title=card.title,
            keyword=excluded,
        )
        stats.skipped.append(
            SkippedVacancy(
                title=card.title,
                company=card.company,
                url=card.url,
                reason=f"стоп-слово в названии: {excluded}",
            )
        )
        return

    try:
        details = open_vacancy(page, card)
    except CaptchaError as exc:
        _enqueue(queue, stats, logger, card, "unknown", f"капча: {exc}")
        stats.stop_reason = "капча на странице вакансии"
        logger.info("hh.ru показал капчу на странице вакансии, запуск остановлен.")
        return
    except (VacancyParseError, PlaywrightError) as exc:
        _record_error(stats, logger, page, config, card, exc)
        return

    _pause(config, logger, "после открытия вакансии")

    if details.already_responded:
        logger.event("skipped_already_responded", url=details.url, title=details.title)
        stats.skipped.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason="отклик уже отправлен",
            )
        )
        return

    if details.blocker:
        _enqueue(queue, stats, logger, details, details.blocker, "признак на странице вакансии")
        return

    try:
        decision = llm.decide(profile, details)
    except LLMError as exc:
        _record_error(stats, logger, page, config, details, exc)
        return

    logger.event(
        "decision",
        url=details.url,
        title=details.title,
        probability=decision.probability,
        threshold=decision.threshold,
        fits=decision.fits,
    )

    if not decision.fits:
        reason = f"P(подходит)={decision.probability:.2f} <= {decision.threshold:.2f}"
        stats.skipped.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=reason,
                probability=decision.probability,
            )
        )
        logger.info(f"Не подходит ({reason}): {details.title} — {details.company}")
        return

    try:
        generation = llm.generate(profile, details, config.cover_letter_max_length)
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
        _enqueue(queue, stats, logger, details, "empty_cover_letter", generation.reason)
        return

    logger.event(
        "cover_letter",
        url=details.url,
        verdict=generation.verdict,
        reason=generation.reason,
        letter_length=len(generation.cover_letter),
    )

    try:
        status = respond(page, generation.cover_letter)
    except PlaywrightError as exc:
        _record_error(stats, logger, page, config, details, exc)
        return

    logger.event("respond", url=details.url, status=status)

    if status == STATUS_SENT:
        stats.sent.append(
            SentVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                probability=decision.probability,
            )
        )
        logger.info(f"Отклик отправлен: {details.title} — {details.company}")
        _pause(config, logger, "после отправки отклика")
        return

    if status == STATUS_ALREADY_RESPONDED:
        stats.skipped.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason="отклик уже отправлен",
                probability=decision.probability,
            )
        )
        return

    if status == STATUS_CAPTCHA:
        _enqueue(queue, stats, logger, details, "unknown", "капча при отправке отклика")
        stats.stop_reason = "капча при отправке отклика"
        logger.info("hh.ru показал капчу при отправке отклика, запуск остановлен.")
        return

    if status in ("test", "questionnaire", "external_form"):
        _enqueue(queue, stats, logger, details, status, "в форме отклика")
        return

    if status == STATUS_NO_FORM:
        _enqueue(queue, stats, logger, details, "unknown", "не найдена форма отклика")
        return

    _enqueue(queue, stats, logger, details, "unknown", f"статус отклика: {status or STATUS_UNKNOWN}")


def _enqueue(
    queue: QueueStore,
    stats: RunStats,
    logger: RunLogger,
    card: VacancyCard | VacancyDetails,
    reason: str,
    note: str = "",
) -> bool:
    """Добавляет вакансию в очередь ручных действий."""
    try:
        record = queue.add(
            url=card.url,
            title=card.title,
            company=card.company,
            reason=reason,
        )
    except QueueError as exc:
        logger.event("queue_error", url=card.url, error=str(exc))
        logger.info(f"Не удалось записать вакансию в очередь: {exc}")
        return False

    if record is None:
        logger.event("queued_duplicate", url=card.url, reason=reason)
        return False

    stats.queued.append(record)
    logger.event("queued", url=card.url, title=card.title, reason=reason, note=note)
    logger.info(f"Отложено ({REASON_TITLES.get(reason, reason)}): {card.title} — {card.company}")
    return True


def _record_error(
    stats: RunStats,
    logger: RunLogger,
    page: Page,
    config: AppConfig,
    card: VacancyCard | VacancyDetails,
    exc: BaseException,
) -> None:
    message = f"{type(exc).__name__}: {exc}"
    stats.errors.append(RunError(url=card.url, title=card.title, message=message))
    logger.event("vacancy_error", url=card.url, title=card.title, error=message)
    logger.info(f"Ошибка при обработке «{card.title}»: {message}")

    screenshot = config.logs_dir / f"{logger.run_id}-error-{len(stats.errors)}.png"
    if save_screenshot(page, screenshot):
        logger.event("screenshot", url=card.url, path=str(screenshot))


def _should_stop(daily_limit: int, stats: RunStats, logger: RunLogger) -> bool:
    if stats.stop_reason:
        return True
    if stats.sent_count >= daily_limit:
        stats.stop_reason = f"достигнут дневной лимит откликов ({daily_limit})"
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
