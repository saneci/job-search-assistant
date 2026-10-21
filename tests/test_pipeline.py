"""Тесты итогового сообщения и формата строк журнала."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from hh_agent.config import AppConfig, load_config
from hh_agent.logging_setup import RunLogger
from hh_agent.models import SentVacancy
from hh_agent.pipeline import (
    OUTCOME_ERROR,
    OUTCOME_QUEUED,
    OUTCOME_SENT,
    OUTCOME_SKIPPED,
    _final_message,
    _outcome,
    _VacancyLog,
)
from hh_agent.report import RunStats


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
        for line in (tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines()
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

    event = json.loads((tmp_path / "run2.jsonl").read_text(encoding="utf-8").splitlines()[0])

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
