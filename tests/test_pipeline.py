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
    _queue,
    _remember_processing,
    _save_problem_screenshot,
    _send_report_email,
    _VacancyLog,
)
from agent.processed import ProcessedStore
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
    assert events[0]["message"] == "Java-разработчик — Ozon (id=777): открываю вакансию"
    assert events[1]["event"] == "respond"
    assert events[1]["vacancy"] == "Java-разработчик — Ozon (id=777)"


def test_vacancy_log_without_company(tmp_path: Path) -> None:
    logger = RunLogger(tmp_path, run_id="run2")
    log = _VacancyLog(logger, "Java-разработчик")

    log.info("открываю вакансию")
    logger.close()

    event = json.loads(logger.events_path.read_text(encoding="utf-8").splitlines()[0])

    assert event["message"] == "Java-разработчик: открываю вакансию"


def test_outcome_detects_sent() -> None:
    assert _outcome((0, 0, 0, 0), (1, 0, 0, 0)) == OUTCOME_SENT


def test_outcome_prefers_sent_over_skipped() -> None:
    assert _outcome((0, 1, 0, 0), (1, 2, 0, 0)) == OUTCOME_SENT


def test_outcome_detects_queue_and_error() -> None:
    assert _outcome((0, 0, 0, 0), (0, 0, 1, 0)) == OUTCOME_QUEUED
    assert _outcome((0, 0, 0, 0), (0, 0, 0, 1)) == OUTCOME_ERROR
    assert _outcome((0, 0, 0, 0), (0, 1, 0, 0)) == OUTCOME_SKIPPED


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
    assert captured["subject"] == "hh-agent: отчёт за 2026-10-21 09:00"


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
    """Страница-заглушка: для ожидания капчи нужен только url."""

    def __init__(self, url: str) -> None:
        self.url = url


def test_outcome_is_none_when_nothing_happened() -> None:
    """Сработал лимит откликов — вакансию не запоминаем, пройдём в следующий раз."""
    assert _outcome((1, 0, 0, 0), (1, 0, 0, 0)) is None


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

    assert _outcome_reason(stats, (0, 0, 0, 0), OUTCOME_SKIPPED) == (
        "вероятность 0.08 не выше порога 0.80"
    )


def test_outcome_reason_takes_queued_reason() -> None:
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    stats.queued.append(
        {"url": "https://hh.ru/vacancy/1", "reason": "Для отклика нужно пройти тестирование"}
    )

    assert _outcome_reason(stats, (0, 0, 0, 0), OUTCOME_QUEUED) == (
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

    _remember_processing(store, logger, card, OUTCOME_SKIPPED, "стоп-слово в названии: 1С")
    logger.close()

    records = json.loads((tmp_path / "report" / "2026-10-21.json").read_text(encoding="utf-8"))

    assert records[0]["status"] == "skipped"
    assert records[0]["reason"] == "стоп-слово в названии: 1С"


def test_loop_chain_records_skip_reason(tmp_path: Path) -> None:
    """Цепочка как в цикле: счётчики -> _outcome -> _outcome_reason -> файл дня."""
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    logger = RunLogger(tmp_path / "logs", run_id="chain")
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/1")

    counters = _counters(stats)
    stats.skipped.append(
        SkippedVacancy(
            title="Java",
            company="Ozon",
            url="https://hh.ru/vacancy/1",
            reason="исключённая компания: Сбер",
        )
    )
    outcome = _outcome(counters, _counters(stats))
    _remember_processing(store, logger, card, outcome, _outcome_reason(stats, counters, outcome))
    logger.close()

    records = json.loads((tmp_path / "report" / "2026-10-21.json").read_text(encoding="utf-8"))

    assert outcome == OUTCOME_SKIPPED
    assert records[0]["reason"] == "исключённая компания: Сбер"
