"""Тесты итогового сообщения и формата строк журнала."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from agent.config import AppConfig, NotificationConfig, load_config
from agent.logging_setup import RunLogger
from agent.models import SentVacancy, SkippedVacancy, VacancyCard
from agent.pipeline import (
    OUTCOME_ERROR,
    OUTCOME_EXCLUDED,
    OUTCOME_QUEUED,
    OUTCOME_SENT,
    OUTCOME_SKIPPED,
    QUEUE_REASON_TEXTS,
    _captcha_waiter,
    _counters,
    _final_message,
    _notify_captcha,
    _notify_captcha_stop,
    _outcome,
    _outcome_reason,
    _plural,
    _process_vacancies,
    _process_vacancy,
    _queue,
    _remember_processing,
    _retry_statuses,
    _retry_vacancies,
    _save_problem_screenshot,
    _send_report_email,
    _VacancyLog,
)
from agent.processed import ProcessedStore
from agent.profile import Profile
from agent.report import RunStats


def make_config(tmp_path: Path) -> AppConfig:
    data = {
        "search_query": "Java",
        "area": "Москва",
        "limit_per_run": 2,
        "delay_between_actions": {"from": 0, "to": 0},
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return load_config(path)


def make_stats(**kwargs: object) -> RunStats:
    return RunStats(started_at=datetime(2026, 10, 21, 9, 0), **kwargs)  # type: ignore[arg-type]


def test_final_message_without_actions(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    message = _final_message(make_stats(), config)

    assert message == (
        "Работа приложения завершена, отправлено откликов: 0 из 2. "
        f"Сессия сохранена в {config.state_path}."
    )


def test_final_message_has_no_last_vacancy(tmp_path: Path) -> None:
    """В итоге — счётчик и причина остановки, без названия последней вакансии."""
    config = make_config(tmp_path)
    stats = make_stats(
        stop_reason="достигнут лимит откликов за запуск (1)",
        sent=[
            SentVacancy(
                title="Java - разработчик",
                company="СберЛизинг",
                url="https://hh.ru/vacancy/1",
                probability=0.93,
            )
        ],
    )

    message = _final_message(stats, config)

    assert message == (
        "Работа приложения завершена, отправлено откликов: 1 из 2. "
        "Причина остановки: достигнут лимит откликов за запуск (1). "
        f"Сессия сохранена в {config.state_path}."
    )
    assert "Java" not in message


def test_vacancy_log_prefixes_lines_and_events(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path, run_id="run")
    log = _VacancyLog(logger, "Java-разработчик", "Ozon", "https://hh.ru/vacancy/777")

    log.info("открываю вакансию")
    log.event("respond", status="sent")
    logger.close()

    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]

    assert events[0]["event"] == "info"
    assert events[0]["message"] == "[777] Java-разработчик — Ozon: открываю вакансию"
    assert events[1]["event"] == "respond"
    assert events[1]["vacancy"] == "Java-разработчик — Ozon"
    assert events[1]["vacancy_id"] == "777"


def test_vacancy_log_without_company(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path, run_id="run2")
    log = _VacancyLog(logger, "Java-разработчик")

    log.info("открываю вакансию")
    logger.close()

    event = json.loads(logger.events_path.read_text(encoding="utf-8").splitlines()[0])

    assert event["message"] == "Java-разработчик: открываю вакансию"


def test_plural_agrees_with_number() -> None:
    """Формы слов в логе согласуются с числом: 1 страница, 2 страницы, 5 страниц."""
    forms = ("страница", "страницы", "страниц")

    assert [_plural(count, *forms) for count in (1, 2, 4, 5, 11, 21, 22, 25, 114)] == [
        "страница",
        "страницы",
        "страницы",
        "страниц",
        "страниц",
        "страница",
        "страницы",
        "страниц",
        "страниц",
    ]


def test_search_plan_and_page_lines(tmp_path: Path, monkeypatch, capsys) -> None:
    """План страниц и начало обработки печатаются понятными фразами."""
    path = tmp_path / "config.yaml"
    path.write_text(
        json.dumps(
            {
                "search_query": "Java developer",
                "area": "Москва",
                "limit_per_run": 1,
                "max_vacancies": 5,
                "per_page": 100,
                "delay_between_actions": {"from": 0, "to": 0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    card = VacancyCard(title="Java-разработчик", company="Ozon", url="https://hh.ru/vacancy/1")
    pages = [[card], []]
    monkeypatch.setattr(
        "agent.pipeline.open_search_page", lambda *args, **kwargs: (pages.pop(0), 545)
    )
    monkeypatch.setattr("agent.pipeline._process_and_remember", lambda *args, **kwargs: None)
    monkeypatch.setattr("agent.pipeline._ensure_session_alive", lambda page: None)

    logger = RunLogger(tmp_path / "logs", run_id="search-lines")
    _process_vacancies(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=make_stats(),
        logger=logger,
        processed=ProcessedStore(tmp_path / "report", today=date(2026, 10, 21)),
    )
    logger.close()

    out = capsys.readouterr().out
    plan = "Запрос «Java developer» на hh.ru нашёл 545 вакансий: 6 страниц по 100"
    page = "Обработка результатов запроса «Java developer»: 1 вакансия на странице 1"

    assert plan in out
    assert page in out

    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    messages = [event.get("message") for event in events]
    assert plan in messages
    assert page in messages


def test_outcome_detects_sent() -> None:
    assert _outcome((0, 0, 0, 0, 0), (1, 0, 0, 0, 0)) == OUTCOME_SENT


def test_outcome_prefers_sent_over_skipped() -> None:
    assert _outcome((0, 1, 0, 0, 0), (1, 2, 0, 0, 0)) == OUTCOME_SENT


def test_outcome_detects_queue_and_error() -> None:
    assert _outcome((0, 0, 0, 0, 0), (0, 0, 1, 0, 0)) == OUTCOME_QUEUED
    assert _outcome((0, 0, 0, 0, 0), (0, 0, 0, 1, 0)) == OUTCOME_ERROR
    assert _outcome((0, 0, 0, 0, 0), (0, 1, 0, 0, 0)) == OUTCOME_SKIPPED
    assert _outcome((0, 0, 0, 0, 0), (0, 0, 0, 0, 1)) == OUTCOME_EXCLUDED


def test_captcha_waiter_disabled_in_headless(tmp_path: Path) -> None:
    """В headless окна нет — ждать ручного решения капчи некому."""
    config = replace(make_config(tmp_path), browser=replace(make_config(tmp_path).browser, headless=True))
    page = _FakePage("https://hh.ru/vacancy/1")

    assert _captcha_waiter(page, config, RunLogger(tmp_path / "logs", run_id="h")) is None  # type: ignore[arg-type]


def test_captcha_waiter_logs_and_returns_result(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    logger = RunLogger(tmp_path / "logs", run_id="captcha")
    page = _FakePage("https://hh.ru/vacancy/1")
    monkeypatch.setattr("agent.pipeline.wait_for_captcha_solved", lambda page, seconds: True)

    waiter = _captcha_waiter(page, config, logger)  # type: ignore[arg-type]
    assert waiter is not None
    assert waiter() is True
    logger.close()

    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    names = [item["event"] for item in events]
    assert "captcha_wait" in names
    assert "captcha_solved" in names


def test_captcha_waiter_notifies_when_captcha_appears(tmp_path: Path, monkeypatch) -> None:
    """Капча в форме: одно письмо при появлении, без дубля на таймауте."""
    config = make_config(tmp_path)
    logger = RunLogger(tmp_path / "logs", run_id="captcha-email")
    page = _FakePage("https://hh.ru/vacancy/1")
    calls: list[dict[str, str]] = []

    def fake_notify(channel, settings, **kwargs):
        calls.append(kwargs)
        return True, "ok"

    monkeypatch.setattr("agent.pipeline.notify_captcha", fake_notify)
    monkeypatch.setattr("agent.pipeline.wait_for_captcha_solved", lambda page, seconds: False)

    waiter = _captcha_waiter(page, config, logger)  # type: ignore[arg-type]
    assert waiter is not None
    assert waiter() is False
    assert waiter.notified is True
    logger.close()

    assert len(calls) == 1
    assert calls[0]["url"] == "https://hh.ru/vacancy/1"
    assert "жду" in calls[0]["note"].lower()

    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    notifications = [item for item in events if item["event"] == "email_notify"]
    assert [item["sent"] for item in notifications] == [True]
    assert notifications[0]["kind"] == "captcha"


def test_captcha_waiter_notifies_only_once(tmp_path: Path, monkeypatch) -> None:
    """Повторная капча в той же попытке отклика письма не дублирует."""
    config = make_config(tmp_path)
    logger = RunLogger(tmp_path / "logs", run_id="captcha-once")
    page = _FakePage("https://hh.ru/vacancy/1")
    calls: list[dict[str, str]] = []

    monkeypatch.setattr(
        "agent.pipeline.notify_captcha",
        lambda channel, settings, **kwargs: (calls.append(kwargs), (True, "ok"))[1],
    )
    monkeypatch.setattr("agent.pipeline.wait_for_captcha_solved", lambda page, seconds: True)

    waiter = _captcha_waiter(page, config, logger)  # type: ignore[arg-type]
    assert waiter is not None
    assert waiter() is True
    assert waiter() is True
    logger.close()

    assert len(calls) == 1


def test_captcha_stop_does_not_repeat_notification(tmp_path: Path, monkeypatch) -> None:
    """Остановку из-за капчи не сообщаем вторым письмом."""
    config = make_config(tmp_path)
    logger = RunLogger(tmp_path / "logs", run_id="captcha-stop")
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        "agent.pipeline.notify_captcha",
        lambda channel, settings, **kwargs: (calls.append(kwargs), (True, "ok"))[1],
    )
    monkeypatch.setattr("agent.pipeline.wait_for_captcha_solved", lambda page, seconds: False)

    waiter = _captcha_waiter(_FakePage("https://hh.ru/vacancy/1"), config, logger)  # type: ignore[arg-type]
    assert waiter is not None
    assert waiter() is False
    _notify_captcha_stop(config, logger, None, waiter)
    logger.close()

    assert len(calls) == 1


def test_captcha_stop_notifies_when_waiter_silent(tmp_path: Path, monkeypatch) -> None:
    """Без ожидания (headless / 0 секунд) об остановке сообщаем одним письмом."""
    config = make_config(tmp_path)
    logger = RunLogger(tmp_path / "logs", run_id="captcha-headless")
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        "agent.pipeline.notify_captcha",
        lambda channel, settings, **kwargs: (calls.append(kwargs), (True, "ok"))[1],
    )

    _notify_captcha_stop(config, logger, None, None)
    logger.close()

    assert len(calls) == 1
    assert "остановлен" in calls[0]["note"].lower()


def test_report_email_can_be_switched_off(tmp_path: Path) -> None:
    """Отчёт и капча настраиваются раздельно: письмо с отчётом можно выключить."""
    config = replace(make_config(tmp_path), report_notify=NotificationConfig(enabled=False))

    sent, message = _send_report_email(config, "отчёт", make_stats())

    assert sent is False
    assert "notifications.report.enabled" in message


def test_report_email_uses_own_recipient(tmp_path: Path, monkeypatch) -> None:
    config = replace(
        make_config(tmp_path),
        report_notify=NotificationConfig(recipient="report@example.com"),
    )
    captured: dict[str, str] = {}

    def fake_send_email(channel, subject, text, recipient="", **kwargs):
        captured["subject"] = subject
        captured["recipient"] = recipient
        return True, "письмо отправлено"

    monkeypatch.setattr("agent.pipeline.send_email", fake_send_email)

    sent, _ = _send_report_email(config, "отчёт", make_stats())

    assert sent is True
    assert captured["recipient"] == "report@example.com"
    assert captured["subject"] == "Отчёт за 2026-10-21 09:00"


def test_notify_captcha_uses_vacancy_and_swallows_errors(tmp_path: Path, monkeypatch) -> None:
    """В письмо уходят вакансия и ссылка, а сбой SMTP не роняет запуск."""
    config = make_config(tmp_path)
    logger = RunLogger(tmp_path / "logs", run_id="notify-fail")
    card = VacancyCard(
        title="Java-разработчик",
        company="Ozon",
        url="https://hh.ru/vacancy/777",
    )
    captured: dict[str, str] = {}

    def broken_notify(channel, settings, **kwargs):
        captured.update(kwargs)
        raise OSError("SMTP недоступен")

    monkeypatch.setattr("agent.pipeline.notify_captcha", broken_notify)

    _notify_captcha(config, logger, card, "Капча в форме отклика.")
    logger.close()

    assert captured["title"] == "Java-разработчик"
    assert captured["company"] == "Ozon"
    assert captured["url"] == "https://hh.ru/vacancy/777"

    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    notification = [item for item in events if item["event"] == "email_notify"][-1]
    assert notification["sent"] is False
    assert "OSError" in notification["message"]


class _FakePage:
    """Страница-заглушка: для ожидания капчи нужен url, для цикла — признак «жива»."""

    def __init__(self, url: str) -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False


def test_outcome_is_none_when_nothing_happened() -> None:
    """Сработал лимит откликов — вакансию не запоминаем, пройдём в следующий раз."""
    assert _outcome((1, 0, 0, 0, 0), (1, 0, 0, 0, 0)) is None


def test_outcome_reason_takes_skip_reason() -> None:
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    stats.skipped.append(
        SkippedVacancy(
            title="Java",
            company="Ozon",
            url="https://hh.ru/vacancy/1",
            reason="вероятность 0.08 не выше порога 0.80",
        )
    )

    assert _outcome_reason(stats, (0, 0, 0, 0, 0), OUTCOME_SKIPPED) == (
        "вероятность 0.08 не выше порога 0.80"
    )


def test_outcome_reason_takes_queued_reason() -> None:
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    stats.queued.append(
        {"url": "https://hh.ru/vacancy/1", "reason": "Для отклика нужно пройти тестирование"}
    )

    assert _outcome_reason(stats, (0, 0, 0, 0, 0), OUTCOME_QUEUED) == (
        "Для отклика нужно пройти тестирование"
    )


def test_outcome_reason_empty_for_other_outcomes() -> None:
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))

    assert _outcome_reason(stats, (0, 0, 0, 0), OUTCOME_SENT) == ""
    assert _outcome_reason(stats, (0, 0, 0, 0), OUTCOME_QUEUED) == ""
    assert _outcome_reason(stats, (0, 0, 0, 0), OUTCOME_SKIPPED) == ""


def test_queue_reason_texts_match_requirements() -> None:
    """Тексты причин для файла дня — человеческие, а не коды."""
    assert QUEUE_REASON_TEXTS["empty_cover_letter"] == (
        "Нужно заполнить сопроводительное письмо вручную"
    )
    assert QUEUE_REASON_TEXTS["test"] == (
        "Для отклика на вакансию необходимо пройти тестирование"
    )
    assert QUEUE_REASON_TEXTS["unknown"] == (
        "hh.ru не подтвердил отправку отклика — проверьте вакансию вручную"
    )
    assert QUEUE_REASON_TEXTS["captcha"] == "hh.ru просит пройти капчу: откликнитесь вручную"


def test_problem_screenshot_is_saved_and_logged(tmp_path: Path) -> None:
    """Скриншот капчи/ошибки сохраняется и попадает в JSONL без конфликта полей."""
    config = replace(make_config(tmp_path), logs_dir=tmp_path / "logs")
    logger = RunLogger(config.logs_dir, run_id="shot")

    _save_problem_screenshot(_ShotPage(), config, logger, "captcha")
    logger.close()

    assert (config.logs_dir / "shot-captcha.png").exists()
    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    screenshot = [item for item in events if item["event"] == "screenshot"][0]
    assert screenshot["kind"] == "captcha"


class _ShotPage:
    """Заглушка страницы: пишет файл вместо реального скриншота."""

    def screenshot(self, path: str, timeout: int | None = None) -> None:
        Path(path).write_bytes(b"png")


class _BrokenShotPage:
    """Страница, у которой скриншот падает: диагностика не должна ронять запуск."""

    def screenshot(self, path: str, timeout: int | None = None) -> None:
        raise RuntimeError("скриншот недоступен")


def test_problem_screenshot_failure_does_not_raise(tmp_path: Path) -> None:
    config = replace(make_config(tmp_path), logs_dir=tmp_path / "logs")
    logger = RunLogger(config.logs_dir, run_id="broken")

    _save_problem_screenshot(_BrokenShotPage(), config, logger, "captcha")
    logger.close()


def test_queue_records_human_reason(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path / "logs", run_id="queued")
    stats = make_stats()
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/5")

    _queue(stats, logger, card, "empty_cover_letter")
    logger.close()

    assert stats.queued[0]["url"] == "https://hh.ru/vacancy/5"
    assert stats.queued[0]["reason"] == "Нужно заполнить сопроводительное письмо вручную"
    assert stats.queued[0]["queued_at"]


def test_queue_unknown_code_gets_generic_reason(tmp_path: Path) -> None:
    stats = make_stats()
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/6")
    logger = RunLogger(tmp_path / "logs", run_id="queued-unknown")

    _queue(stats, logger, card, "no_form")
    logger.close()

    assert stats.queued[0]["reason"] == (
        "hh.ru не подтвердил отправку отклика — проверьте вакансию вручную"
    )


def test_remember_processing_writes_reason_to_day_report(tmp_path: Path) -> None:
    """Причина пропуска должна попасть в файл дня."""
    logger = RunLogger(tmp_path / "logs", run_id="reason")
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/1")

    _remember_processing(store, logger, card, OUTCOME_SKIPPED, "Стоп-слово в названии: 1С")
    logger.close()

    records = json.loads((tmp_path / "report" / "2026-10-21.json").read_text(encoding="utf-8"))

    assert records["vacancies"][0]["status"] == "skipped"
    assert records["vacancies"][0]["reason"] == "Стоп-слово в названии: 1С"


def test_loop_chain_records_skip_reason(tmp_path: Path) -> None:
    """Цепочка как в цикле: счётчики -> _outcome -> _outcome_reason -> файл дня."""
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    logger = RunLogger(tmp_path / "logs", run_id="chain")
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/1")

    counters = _counters(stats)
    stats.excluded.append(
        SkippedVacancy(
            title="Java",
            company="Ozon",
            url="https://hh.ru/vacancy/1",
            reason="Исключённая компания: Сбер",
        )
    )
    outcome = _outcome(counters, _counters(stats))
    _remember_processing(store, logger, card, outcome, _outcome_reason(stats, counters, outcome))
    logger.close()

    records = json.loads((tmp_path / "report" / "2026-10-21.json").read_text(encoding="utf-8"))

    assert outcome == OUTCOME_EXCLUDED
    assert records["vacancies"][0]["status"] == "excluded"
    assert records["vacancies"][0]["reason"] == "Исключённая компания: Сбер"


def test_retry_statuses_take_config_or_explicit_value(tmp_path: Path) -> None:
    """--retry без значения берёт набор из retry.statuses, со значением — только он."""
    config = make_config(tmp_path)

    assert _retry_statuses(config, "all") == config.retry.statuses
    assert config.retry.statuses == ("error", "queued", "excluded")
    assert _retry_statuses(config, "excluded") == ("excluded",)


def test_excluded_company_gets_own_status(tmp_path: Path) -> None:
    """Компания из exclude_companies — статус excluded, а не skipped.

    Так её видно в отчёте отдельно и можно повторить, убрав из списка.
    """
    base = make_config(tmp_path)
    config = replace(base, search=replace(base.search, exclude_companies=("Сбер",)))
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="excluded")
    card = VacancyCard(title="Java", company="Сбер. IT", url="https://hh.ru/vacancy/7")

    _process_vacancy(
        _FakePage("https://hh.ru/search/vacancy"),
        config,
        Profile(text="Java", examples="", style_rules=""),
        None,  # type: ignore[arg-type]
        stats,
        logger,
        card,
    )
    logger.close()

    assert stats.skipped == []
    assert [item.company for item in stats.excluded] == ["Сбер. IT"]
    assert stats.excluded[0].reason == "Исключённая компания: Сбер"


def test_stop_word_gets_excluded_status(tmp_path: Path) -> None:
    """Стоп-слово в названии — статус excluded: его видно отдельно и можно повторить."""
    base = make_config(tmp_path)
    config = replace(base, search=replace(base.search, exclude_keywords=("lead",)))
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="stop-word")
    card = VacancyCard(
        title="Java Team Lead (Депозитарий)",
        company="Сбер. IT",
        url="https://hh.ru/vacancy/138652903",
    )

    _process_vacancy(
        _FakePage("https://hh.ru/search/vacancy"),
        config,
        Profile(text="Java", examples="", style_rules=""),
        None,  # type: ignore[arg-type]
        stats,
        logger,
        card,
    )
    logger.close()

    assert stats.skipped == []
    assert stats.excluded[0].url == "https://hh.ru/vacancy/138652903"
    assert stats.excluded[0].reason == "Стоп-слово в названии: lead"


def test_retry_goes_by_url_from_report(tmp_path: Path, monkeypatch) -> None:
    """--retry открывает ссылки из отчёта и обновляет запись, не трогая выдачу."""
    config = make_config(tmp_path)
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="error")
    store.record(url="https://hh.ru/vacancy/2", title="Go", company="VK", status="sent")
    logger = RunLogger(tmp_path / "logs", run_id="retry")
    stats = make_stats()
    visited: list[str] = []

    def fake_process(page, config, profile, llm, stats, logger, card) -> None:
        visited.append(card.url)
        stats.sent.append(
            SentVacancy(
                title=card.title,
                company=card.company,
                url=card.url,
                probability=0.9,
            )
        )

    monkeypatch.setattr("agent.pipeline._process_vacancy", fake_process)

    _retry_vacancies(
        _FakePage("https://hh.ru/vacancy/1"),
        config,
        Profile(text="Java", examples="", style_rules=""),
        None,  # type: ignore[arg-type]
        stats,
        logger,
        store,
        ("error",),
    )
    logger.close()

    payload = json.loads((tmp_path / "report" / "2026-10-21.json").read_text(encoding="utf-8"))

    assert visited == ["https://hh.ru/vacancy/1"]
    assert payload["vacancies"][0]["status"] == "sent"
    assert payload["vacancies"][0]["attempts"] == 2
    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["count"] for item in events if item["event"] == "retry_selected"] == [1]
