"""Основной цикл работы агента (раздел 3.4 требований)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Callable

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
from .models import RunError, SentVacancy, SkippedVacancy, VacancyCard, VacancyDetails
from .profile import Profile, load_profile
from .processed import ProcessedError, ProcessedStore
from .report import RunStats, build_details, build_report, send_email, write_details
from .resume import boost_resume


class SessionLostError(RuntimeError):
    """Сессия hh.ru перестала быть действительной во время работы."""


OUTCOME_SENT = "sent"
OUTCOME_SKIPPED = "skipped"
OUTCOME_QUEUED = "queued"
OUTCOME_ERROR = "error"

# Вакансии, на которые нельзя откликнуться автоматически: что писать в отчёте за день.
QUEUE_REASON_TEXTS: dict[str, str] = {
    "empty_cover_letter": "Нужно заполнить сопроводительное письмо вручную",
    "test": "Для отклика на вакансию необходимо пройти тестирование",
    "questionnaire": "Для отклика на вакансию необходимо заполнить анкету",
    "external_form": "Отклик нужно отправить на сайте работодателя",
    "captcha": "hh.ru просит пройти капчу: откликнитесь вручную",
    "unknown": "hh.ru не подтвердил отправку отклика — проверьте вакансию вручную",
}


class _VacancyLog:
    """Строки журнала по вакансии: «Название — Компания (id=123): событие»."""

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
        vid = vacancy_id(url)
        if vid:
            self.label += f" (id={vid})"

    def info(self, message: str) -> None:
        self._logger.info(f"{self.label}: {message}")

    def event(self, name: str, **fields: object) -> None:
        self._logger.event(name, vacancy=self.label, **fields)


def run(config: AppConfig, logger: RunLogger) -> tuple[RunStats, Path]:
    """Выполняет полный цикл: поиск, оценка, отклики и отчёт."""
    stats = RunStats(started_at=datetime.now())
    details_path = config.logs_dir / f"{logger.run_id}.json"

    profile = load_profile(config.profile_path)

    with BrowserSession(config.browser, config.state_path, logger) as session:
        session.ensure_logged_in()
        page = session.new_page()
        try:
            boost_resume(page, config, logger)
            with LLMClient(config.llm) as llm:
                _process_vacancies(page, config, profile, llm, stats, logger)
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
    write_details(details_path, build_details(stats, logger.run_id))

    report = build_report(stats, details_path)
    logger.info("")
    logger.info_plain(build_report(stats, None))
    logger.info(f"Отчёт сохранён в файле: {details_path}")

    email_sent, email_message = send_email(
        config.email,
        f"hh-agent: отчёт за {stats.started_at:%Y-%m-%d %H:%M}",
        report,
    )
    logger.event("report_email", sent=email_sent, message=email_message)
    if not email_sent:
        logger.info(f"Email-отчёт не отправлен: {email_message}")

    logger.info(_final_message(stats, config))

    return stats, details_path


def _process_vacancies(
    page: Page,
    config: AppConfig,
    profile: Profile,
    llm: LLMClient,
    stats: RunStats,
    logger: RunLogger,
) -> None:
    search = config.search
    seen: set[str] = set()
    reviewed = 0
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

    for query in search.queries:
        if _should_stop(search.limit_per_run, stats, logger):
            break

        for page_index in range(search.pages_limit):
            if _should_stop(search.limit_per_run, stats, logger):
                break

            logger.info(f"Поиск «{query}», страница {page_index + 1}: открываю страницу...")
            try:
                cards, total = open_search_page(page, search, query, page_index)
            except CaptchaError as exc:
                stats.stop_reason = f"капча на странице поиска ({query}, стр. {page_index + 1})"
                logger.event("captcha", query=query, page=page_index, url=page.url, error=str(exc))
                logger.info("hh.ru показал капчу на странице поиска, запуск остановлен.")
                break
            except PageLoadError as exc:
                logger.event("search_page_error", query=query, page=page_index, error=str(exc))
                logger.info(f"Не удалось открыть поиск ({query}): {exc}")
                break

            if not cards:
                if total:
                    logger.info(
                        f"Запрос «{query}», страница {page_index + 1}: вакансий нет "
                        f"(hh.ru всего нашёл {total})."
                    )
                else:
                    logger.info(f"Запрос «{query}», страница {page_index + 1}: вакансий не найдено.")
                break

            found_total = f" (hh.ru всего нашёл {total})" if total and page_index == 0 else ""
            logger.info(
                f"Запрос «{query}», страница {page_index + 1}: "
                f"найдено {len(cards)} вакансий{found_total}."
            )

            for card in cards:
                if _should_stop(search.limit_per_run, stats, logger):
                    break
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

                if _page_closed(page):
                    raise SessionLostError("окно браузера закрыто — запуск остановлен")
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
    stats: RunStats,
    logger: RunLogger,
    card: VacancyCard,
) -> None:
    log = _VacancyLog(logger, card.title, card.company, card.url)
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
        log.info(f"пропуск: стоп-слово «{excluded}» в названии")
        _remember(stats, f"вакансия «{card.title}» пропущена (стоп-слово «{excluded}»)")
        return

    excluded_company = is_company_excluded(card.company, config.search.exclude_companies)
    if excluded_company:
        logger.event(
            "skipped_exclude_company",
            url=card.url,
            title=card.title,
            company=card.company,
            keyword=excluded_company,
        )
        stats.skipped.append(
            SkippedVacancy(
                title=card.title,
                company=card.company,
                url=card.url,
                reason=f"исключённая компания: {excluded_company}",
            )
        )
        log.info(f"пропуск: компания «{card.company}» в списке исключений")
        _remember(stats, f"вакансия «{card.title}» пропущена (компания «{card.company}»)")
        return

    log.info("открываю вакансию")
    try:
        details = open_vacancy(page, card)
    except CaptchaError as exc:
        _save_problem_screenshot(page, config, logger, "captcha")
        _queue(stats, logger, card, "captcha", f"капча: {exc}")
        stats.stop_reason = "капча на странице вакансии"
        logger.info("hh.ru показал капчу на странице вакансии, запуск остановлен.")
        return
    except (VacancyParseError, PageLoadError, PlaywrightError) as exc:
        _record_error(stats, logger, page, config, card, exc)
        return

    _pause(config, logger, "после открытия вакансии")

    excluded_company = is_company_excluded(details.company, config.search.exclude_companies)
    if excluded_company:
        logger.event(
            "skipped_exclude_company",
            url=details.url,
            title=details.title,
            company=details.company,
            keyword=excluded_company,
        )
        stats.skipped.append(
            SkippedVacancy(
                title=details.title,
                company=details.company,
                url=details.url,
                reason=f"исключённая компания: {excluded_company}",
            )
        )
        log.info(f"пропуск: компания «{details.company}» в списке исключений")
        _remember(stats, f"вакансия «{details.title}» пропущена (компания «{details.company}»)")
        return

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
        log.info("пропуск: отклик уже отправлен")
        _remember(stats, f"вакансия «{details.title}» пропущена (отклик уже отправлен)")
        return

    if details.blocker:
        _queue(stats, logger, details, details.blocker, "признак на странице вакансии")
        return

    if stats.sent_count >= config.search.limit_per_run:
        stats.stop_reason = f"достигнут лимит откликов за запуск ({config.search.limit_per_run})"
        log.info(f"лимит откликов достигнут ({stats.sent_count}), вакансия не обрабатывается")
        _remember(stats, f"вакансия «{details.title}» пропущена (лимит откликов)")
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
        f"LLM-решение: «{verdict}» (порог {decision.threshold:.2f}, {probabilities})"
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
        _remember(stats, f"вакансия «{details.title}» не подходит ({llm_reason})")
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
    )
    log.info(f"LLM-вердикт: {generation.reason or '—'}")
    if generation.greeting_added:
        log.info("в письме не было приветствия, добавил «Здравствуйте!»")

    try:
        status = respond(page, generation.cover_letter, captcha_waiter=_captcha_waiter(page, config, logger))
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
        _remember(stats, f"отклик на «{details.title}» отправлен")
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
        _save_problem_screenshot(page, config, logger, "captcha")
        _queue(stats, logger, details, "captcha", "капча в форме отклика")
        stats.stop_reason = "Капча при отправке отклика — откликнитесь вручную"
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
    _remember(stats, f"Вакансия «{card.title}» отложена ({reason})")


def _captcha_waiter(page: Page, config: AppConfig, logger: RunLogger) -> Callable[[], bool] | None:
    """Ждёт, пока человек решит капчу в открытом окне браузера.

    В headless-режиме окна нет и ждать некому — возвращаем None, чтобы ответ
    ушёл в отложенные с понятной причиной.
    """
    seconds = config.browser.captcha_wait_seconds
    if seconds <= 0 or config.browser.headless:
        return None

    def wait() -> bool:
        logger.event("captcha_wait", seconds=seconds, url=page.url)
        logger.info(
            "hh.ru просит пройти капчу: введите текст с картинки в открытом окне браузера "
            f"и нажмите «Отправить», жду до {seconds} с."
        )
        solved = wait_for_captcha_solved(page, seconds)
        logger.event("captcha_solved" if solved else "captcha_timeout", seconds=seconds)
        if solved:
            logger.info("Капча пройдена, продолжаю отклик.")
        else:
            logger.info("Капча не пройдена за отведённое время, отклик отложен.")
        return solved

    return wait


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
    _remember(stats, f"ошибка при обработке «{card.title}»")

    screenshot = config.logs_dir / f"{logger.run_id}-error-{len(stats.errors)}.png"
    if save_screenshot(page, screenshot):
        logger.event("screenshot", url=card.url, path=str(screenshot))


def _remember(stats: RunStats, text: str) -> None:
    """Запоминает последнее действие — попадает в детали отчёта (logs/<run_id>.json)."""
    stats.last_action = text


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


def _counters(stats: RunStats) -> tuple[int, int, int, int]:
    """Счётчики результата: отправлено, пропущено, в очередь, ошибки."""
    return (stats.sent_count, len(stats.skipped), len(stats.queued), len(stats.errors))


def _outcome(
    before: tuple[int, int, int, int],
    after: tuple[int, int, int, int],
) -> str | None:
    """Результат обработки вакансии по изменению счётчиков.

    None — вакансия не обрабатывалась (например, сработал лимит откликов),
    такие вакансии не запоминаем, чтобы пройти их в следующем запуске.
    """
    if after[0] > before[0]:
        return OUTCOME_SENT
    if after[3] > before[3]:
        return OUTCOME_ERROR
    if after[2] > before[2]:
        return OUTCOME_QUEUED
    if after[1] > before[1]:
        return OUTCOME_SKIPPED
    return None


def _outcome_reason(
    stats: RunStats,
    before: tuple[int, int, int, int],
    outcome: str,
) -> str:
    """Причина результата для файла дня: из последней записи skipped/queued."""
    if outcome == OUTCOME_SKIPPED and len(stats.skipped) > before[1]:
        return stats.skipped[-1].reason
    if outcome == OUTCOME_QUEUED and len(stats.queued) > before[2]:
        return str(stats.queued[-1].get("reason", ""))
    return ""


def _remember_processing(
    processed: ProcessedStore,
    logger: RunLogger,
    card: VacancyCard,
    outcome: str,
    reason: str = "",
) -> None:
    """Запоминает вакансию, чтобы не проходить её в следующих запусках.

    Для пропусков и отложенных вакансий пишется причина — по файлу дня потом
    видно, почему вакансию не взяли в работу.
    """
    if outcome in (OUTCOME_SKIPPED, OUTCOME_QUEUED) and not reason:
        logger.event("reason_missing", url=card.url, title=card.title, status=outcome)
    try:
        processed.add(
            url=card.url,
            title=card.title,
            company=card.company,
            status=outcome,
            reason=reason,
        )
    except ProcessedError as exc:
        logger.event("processed_error", url=card.url, error=str(exc))
        logger.info(f"Не удалось сохранить память об обработанной вакансии: {exc}")


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
