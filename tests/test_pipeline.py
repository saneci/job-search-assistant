"""Тесты итогового сообщения и формата строк журнала."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config import AppConfig, NotificationConfig, load_config
from agent.hh_vacancy import (
    ResumeNotFoundError,
    ResponseResult,
    STATUS_REJECTED,
    STATUS_SENT,
)
from agent.habr_search import HabrResponse
from agent.llm import DecisionResult, GenerationResult
from agent.logging_setup import RunLogger
from agent.models import (
    ARCHIVE_REASON,
    REJECTED_REASON,
    SentVacancy,
    SkippedVacancy,
    VacancyCard,
    VacancyDetails,
)
from agent.pipeline import (
    OUTCOME_ERROR,
    OUTCOME_EXCLUDED,
    OUTCOME_QUEUED,
    OUTCOME_REJECTED,
    OUTCOME_SENT,
    OUTCOME_SKIPPED,
    PAGE_STATUS_REASONS,
    QUEUE_REASON_TEXTS,
    ResumeNameError,
    _captcha_waiter,
    _check_new_day,
    _counters,
    _final_message,
    _generation_reason_text,
    _last_letter_sentence,
    _letter_has_project_link,
    _notify_captcha,
    _notify_captcha_stop,
    _outcome,
    _outcome_reason,
    _plural,
    _process_and_remember,
    _process_vacancies,
    _process_vacancy,
    _profile_name,
    _queue,
    _remember_card_already_responded,
    _remember_processing,
    _require_resume_name,
    _retry_statuses,
    _retry_vacancies,
    _save_problem_screenshot,
    _send_report_email,
    _sync_habr_responses,
    _VacancyLog,
    channel_for,
    with_resume,
)
from agent.processed import ProcessedStore
from agent.profile import Profile
from agent.report import RunStats


def make_config(tmp_path: Path) -> AppConfig:
    data = {
        "search": {"queries": "Java", "area": "Москва"},
        "pipeline": {
            "limit_per_run": 2,
            "delay_between_actions": {"from": 0, "to": 0},
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return load_config(path)


def make_stats(**kwargs: object) -> RunStats:
    return RunStats(started_at=datetime(2026, 10, 21, 9, 0), **kwargs)  # type: ignore[arg-type]


class _EventLogger:
    """Минимальный журнал: запоминает события и их поля."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def event(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))


def test_require_resume_name_stops_hh_without_name(tmp_path: Path) -> None:
    """Без pipeline.resume.name отклик на hh.ru невозможен: запуск не начинается."""
    config = make_config(tmp_path)
    logger = _EventLogger()

    with pytest.raises(ResumeNameError):
        _require_resume_name(config, logger, channel_for(config))  # type: ignore[arg-type]

    assert logger.events[0][0] == "resume_name_is_empty"


def test_require_resume_name_allows_habr_and_filled_name(tmp_path: Path) -> None:
    """Habr работает без резюме hh.ru, а заполненное имя проходит проверку."""
    config = make_config(tmp_path)
    logger = _EventLogger()

    _require_resume_name(config, logger, channel_for(config, "habr"))  # type: ignore[arg-type]
    _require_resume_name(  # type: ignore[arg-type]
        with_resume(config, "Java Developer"), logger, channel_for(config)
    )

    assert logger.events == []


def test_with_resume_returns_copy_with_new_name(tmp_path: Path) -> None:
    """Повтор с другим резюме получает копию конфига, исходный не меняется."""
    config = make_config(tmp_path)

    changed = with_resume(config, "Team Lead")

    assert changed.pipeline.resume.name == "Team Lead"
    assert config.pipeline.resume.name == ""
    assert with_resume(config, "") is config


def test_profile_name_is_only_for_hh(tmp_path: Path) -> None:
    """Habr не использует резюме hh.ru: в его записях профиля нет."""
    config = with_resume(make_config(tmp_path), "Java Developer")

    assert _profile_name(config, channel_for(config)) == "Java Developer"
    assert _profile_name(config, channel_for(config, "habr")) == ""


def test_final_message_without_actions(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    message = _final_message(make_stats(), config)

    assert message == (
        "Работа приложения завершена, отправлено откликов: 0 из 2. "
        f"Сессия сохранена в {config.data.paths.session_state}."
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
        f"Сессия сохранена в {config.data.paths.session_state}."
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
    assert events[0]["message"] == "Java-разработчик — Ozon: открываю вакансию"
    assert events[0]["vacancy_id"] == "777"
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


class _NightDate(date):
    """Дата «после полуночи» для проверки переключения файлов."""

    @classmethod
    def today(cls) -> date:
        return date(2026, 10, 22)


def test_check_new_day_rotates_log_and_report(tmp_path: Path, monkeypatch) -> None:
    """Через полночь журнал и дневной отчёт переключаются на файлы новой даты."""
    logger = RunLogger(tmp_path / "logs", run_id="night")
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    monkeypatch.setattr("agent.pipeline.date", _NightDate)

    _check_new_day(logger, store)
    _check_new_day(logger, store)
    logger.close()

    assert store.today == date(2026, 10, 22)
    assert store.path.name == "2026-10-22.json"
    assert logger.day == "2026-10-22"
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "2026-10-22.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events].count("new_day") == 1


def test_blocked_vacancy_is_skipped_with_reason(tmp_path: Path, monkeypatch) -> None:
    """«У вас нет доступа к этой вакансии» → status skipped с этой же причиной."""
    config = make_config(tmp_path)
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/7")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="blocked",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="blocked")

    _process_vacancy(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        card=card,
    )
    logger.close()

    assert [item.reason for item in stats.skipped] == ["У вас нет доступа к этой вакансии"]
    assert stats.queued == []


def test_invited_vacancy_is_skipped_without_queue(tmp_path: Path, monkeypatch) -> None:
    """«Вас пригласили» → вакансию проходим, а не откладываем в ручные."""
    config = make_config(tmp_path)
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/8")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="invited",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="invited")

    _process_vacancy(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        card=card,
    )
    logger.close()

    assert set(PAGE_STATUS_REASONS) == {
        "archived",
        "blocked",
        "unavailable",
        "invited",
        "rejected",
    }
    assert [item.reason for item in stats.skipped] == [
        "Уже получено приглашение на интервью по другому резюме"
    ]
    assert stats.queued == []


def test_other_resume_fit_is_queued(tmp_path: Path, monkeypatch) -> None:
    """Подходящая вакансия с откликом другого резюме идёт в очередь, а не в пропуск."""
    config = make_config(tmp_path)
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/139016311")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="Java, Spring, PostgreSQL.",
        blocker="other_resume",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=True,
                probability=0.99,
                threshold=0.8,
                probabilities={"подходит": 0.99, "не подходит": 0.01},
                chosen="подходит",
            )

        def generate(self, profile, vacancy, max_length=1500, signature=""):
            raise AssertionError("письмо не нужно: отклик уже отправлен другим резюме")

    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="other-resume-fit")
    _process_vacancy(
        None,  # type: ignore[arg-type]
        config,
        Profile(text="Java", examples="", style_rules=""),
        FakeLLM(),
        stats,
        logger,
        card,
    )
    logger.close()

    assert stats.sent == []
    assert stats.skipped == []
    assert [item["reason"] for item in stats.queued] == [
        "Уже отправлен отклик по другому резюме"
    ]


