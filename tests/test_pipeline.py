"""Тесты итогового сообщения и формата строк журнала."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from agent.config import AppConfig, load_config
from agent.logging_setup import RunLogger
from agent.models import SentVacancy, SkippedVacancy, VacancyCard
from agent.pipeline import (
    OUTCOME_ERROR,
    OUTCOME_QUEUED,
    OUTCOME_SENT,
    OUTCOME_SKIPPED,
    QUEUE_REASON_TEXTS,
    _counters,
    _final_message,
    _outcome,
    _outcome_reason,
    _queue,
    _remember_processing,
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
        "Непредвиденная ошибка, детальная информация в логе"
    )


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

    assert stats.queued[0]["reason"] == "Непредвиденная ошибка, детальная информация в логе"


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