def test_other_resume_not_fit_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """Если вакансия не подходит, отклик другого резюме не повод её откладывать."""
    config = make_config(tmp_path)
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/139016311")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="Java, Spring, PostgreSQL.",
        blocker="other_resume",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=False,
                probability=0.4,
                threshold=0.8,
                probabilities={"подходит": 0.4, "не подходит": 0.6},
                chosen="не подходит",
            )

        def explain_rejection(self, profile, vacancy, probability, threshold):
            del profile, vacancy, probability, threshold
            return ""

    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="other-resume-not-fit")
    _process_vacancy(
        None,  # type: ignore[arg-type]
        config,
        Profile(text="Java", examples="", style_rules=""),
        FakeLLM(),
        stats,
        logger,
        card,
    )
    logger.close()

    assert stats.queued == []
    assert [item.reason for item in stats.skipped] == [
        "вероятность 0.40 не выше порога 0.80"
    ]


def test_invited_vacancy_marks_other_resume_invite(tmp_path: Path, monkeypatch) -> None:
    """«Вас пригласили»: другому резюме по этой вакансии ставится invite."""
    config = with_resume(make_config(tmp_path), "Team Lead")
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/12")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="invited",
    )
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    store.record(
        url=card.url,
        title=card.title,
        company=card.company,
        status="sent",
        profile="Java Developer",
        channel="hh",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="invited-other-resume")

    _process_and_remember(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        processed=store,
        card=card,
        channel=channel_for(config),
    )
    logger.close()

    rows = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"]
    other = [row for row in rows if row.get("profile") == "Java Developer"]
    assert [row["status"] for row in other] == ["invite"]
    assert other[0]["reason"] == "По отклику пришло приглашение на интервью"
    # Приглашение не обработка вакансии: счётчик попыток не растёт.
    assert other[0]["attempts"] == 1
    assert [item.reason for item in stats.skipped] == [
        "Уже получено приглашение на интервью по другому резюме"
    ]


def test_unavailable_vacancy_is_skipped_with_reason(tmp_path: Path, monkeypatch) -> None:
    """«Вам недоступна эта вакансия» → skipped, updated_at — момент обработки."""
    config = with_resume(make_config(tmp_path), "Team Lead")
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/13")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="unavailable",
    )
    report = tmp_path / "report"
    report.mkdir()
    (report / "2026-10-21.json").write_text(
        json.dumps(
            {
                "date": "2026-10-21",
                "updated_at": "2026-10-21T09:00:00+03:00",
                "summary": {},
                "vacancies": [
                    {
                        "url": card.url,
                        "title": card.title,
                        "company": card.company,
                        "status": "queued",
                        "attempts": 1,
                        "created_at": "2026-10-21T09:00:00+03:00",
                        "updated_at": "2026-10-21T09:00:00+03:00",
                        "reason": "нужен ручной отклик",
                        "profile": "Team Lead",
                        "channel": "hh",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = ProcessedStore(report, today=date(2026, 10, 21))
    store.load()
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="unavailable")

    _process_and_remember(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        processed=store,
        card=card,
        channel=channel_for(config),
    )
    logger.close()

    row = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"][0]
    assert row["status"] == "skipped"
    assert row["reason"] == "Вакансия недоступна или заблокирована"
    # created_at не меняется, updated_at — время последней обработки.
    assert row["created_at"] == "2026-10-21T09:00:00+03:00"
    assert row["updated_at"] != "2026-10-21T09:00:00+03:00"
    assert row["attempts"] == 2
    assert [item.reason for item in stats.skipped] == ["Вакансия недоступна или заблокирована"]


def test_archived_vacancy_is_recorded_as_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Вакансия в архиве»: новая запись идёт в skipped с полем archived."""
    config = with_resume(make_config(tmp_path), "Team Lead")
    card = VacancyCard(
        title="Team lead data platform",
        company="Ozon",
        url="https://hh.ru/vacancy/137925661",
    )
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="archived",
        archived=True,
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="archived-new")

    _process_and_remember(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        processed=store,
        card=card,
        channel=channel_for(config),
    )
    logger.close()

    row = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"][0]
    assert row["status"] == "skipped"
    assert row["archived"] is True
    assert row["reason"] == ARCHIVE_REASON
    assert row["attempts"] == 1


def test_unavailable_vacancy_is_archived_in_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Закрытый доступ к вакансии — тоже архив: в отчёте появляется archived."""
    config = with_resume(make_config(tmp_path), "Team Lead")
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/15")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="unavailable",
        archived=True,
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="archived-unavailable")

    _process_and_remember(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        processed=store,
        card=card,
        channel=channel_for(config),
    )
    logger.close()

    row = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"][0]
    assert row["status"] == "skipped"
    assert row["archived"] is True
    assert row["reason"] == "Вакансия недоступна или заблокирована"


def test_archived_known_vacancy_keeps_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Архивная вакансия, которая уже в отчёте: статус и причина не меняются."""
    config = with_resume(make_config(tmp_path), "Team Lead")
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/16")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="archived",
        archived=True,
    )
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    store.record(
        url=card.url,
        title=card.title,
        company=card.company,
        status="sent",
        reason="отклик уже отправлен",
        profile="Team Lead",
        channel="hh",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="archived-known")

    _process_and_remember(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        processed=store,
        card=card,
        channel=channel_for(config),
    )
    logger.close()

    row = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"][0]
    # Статус прежний: метрики по уже обработанной вакансии не едут.
    assert row["status"] == "sent"
    # Причина становится архивной: она одна у всех записей с `archived: true`.
    assert row["reason"] == ARCHIVE_REASON
    assert row["archived"] is True
    assert row["archived"] is True
    assert row["attempts"] == 1


def test_skipped_page_state_does_not_wait_pause(tmp_path: Path, monkeypatch) -> None:
    """Пауза между действиями не тратится на вакансию, которую пропускаем сразу."""
    config = make_config(tmp_path)
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/14")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="unavailable",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    pauses: list[str] = []
    monkeypatch.setattr(
        "agent.pipeline._pause", lambda config, logger, reason: pauses.append(reason)
    )
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="skip-without-pause")

    _process_vacancy(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        card=card,
    )
    logger.close()

    assert pauses == []
    assert [item.reason for item in stats.skipped] == ["Вакансия недоступна или заблокирована"]


def test_rejected_vacancy_gets_own_status(tmp_path: Path, monkeypatch) -> None:
    """«Вам отказали» → статус rejected с причиной «По отклику пришёл отказ»."""
    config = make_config(tmp_path)
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/9")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="rejected",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="rejected")

    _process_vacancy(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        card=card,
    )
    logger.close()

    assert stats.skipped == []
    assert [item.reason for item in stats.rejected] == ["По отклику пришёл отказ"]
    assert stats.queued == []
    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    page_events = [item for item in events if item["event"] == "page_status"]
    assert [item["status"] for item in page_events] == ["rejected"]


def test_already_responded_vacancy_gets_sent_status(tmp_path: Path, monkeypatch) -> None:
    """«Отклик отправлен» на странице → статус sent, а не skipped."""
    config = make_config(tmp_path)
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/11")
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        already_responded=True,
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="already")

    _process_vacancy(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        card=card,
    )
    logger.close()

    assert stats.skipped == []
    assert [item.reason for item in stats.already_sent] == ["отклик уже отправлен"]
    # Лимит откликов такие вакансии не расходуют.
    assert stats.sent_count == 0
    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    already = [item for item in events if item["event"] == "already_responded"]
    assert [item["status"] for item in already] == ["sent"]


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
                "search": {
                    "queries": "Java developer",
                    "area": "Москва",
                    "per_page": 100,
                },
                "pipeline": {
                    "limit_per_run": 1,
                    "max_vacancies": 5,
                    "delay_between_actions": {"from": 0, "to": 0},
                },
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
    plan = "По запросу «Java developer» найдено 545 вакансий, разделяю обработку на 6 страниц"
    page = (
        "Обработка 1 вакансия из результата запроса «Java developer»: страница 1 из 6"
    )

    assert plan in out
    assert page in out

    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    messages = [event.get("message") for event in events]
    assert plan in messages
    assert page in messages

    # Карточек пришло меньше, чем ждали: обрыв догрузки видно в JSONL.
    short = [item for item in events if item["event"] == "search_page_short"]
    assert short[0]["cards"] == 1
    assert short[0]["expected"] == 100


def test_outcome_detects_sent() -> None:
    assert _outcome((0, 0, 0, 0, 0, 0, 0), (1, 0, 0, 0, 0, 0, 0)) == OUTCOME_SENT


def test_outcome_prefers_sent_over_skipped() -> None:
    assert _outcome((0, 1, 0, 0, 0, 0, 0), (1, 2, 0, 0, 0, 0, 0)) == OUTCOME_SENT


def test_outcome_detects_queue_and_error() -> None:
    assert _outcome((0, 0, 0, 0, 0, 0, 0), (0, 0, 1, 0, 0, 0, 0)) == OUTCOME_QUEUED
    assert _outcome((0, 0, 0, 0, 0, 0, 0), (0, 0, 0, 1, 0, 0, 0)) == OUTCOME_ERROR
    assert _outcome((0, 0, 0, 0, 0, 0, 0), (0, 1, 0, 0, 0, 0, 0)) == OUTCOME_SKIPPED
    assert _outcome((0, 0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 1, 0, 0)) == OUTCOME_EXCLUDED


def test_outcome_detects_rejected() -> None:
    """Отказ по отклику — отдельный статус, не пропуск."""
    assert _outcome((0, 0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 1, 0)) == "rejected"


def test_outcome_marks_already_responded_as_sent() -> None:
    """Отклик уже был отправлен — в файле дня это sent, а не skipped."""
    assert _outcome((0, 0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0, 1)) == OUTCOME_SENT


def test_captcha_waiter_disabled_in_headless(tmp_path: Path) -> None:
    """В headless окна нет — ждать ручного решения капчи некому."""
    base = make_config(tmp_path)
    config = base.model_copy(
        update={"browser": base.browser.model_copy(update={"headless": True})}
    )
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
    config = make_config(tmp_path)
    notifications = config.notifications.model_copy(
        update={"report": NotificationConfig(enabled=False)}
    )
    config = config.model_copy(update={"notifications": notifications})

    sent, message = _send_report_email(config, "отчёт", make_stats())

    assert sent is False
    assert "notifications.report.enabled" in message


def test_report_email_uses_own_recipient(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    notifications = config.notifications.model_copy(
        update={"report": NotificationConfig(recipient="report@example.com")}
    )
    config = config.model_copy(update={"notifications": notifications})
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
    assert _outcome((1, 0, 0, 0, 0, 0, 0), (1, 0, 0, 0, 0, 0, 0)) is None


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

    assert _outcome_reason(stats, (0, 0, 0, 0, 0, 0, 0), OUTCOME_SKIPPED) == (
        "вероятность 0.08 не выше порога 0.80"
    )


def test_outcome_reason_takes_queued_reason() -> None:
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    stats.queued.append(
        {"url": "https://hh.ru/vacancy/1", "reason": "Для отклика нужно пройти тестирование"}
    )

    assert _outcome_reason(stats, (0, 0, 0, 0, 0, 0, 0), OUTCOME_QUEUED) == (
        "Для отклика нужно пройти тестирование"
    )


def test_outcome_reason_takes_rejected_reason() -> None:
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    stats.rejected.append(
        SkippedVacancy(
            title="Java-разработчик",
            company="Клиника",
            url="https://hh.ru/vacancy/136409645",
            reason="По отклику пришёл отказ",
        )
    )

    assert (
        _outcome_reason(stats, (0, 0, 0, 0, 0, 0, 0), "rejected") == "По отклику пришёл отказ"
    )


def test_outcome_reason_takes_already_responded_reason() -> None:
    """Для «уже отправлено» причина остаётся в записи: видно, почему sent."""
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    stats.already_sent.append(
        SkippedVacancy(
            title="Java-разработчик",
            company="Ozon",
            url="https://hh.ru/vacancy/1",
            reason="отклик уже отправлен",
        )
    )

    assert (
        _outcome_reason(stats, (0, 0, 0, 0, 0, 0, 0), OUTCOME_SENT) == "отклик уже отправлен"
    )


def test_outcome_reason_takes_sent_letter_reason() -> None:
    """У настоящего отклика причина — последнее предложение письма LLM."""
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    sentence = (
        "Практического опыта с gRPC, GraphQL и Redis в профиле нет, "
        "но это не перекрывает основной релевантный опыт."
    )
    stats.sent.append(
        SentVacancy(
            title="Java-разработчик",
            company="Ozon",
            url="https://hh.ru/vacancy/1",
            probability=0.9,
            reason=sentence,
        )
    )

    assert _outcome_reason(stats, (0, 0, 0, 0, 0, 0, 0), OUTCOME_SENT) == sentence


def test_last_letter_sentence_skips_signature() -> None:
    """В причину sent идёт последнее предложение письма, а не подпись."""
    sentence = (
        "Практического опыта с gRPC, GraphQL и Redis в профиле нет, "
        "но это не перекрывает основной релевантный опыт."
    )
    signature = "С уважением,\nАлександр Гладской\nТелеграм: @saneci\nПочта: a@b.ru"
    letter = f"Здравствуйте! Готов обсудить вакансию. {sentence}\n\n{signature}"

    assert _last_letter_sentence(letter, signature) == sentence


def test_last_letter_sentence_drops_signature_added_by_model() -> None:
    """Если модель добавила подпись сама, строки контактов в причину не попадают."""
    sentence = "Готов обсудить задачи и сроки на созвоне."
    letter = (
        f"Здравствуйте! {sentence}\n\nС уважением,\nАлександр\n"
        "Телеграм: @saneci\nПочта: a@b.ru"
    )

    assert _last_letter_sentence(letter) == sentence


def test_remember_card_already_responded_keeps_sent_record(tmp_path: Path) -> None:
    """Повторная карточка «Вы откликнулись» не переносит запись в сегодняшний файл."""
    store = ProcessedStore(tmp_path / "report", today=date(2026, 11, 2))
    url = "https://career.habr.com/vacancies/1000150058"
    store.record(
        url=url,
        title="Java-разработчик",
        company="Компания",
        status=OUTCOME_SENT,
        reason="отклик уже отправлен",
        channel="habr",
    )
    before = store.find_record("Java-разработчик", "Компания", url, "")
    assert before is not None
    updated_at = before["updated_at"]
    attempts = before["attempts"]
    logger = RunLogger(tmp_path / "logs", run_id="habr-already-responded")
    card = VacancyCard(title="Java-разработчик", company="Компания", url=url)

    recorded = _remember_card_already_responded(
        store,
        logger,
        card,
        "",
        SimpleNamespace(name="habr"),
    )
    logger.close()

    after = store.find_record("Java-разработчик", "Компания", url, "")
    assert recorded is False
    assert after is not None
    assert after["updated_at"] == updated_at
    assert after["attempts"] == attempts


def test_remember_card_already_responded_keeps_rejected(tmp_path: Path) -> None:
    """Терминальный отказ карточка Habr не откатывает в sent."""
    store = ProcessedStore(tmp_path / "report", today=date(2026, 11, 2))
    url = "https://career.habr.com/vacancies/1000164183"
    store.record(
        url=url,
        title="Java-разработчик",
        company="Компания",
        status=OUTCOME_REJECTED,
        reason=REJECTED_REASON,
        channel="habr",
    )
    before = store.find_record("Java-разработчик", "Компания", url, "")
    assert before is not None
    updated_at = before["updated_at"]
    logger = RunLogger(tmp_path / "logs", run_id="habr-rejected")
    card = VacancyCard(title="Java-разработчик", company="Компания", url=url)

    recorded = _remember_card_already_responded(
        store,
        logger,
        card,
        "",
        SimpleNamespace(name="habr"),
    )
    logger.close()

    after = store.find_record("Java-разработчик", "Компания", url, "")
    assert recorded is False
    assert after is not None
    assert after["status"] == OUTCOME_REJECTED
    assert after["reason"] == REJECTED_REASON
    assert after["updated_at"] == updated_at


def test_remember_card_already_responded_keeps_invite(tmp_path: Path) -> None:
    """Приглашение карточка Habr не понижает до sent."""
    store = ProcessedStore(tmp_path / "report", today=date(2026, 11, 2))
    url = "https://career.habr.com/vacancies/1000164183"
    store.record(
        url=url,
        title="Java-разработчик",
        company="Компания",
        status="invite",
        reason="По отклику пришло приглашение на интервью",
        channel="habr",
    )
    logger = RunLogger(tmp_path / "logs", run_id="habr-invite")
    card = VacancyCard(title="Java-разработчик", company="Компания", url=url)

    recorded = _remember_card_already_responded(
        store,
        logger,
        card,
        "",
        SimpleNamespace(name="habr"),
    )
    logger.close()

    after = store.find_record("Java-разработчик", "Компания", url, "")
    assert recorded is False
    assert after is not None
    assert after["status"] == "invite"


def test_remember_card_already_responded_keeps_archived(tmp_path: Path) -> None:
    """Архивная запись терминальна: карточка Habr её не возвращает в sent."""
    store = ProcessedStore(tmp_path / "report", today=date(2026, 11, 2))
    url = "https://career.habr.com/vacancies/1000164183"
    store.record(
        url=url,
        title="Java-разработчик",
        company="Компания",
        status=OUTCOME_SKIPPED,
        reason=ARCHIVE_REASON,
        channel="habr",
        archived=True,
    )
    logger = RunLogger(tmp_path / "logs", run_id="habr-archived")
    card = VacancyCard(title="Java-разработчик", company="Компания", url=url)

    recorded = _remember_card_already_responded(
        store,
        logger,
        card,
        "",
        SimpleNamespace(name="habr"),
    )
    logger.close()

    after = store.find_record("Java-разработчик", "Компания", url, "")
    assert recorded is False
    assert after is not None
    assert after["status"] == OUTCOME_SKIPPED
    assert after["archived"] is True


def test_remember_card_already_responded_skips_known_status(tmp_path: Path) -> None:
    """Любая известная запись пропускается: карточка не меняет её статус."""
    store = ProcessedStore(tmp_path / "report", today=date(2026, 11, 2))
    url = "https://career.habr.com/vacancies/1000150058"
    store.record(
        url=url,
        title="Java-разработчик",
        company="Компания",
        status=OUTCOME_QUEUED,
        reason="Нужен ручной отклик",
        channel="habr",
    )
    before = store.find_record("Java-разработчик", "Компания", url, "")
    assert before is not None
    updated_at = before["updated_at"]
    attempts = before["attempts"]
    logger = RunLogger(tmp_path / "logs", run_id="habr-known-status")
    card = VacancyCard(title="Java-разработчик", company="Компания", url=url)

    recorded = _remember_card_already_responded(
        store,
        logger,
        card,
        "",
        SimpleNamespace(name="habr"),
    )
    logger.close()

    after = store.find_record("Java-разработчик", "Компания", url, "")
    assert recorded is False
    assert after is not None
    assert after["status"] == OUTCOME_QUEUED
    assert after["reason"] == "Нужен ручной отклик"
    assert after["updated_at"] == updated_at
    assert after["attempts"] == attempts


def test_sync_habr_responses_updates_known_and_keeps_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    """Сверка Habr уточняет неархивные записи, но не переоткрывает отказ."""
    report_dir = tmp_path / "report"
    old = ProcessedStore(report_dir, today=date(2026, 10, 28))
    urls = {
        "rejected": "https://career.habr.com/vacancies/1000164183",
        "invite": "https://career.habr.com/vacancies/1000164184",
        "queued": "https://career.habr.com/vacancies/1000164185",
        "terminal": "https://career.habr.com/vacancies/1000164186",
    }
    old.record(
        url=urls["rejected"],
        title="Java 1",
        company="ЛАНИТ",
        status=OUTCOME_SENT,
        channel="habr",
    )
    old.record(
        url=urls["invite"],
        title="Java 2",
        company="ЛАНИТ",
        status=OUTCOME_SENT,
        channel="habr",
    )
    old.record(
        url=urls["queued"],
        title="Java 3",
        company="ЛАНИТ",
        status=OUTCOME_QUEUED,
        reason="Нужен ручной отклик",
        channel="habr",
    )
    old.record(
        url=urls["terminal"],
        title="Java 4",
        company="ЛАНИТ",
        status=OUTCOME_REJECTED,
        reason=REJECTED_REASON,
        channel="habr",
    )

    store = ProcessedStore(report_dir, today=date(2026, 11, 2))
    store.load()
    responses = [
        HabrResponse(
            url=urls["rejected"],
            title="Java 1",
            company="ЛАНИТ",
            label="Отказ",
            status=OUTCOME_REJECTED,
        ),
        HabrResponse(
            url=urls["invite"],
            title="Java 2",
            company="ЛАНИТ",
            label="Приглашение",
            status="invite",
        ),
        HabrResponse(
            url=urls["queued"],
            title="Java 3",
            company="ЛАНИТ",
            label="Отказ",
            status=OUTCOME_REJECTED,
        ),
        HabrResponse(
            url=urls["terminal"],
            title="Java 4",
            company="ЛАНИТ",
            label="Отказ",
            status=OUTCOME_REJECTED,
        ),
        HabrResponse(
            url="https://career.habr.com/vacancies/1000164187",
            title="Java 5",
            company="ЛАНИТ",
            label="Отказ",
            status=OUTCOME_REJECTED,
        ),
    ]
    monkeypatch.setattr(
        "agent.pipeline.habr_search.open_responses_page",
        lambda page, page_index: responses if page_index == 0 else [],
    )
    logger = RunLogger(tmp_path / "logs", run_id="habr-sync")

    _sync_habr_responses(None, store, logger)  # type: ignore[arg-type]
    logger.close()

    rejected = store.find_record("Java 1", "ЛАНИТ", urls["rejected"], "")
    invite = store.find_record("Java 2", "ЛАНИТ", urls["invite"], "")
    queued = store.find_record("Java 3", "ЛАНИТ", urls["queued"], "")
    terminal = store.find_record("Java 4", "ЛАНИТ", urls["terminal"], "")
    assert rejected is not None and rejected["status"] == OUTCOME_REJECTED
    assert invite is not None and invite["status"] == "invite"
    assert invite["invited_at"]
    assert queued is not None and queued["status"] == OUTCOME_REJECTED
    assert terminal is not None and terminal["status"] == OUTCOME_REJECTED
    today = json.loads((report_dir / "2026-11-02.json").read_text(encoding="utf-8"))
    assert {
        row["url"]: row["status"] for row in today["vacancies"]
    } == {
        urls["rejected"]: OUTCOME_REJECTED,
        urls["invite"]: "invite",
        urls["queued"]: OUTCOME_REJECTED,
    }
    old_rows = json.loads((report_dir / "2026-10-28.json").read_text(encoding="utf-8"))
    assert [row["url"] for row in old_rows["vacancies"]] == [urls["terminal"]]
    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    sync = [item for item in events if item["event"] == "habr_responses_sync"][0]
    assert sync["read"] == 5
    assert sync["updated"] == 3


def test_sync_habr_responses_does_not_move_unchanged_status(
    tmp_path: Path, monkeypatch
) -> None:
    """Повторный статус с той же страницы не переписывает отчёт."""
    report_dir = tmp_path / "report"
    old = ProcessedStore(report_dir, today=date(2026, 10, 28))
    url = "https://career.habr.com/vacancies/1000164183"
    old.record(
        url=url,
        title="Java разработчик",
        company="ЛАНИТ",
        status=OUTCOME_SENT,
        reason="отклик уже отправлен",
        channel="habr",
    )

    store = ProcessedStore(report_dir, today=date(2026, 11, 2))
    store.load()
    response = HabrResponse(
        url=url,
        title="Java разработчик",
        company="ЛАНИТ",
        label="Прочитано",
        status=OUTCOME_SENT,
    )
    monkeypatch.setattr(
        "agent.pipeline.habr_search.open_responses_page",
        lambda page, page_index: [response] if page_index == 0 else [],
    )
    logger = RunLogger(tmp_path / "logs", run_id="habr-sync-same")

    _sync_habr_responses(None, store, logger)  # type: ignore[arg-type]
    logger.close()

    assert not (report_dir / "2026-11-02.json").exists()
    rows = json.loads((report_dir / "2026-10-28.json").read_text(encoding="utf-8"))
    assert rows["vacancies"][0]["status"] == OUTCOME_SENT
    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    sync = [item for item in events if item["event"] == "habr_responses_sync"][0]
    assert sync["updated"] == 0


def test_sync_habr_responses_does_not_fail_run_on_page_error(
    tmp_path: Path, monkeypatch
) -> None:
    """Сбой страницы откликов пишется в лог и не ломает запуск."""
    store = ProcessedStore(tmp_path / "report", today=date(2026, 11, 2))
    logger = RunLogger(tmp_path / "logs", run_id="habr-sync-error")

    def fail(page, page_index):
        raise RuntimeError("нет доступа")

    monkeypatch.setattr("agent.pipeline.habr_search.open_responses_page", fail)
    _sync_habr_responses(None, store, logger)  # type: ignore[arg-type]
    logger.close()

    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(item["event"] == "habr_responses_error" for item in events)


def test_outcome_reason_empty_for_other_outcomes() -> None:
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))

    assert _outcome_reason(stats, (0, 0, 0, 0, 0, 0, 0), OUTCOME_SENT) == ""
    assert _outcome_reason(stats, (0, 0, 0, 0, 0, 0, 0), OUTCOME_QUEUED) == ""
    assert _outcome_reason(stats, (0, 0, 0, 0, 0, 0, 0), OUTCOME_SKIPPED) == ""


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
    assert QUEUE_REASON_TEXTS["no_form"] == (
        "hh.ru не показал форму отклика — проверьте вакансию вручную"
    )
    assert QUEUE_REASON_TEXTS["captcha"] == "hh.ru просит пройти капчу: откликнитесь вручную"


def test_problem_screenshot_is_saved_and_logged(tmp_path: Path) -> None:
    """Скриншот капчи/ошибки сохраняется и попадает в JSONL без конфликта полей."""
    config = make_config(tmp_path)
    paths = config.data.paths.model_copy(update={"logs_dir": tmp_path / "logs"})
    config = config.model_copy(update={"data": config.data.model_copy(update={"paths": paths})})
    logger = RunLogger(config.data.paths.logs_dir, run_id="shot")

    _save_problem_screenshot(_ShotPage(), config, logger, "captcha")
    logger.close()

    assert (config.data.paths.logs_dir / "shot-captcha.png").exists()
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
    config = make_config(tmp_path)
    paths = config.data.paths.model_copy(update={"logs_dir": tmp_path / "logs"})
    config = config.model_copy(update={"data": config.data.model_copy(update={"paths": paths})})
    logger = RunLogger(config.data.paths.logs_dir, run_id="broken")

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


def test_queue_no_form_gets_specific_reason(tmp_path: Path) -> None:
    stats = make_stats()
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/6")
    logger = RunLogger(tmp_path / "logs", run_id="queued-no-form")

    _queue(stats, logger, card, "no_form")
    logger.close()

    assert stats.queued[0]["reason"] == (
        "hh.ru не показал форму отклика — проверьте вакансию вручную"
    )


def test_queue_uses_reason_from_site(tmp_path: Path) -> None:
    """Реальная причина от hh.ru важнее общей формулировки для кода."""
    stats = make_stats()
    card = VacancyCard(title="Java", company="Финтех", url="https://hh.ru/vacancy/137974891")
    logger = RunLogger(tmp_path / "logs", run_id="queued-notice")
    notice = (
        "Чтобы откликнуться на эту вакансию, поменяйте видимость резюме на "
        "«Видно всем работодателям, зарегистрированным на hh.ru»"
    )

    _queue(stats, logger, card, "no_form", reason=notice)
    logger.close()

    assert stats.queued[0]["reason"] == notice
    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    queued = [item for item in events if item["event"] == "queued"][0]
    assert queued["reason"] == notice
    assert queued["vacancy_id"] == "137974891"
    info = [item for item in events if item["event"] == "info"][0]
    assert info["vacancy_id"] == "137974891"
    assert notice in info["message"]


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


def test_remember_processing_writes_profile(tmp_path: Path) -> None:
    """В отчёт попадает резюме, с которым обрабатывалась вакансия."""
    logger = RunLogger(tmp_path / "logs", run_id="profile")
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    card = VacancyCard(title="Java", company="Ozon", url="https://hh.ru/vacancy/1")

    _remember_processing(
        store,
        logger,
        card,
        OUTCOME_SKIPPED,
        "Стоп-слово в названии: 1С",
        "Java Developer",
    )
    logger.close()

    report_path = tmp_path / "report" / "2026-10-21.json"
    records = json.loads(report_path.read_text(encoding="utf-8"))

    assert records["vacancies"][0]["profile"] == "Java Developer"


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


def test_loop_chain_records_rejection(tmp_path: Path) -> None:
    """Цепочка как в цикле: отказ -> _outcome -> _outcome_reason -> файл дня."""
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 0))
    store = ProcessedStore(tmp_path / "report", today=date(2026, 10, 21))
    logger = RunLogger(tmp_path / "logs", run_id="chain-rejected")
    card = VacancyCard(title="Java", company="Клиника", url="https://hh.ru/vacancy/136409645")

    counters = _counters(stats)
    stats.rejected.append(
        SkippedVacancy(
            title="Java",
            company="Клиника",
            url="https://hh.ru/vacancy/136409645",
            reason="По отклику пришёл отказ",
        )
    )
    outcome = _outcome(counters, _counters(stats))
    _remember_processing(store, logger, card, outcome, _outcome_reason(stats, counters, outcome))
    logger.close()

    records = json.loads((tmp_path / "report" / "2026-10-21.json").read_text(encoding="utf-8"))

    assert outcome == "rejected"
    assert records["summary"]["rejected"] == 1
    assert records["vacancies"][0]["status"] == "rejected"
    assert records["vacancies"][0]["reason"] == "По отклику пришёл отказ"


def test_retry_does_not_wait_between_records(tmp_path: Path, monkeypatch) -> None:
    """--retry не паузит между записями: темп задаёт пауза перед обращением к LLM."""
    config = make_config(tmp_path)
    store = ProcessedStore(tmp_path / "report", today=date.today())
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="error")
    store.record(url="https://hh.ru/vacancy/2", title="Go", company="VK", status="error")
    logger = RunLogger(tmp_path / "logs", run_id="retry-no-pause")
    stats = make_stats()
    pauses: list[str] = []

    def fake_process(page, config, profile, llm, stats, logger, card) -> None:
        del page, config, profile, llm, stats, logger, card
        return None

    monkeypatch.setattr("agent.pipeline._process_vacancy", fake_process)
    monkeypatch.setattr(
        "agent.pipeline._pause", lambda config, logger, reason: pauses.append(reason)
    )

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

    assert stats.reviewed == 2
    assert pauses == []


def test_llm_skip_does_not_wait_pause(tmp_path: Path, monkeypatch) -> None:
    """Вакансия не подошла по оценке LLM — паузы нет: ждём только перед откликом."""
    config = make_config(tmp_path)
    details = VacancyDetails(
        url="https://hh.ru/vacancy/138836233",
        title="Java-разработчик",
        company="ППК",
        salary="",
        description="Java, Spring, PostgreSQL.",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    pauses: list[str] = []
    monkeypatch.setattr(
        "agent.pipeline._pause", lambda config, logger, reason: pauses.append(reason)
    )

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=False,
                probability=0.1,
                threshold=0.8,
                probabilities={"подходит": 0.1, "не подходит": 0.9},
                chosen="не подходит",
            )

        def explain_rejection(self, profile, vacancy, probability, threshold):
            del profile, vacancy, probability, threshold
            return "не подходит"

    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="llm-skip")
    card = VacancyCard(title=details.title, company=details.company, url=details.url)

    _process_vacancy(
        _FakePage(details.url),
        config,
        Profile(text="Java", examples="", style_rules=""),
        FakeLLM(),
        stats,
        logger,
        card,
    )
    logger.close()

    assert pauses == []
    assert len(stats.skipped) == 1


def test_probability_equal_threshold_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """Даже если клиент ошибочно поставил fits=True, ровно порог — пропуск."""
    config = make_config(tmp_path)
    details = VacancyDetails(
        url="https://hh.ru/vacancy/137749988",
        title="Java Developer",
        company="Gudwin",
        salary="",
        description="Java, Spring, PostgreSQL.",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=True,
                probability=0.8,
                threshold=0.8,
                probabilities={"подходит": 0.8, "не подходит": 0.2},
                chosen="подходит",
            )

        def explain_rejection(self, profile, vacancy, probability, threshold):
            del profile, vacancy, probability, threshold
            return ""

        def generate(self, profile, vacancy, max_length=1500, signature=""):
            raise AssertionError("письмо для вакансии ровно на пороге не генерируется")

    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="threshold-equal")
    card = VacancyCard(title=details.title, company=details.company, url=details.url)

    _process_vacancy(
        _FakePage(details.url),
        config,
        Profile(text="Java", examples="", style_rules=""),
        FakeLLM(),
        stats,
        logger,
        card,
    )
    logger.close()

    assert stats.sent == []
    assert [item.reason for item in stats.skipped] == [
        "вероятность 0.80 не выше порога 0.80"
    ]


def test_pause_happens_before_response(tmp_path: Path, monkeypatch) -> None:
    """Пауза выполняется перед отправкой отклика, а не перед оценкой LLM."""
    config = make_config(tmp_path)
    details = VacancyDetails(
        url="https://hh.ru/vacancy/138836233",
        title="Java-разработчик",
        company="ППК",
        salary="",
        description="Java, Spring, PostgreSQL.",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    order: list[str] = []

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=True,
                probability=0.95,
                threshold=0.8,
                probabilities={"подходит": 0.95, "не подходит": 0.05},
                chosen="подходит",
            )

        def generate(self, profile, vacancy, max_length=1500, signature=""):
            del profile, vacancy, max_length, signature
            return GenerationResult(
                verdict="подходит",
                reason="ok",
                cover_letter="Здравствуйте! Готов обсудить вакансию.",
            )

    def fake_respond(page, letter, captcha_waiter=None):
        del page, letter, captcha_waiter
        order.append("respond")
        return ResponseResult(status=STATUS_SENT)

    monkeypatch.setattr("agent.pipeline.respond", fake_respond)
    monkeypatch.setattr(
        "agent.pipeline._pause", lambda config, logger, reason: order.append(reason)
    )
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="pause-before-respond")
    card = VacancyCard(title=details.title, company=details.company, url=details.url)

    _process_vacancy(
        _FakePage(details.url),
        config,
        Profile(text="Java", examples="", style_rules=""),
        FakeLLM(),
        stats,
        logger,
        card,
    )
    logger.close()

    assert order == ["перед отправкой отклика", "respond", "после отправки отклика"]


def test_sent_vacancy_reason_is_last_letter_sentence(tmp_path: Path, monkeypatch) -> None:
    """В причине sent — последнее предложение письма, а не общий вердикт."""
    config = make_config(tmp_path)
    details = VacancyDetails(
        url="https://hh.ru/vacancy/138836233",
        title="Java-разработчик",
        company="ППК",
        salary="",
        description="Java, Spring, PostgreSQL.",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=True,
                probability=0.95,
                threshold=0.8,
                probabilities={"подходит": 0.95, "не подходит": 0.05},
                chosen="подходит",
            )

        def generate(self, profile, vacancy, max_length=1500, signature=""):
            del profile, vacancy, max_length, signature
            return GenerationResult(
                verdict="подходит",
                reason="ok",
                cover_letter=(
                    "Здравствуйте! Готов обсудить вакансию. "
                    "Практического опыта с gRPC в профиле нет, "
                    "но это не перекрывает основной релевантный опыт."
                ),
            )

    def fake_respond(page, letter, captcha_waiter=None):
        del page, letter, captcha_waiter
        return ResponseResult(status=STATUS_SENT)

    monkeypatch.setattr("agent.pipeline.respond", fake_respond)
    monkeypatch.setattr("agent.pipeline._pause", lambda config, logger, reason: None)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="sent-letter-reason")
    card = VacancyCard(title=details.title, company=details.company, url=details.url)

    _process_vacancy(
        _FakePage(details.url),
        config,
        Profile(text="Java", examples="", style_rules=""),
        FakeLLM(),
        stats,
        logger,
        card,
    )
    logger.close()

    assert stats.sent[0].reason == (
        "Практического опыта с gRPC в профиле нет, "
        "но это не перекрывает основной релевантный опыт."
    )


def test_respond_rejected_marks_vacancy_rejected(tmp_path: Path, monkeypatch) -> None:
    """Резюме уже откликалось (его нет в форме) — вакансия идёт со статусом rejected."""
    config = make_config(tmp_path)
    details = VacancyDetails(
        url="https://hh.ru/vacancy/138836233",
        title="Java-разработчик",
        company="ППК",
        salary="",
        description="Java, Spring, PostgreSQL.",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=True,
                probability=0.95,
                threshold=0.8,
                probabilities={"подходит": 0.95, "не подходит": 0.05},
                chosen="подходит",
            )

        def generate(self, profile, vacancy, max_length=1500, signature=""):
            del profile, vacancy, max_length, signature
            return GenerationResult(
                verdict="подходит",
                reason="ok",
                cover_letter="Здравствуйте! Готов обсудить вакансию.",
            )

    def fake_respond(page, letter, captcha_waiter=None):
        del page, letter, captcha_waiter
        return ResponseResult(status=STATUS_REJECTED, reason=REJECTED_REASON)

    monkeypatch.setattr("agent.pipeline.respond", fake_respond)
    monkeypatch.setattr("agent.pipeline._pause", lambda config, logger, reason: None)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="respond-rejected")
    card = VacancyCard(title=details.title, company=details.company, url=details.url)

    _process_vacancy(
        _FakePage(details.url),
        config,
        Profile(text="Java", examples="", style_rules=""),
        FakeLLM(),
        stats,
        logger,
        card,
    )
    logger.close()

    assert stats.skipped == []
    assert [item.reason for item in stats.rejected] == ["По отклику пришёл отказ"]


def test_retry_pauses_before_response(tmp_path: Path, monkeypatch) -> None:
    """В --retry порядок тот же: пауза перед откликом, отклик, пауза после."""
    config = with_resume(make_config(tmp_path), "Team Lead")
    details = VacancyDetails(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        salary="",
        description="Java, Spring, PostgreSQL.",
    )
    store = ProcessedStore(tmp_path / "report", today=date.today())
    store.record(
        url=details.url,
        title=details.title,
        company=details.company,
        status="error",
        profile="Team Lead",
        channel="hh",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    order: list[str] = []

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=True,
                probability=0.95,
                threshold=0.8,
                probabilities={"подходит": 0.95, "не подходит": 0.05},
                chosen="подходит",
            )

        def generate(self, profile, vacancy, max_length=1500, signature=""):
            del profile, vacancy, max_length, signature
            return GenerationResult(
                verdict="подходит",
                reason="ok",
                cover_letter="Здравствуйте! Готов обсудить вакансию.",
            )

    def fake_respond(page, letter, captcha_waiter=None):
        del page, letter, captcha_waiter
        order.append("respond")
        return ResponseResult(status=STATUS_SENT)

    monkeypatch.setattr("agent.pipeline.respond", fake_respond)
    monkeypatch.setattr(
        "agent.pipeline._pause", lambda config, logger, reason: order.append(reason)
    )
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="retry-pause")

    _retry_vacancies(
        _FakePage(details.url),
        config,
        Profile(text="Java", examples="", style_rules=""),
        FakeLLM(),
        stats,
        logger,
        store,
        ("error",),
    )
    logger.close()

    assert order == ["перед отправкой отклика", "respond", "после отправки отклика"]


def test_retry_statuses_take_config_or_explicit_value(tmp_path: Path) -> None:
    """--retry без значения берёт набор из retry.statuses, со значением — только он."""
    config = make_config(tmp_path)

    assert _retry_statuses(config, "all") == config.pipeline.retry.statuses
    assert config.pipeline.retry.statuses == ("error", "queued", "excluded")
    assert _retry_statuses(config, "excluded") == ("excluded",)


def test_excluded_company_gets_own_status(tmp_path: Path) -> None:
    """Компания из exclude_companies — статус excluded, а не skipped.

    Так её видно в отчёте отдельно и можно повторить, убрав из списка.
    """
    base = make_config(tmp_path)
    config = base.model_copy(
        update={"search": base.search.model_copy(update={"exclude_companies": ("Сбер",)})}
    )
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
    config = base.model_copy(
        update={"search": base.search.model_copy(update={"exclude_keywords": ("lead",)})}
    )
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
    # Журнал и отчёт в тесте — за 21 октября: смена даты не должна вмешиваться.
    logger.switch_day("2026-10-21")
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

    class _ReportDay(date):
        """«Сегодня» в конвейере — та же дата, что у отчёта: файл дня не меняется."""

        @classmethod
        def today(cls) -> date:
            return date(2026, 10, 21)

    monkeypatch.setattr("agent.pipeline.date", _ReportDay)

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


def test_generation_reason_strips_positive_intro() -> None:
    """В отчёт идёт причина, а не вводное «профиль совпадает»."""
    reason = (
        "По Java, Spring, PostgreSQL, микросервисам, Kafka и Camunda профиль хорошо "
        "совпадает с вакансией. Но для основной задачи — разработки серверных "
        "приложений на Kotlin — требуется от двух лет опыта, а профиль подтверждает "
        "только работу с Kotlin-утилитами и библиотеками без указания срока и "
        "серверной разработки."
    )

    assert _generation_reason_text(reason) == (
        "Для основной задачи — разработки серверных приложений на Kotlin — требуется "
        "от двух лет опыта, а профиль подтверждает только работу с Kotlin-утилитами "
        "и библиотеками без указания срока и серверной разработки."
    )


def test_generation_reason_keeps_plain_explanation() -> None:
    reason = "Профиль не соответствует: нет опыта с Kotlin от двух лет."

    assert _generation_reason_text(reason) == reason


def test_generation_reason_handles_inline_contrast() -> None:
    reason = "Java и Spring совпадают, но нет опыта с Kotlin от двух лет."

    assert _generation_reason_text(reason) == "Нет опыта с Kotlin от двух лет."


def test_generation_reason_drops_final_verdict() -> None:
    reason = "Для задачи нужен опыт с Kotlin от двух лет. Поэтому вердикт: не подходит."

    assert _generation_reason_text(reason) == "Для задачи нужен опыт с Kotlin от двух лет."


def test_generation_reason_ignores_short_technical_text() -> None:
    assert _generation_reason_text("ok") == ""
    assert _generation_reason_text("") == ""



def test_letter_has_project_link() -> None:
    assert _letter_has_project_link("Ссылка: https://github.com/user/project", ())
    assert _letter_has_project_link(
        "Мой GitHub: github.com/user/project",
        ("https://github.com/user/project",),
    )
    assert not _letter_has_project_link(
        "Здравствуйте! Готов обсудить вакансию.",
        ("https://github.com/user/project",),
    )


def test_required_project_link_blocks_send(tmp_path: Path, monkeypatch) -> None:
    """Вакансия требует ссылки на проекты, а в письме их нет — не отправляем."""
    base = make_config(tmp_path)
    cover = base.llm.cover_letter.model_copy(
        update={"pet_projects": ("https://github.com/user/project",)}
    )
    config = base.model_copy(
        update={"llm": base.llm.model_copy(update={"cover_letter": cover})}
    )
    details = VacancyDetails(
        url="https://hh.ru/vacancy/138836233",
        title="Java-разработчик",
        company="ППК",
        salary="",
        description=(
            "P. S. Пожалуйста, приложите к отклику в сопроводительном письме "
            "ссылку на ваши проекты (например, на GitHub)."
        ),
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=True,
                probability=0.95,
                threshold=0.8,
                probabilities={"подходит": 0.95, "не подходит": 0.05},
                chosen="подходит",
            )

        def generate(self, profile, vacancy, max_length=1500, signature=""):
            return GenerationResult(
                verdict="подходит",
                reason="ok",
                cover_letter="Здравствуйте! Готов обсудить вакансию.",
            )

    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="project-links")
    card = VacancyCard(title="Java-разработчик", company="ППК", url=details.url)

    _process_vacancy(
        _FakePage("https://hh.ru/vacancy/138836233"),
        config,
        Profile(text="Java", examples="", style_rules=""),
        FakeLLM(),
        stats,
        logger,
        card,
    )
    logger.close()

    assert stats.sent == []
    assert stats.queued[0]["reason"] == (
        "В вакансии требуется ссылка на проекты в письме, но ни одна ссылка "
        "из llm.cover_letter.pet-projects в письмо не попала"
    )
    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["code"] for item in events if item["event"] == "queued"] == ["project_links"]


def test_resume_not_found_is_logged_and_stops_run(tmp_path: Path, monkeypatch) -> None:
    """Нет настроенного резюме в форме: событие с именем и URL, запуск падает."""
    config = make_config(tmp_path)
    details = VacancyDetails(
        url="https://hh.ru/vacancy/138836233",
        title="Java-разработчик",
        company="ППК",
        salary="",
        description="Java, Spring, PostgreSQL.",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)

    def fake_respond(page, letter, captcha_waiter=None):
        del page, letter, captcha_waiter
        raise ResumeNotFoundError("Team Lead")

    monkeypatch.setattr("agent.pipeline.respond", fake_respond)

    class FakeLLM:
        def decide(self, profile, vacancy):
            return DecisionResult(
                fits=True,
                probability=0.95,
                threshold=0.8,
                probabilities={"подходит": 0.95, "не подходит": 0.05},
                chosen="подходит",
            )

        def generate(self, profile, vacancy, max_length=1500, signature=""):
            return GenerationResult(
                verdict="подходит",
                reason="ok",
                cover_letter="Здравствуйте! Готов обсудить вакансию.",
            )

    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="resume-not-found")
    card = VacancyCard(title=details.title, company=details.company, url=details.url)

    with pytest.raises(ResumeNotFoundError):
        _process_vacancy(
            _FakePage(details.url),
            config,
            Profile(text="Java", examples="", style_rules=""),
            FakeLLM(),
            stats,
            logger,
            card,
        )
    logger.close()

    events = [
        json.loads(line) for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    found = [item for item in events if item["event"] == "resume_not_found"]
    assert found[0]["resume"] == "Team Lead"
    assert found[0]["url"] == details.url
