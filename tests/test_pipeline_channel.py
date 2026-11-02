"""Тесты конвейера на канале Habr: выбор канала, сессии и лента без запросов."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from agent.channel import HABR, HH
from agent.config import load_config
from agent.logging_setup import RunLogger
from agent.models import VacancyCard, VacancyDetails
from agent.pipeline import (
    _process_and_remember,
    _process_vacancies,
    _process_vacancy,
    channel_for,
    log_channel_choice,
    session_settings,
    with_channel,
)
from agent.processed import ProcessedStore
from agent.report import RunStats


def write_config(tmp_path: Path, channel: str = "habr") -> Path:
    """Конфиг канала: у Habr нет запросов и региона, у hh они обязательны."""
    search: dict[str, Any] = {"channel": channel}
    if channel == "hh":
        search.update({"queries": ["Java"], "area": 1})
    data = {
        "search": search,
        "pipeline": {"limit_per_run": 3, "delay_between_actions": {"from": 0, "to": 0}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def make_stats() -> RunStats:
    return RunStats(started_at=datetime(2026, 10, 27, 9, 0))


def test_habr_channel_uses_own_session_and_profile(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    channel = channel_for(config)
    state_path, profile_dir = session_settings(config, channel)

    assert channel.name == HABR
    assert state_path.name == "habr-state.json"
    assert profile_dir is not None
    assert profile_dir.name == "habr-profile"


def test_habr_feed_runs_without_queries_and_boost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load_config(write_config(tmp_path))
    card = VacancyCard(
        title="Java-разработчик",
        company="Ozon",
        url="https://career.habr.com/vacancies/1000167337",
    )
    pages: list[tuple[list[VacancyCard], int | None]] = [([card], 168), ([], None)]
    captured: dict[str, Any] = {}

    def fake_search(page: Any, query: str, page_index: int) -> tuple[list[VacancyCard], int | None]:
        del page, query, page_index
        return pages.pop(0)

    def fake_process(
        page: Any,
        config: Any,
        profile: Any,
        llm: Any,
        stats: Any,
        logger: Any,
        processed: Any,
        vacancy: VacancyCard,
        channel: Any = None,
    ) -> None:
        del page, config, profile, llm, stats, logger, processed
        captured["channel"] = channel.name
        captured["url"] = vacancy.url

    def fake_boost(*args: object, **kwargs: object) -> None:
        captured["boost"] = True

    monkeypatch.setattr("agent.habr_search.open_search_page", fake_search)
    monkeypatch.setattr("agent.pipeline._process_and_remember", fake_process)
    monkeypatch.setattr("agent.pipeline._ensure_session_alive", lambda page: None)
    monkeypatch.setattr("agent.pipeline.boost_resume", fake_boost)

    logger = RunLogger(tmp_path / "logs", run_id="habr-channel")
    _process_vacancies(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=make_stats(),
        logger=logger,
        processed=ProcessedStore(tmp_path / "report", today=date.today()),
    )
    logger.close()

    out = capsys.readouterr().out
    assert captured["channel"] == HABR
    assert captured["url"] == card.url
    assert "boost" not in captured
    assert "Начинаю поиск на career.habr.com в ленте «Подходящие»..." in out
    assert "В ленте «Подходящие» найдено 168 вакансий" in out
    assert "Обработка 1 вакансия из ленты «Подходящие»: страница 1 из 7" in out


def test_feed_card_with_response_is_recorded_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Вы откликнулись» на карточке: вакансию не открываем, пишем sent и идём дальше."""
    config = load_config(write_config(tmp_path))
    card = VacancyCard(
        title="Java-разработчик",
        company="Ozon",
        url="https://career.habr.com/vacancies/1000167337",
        already_responded=True,
    )

    def fake_search(
        page: Any, query: str, page_index: int
    ) -> tuple[list[VacancyCard], int | None]:
        del page, query, page_index
        return [card], 1

    opened: list[str] = []
    monkeypatch.setattr("agent.habr_search.open_search_page", fake_search)
    monkeypatch.setattr(
        "agent.pipeline._process_and_remember",
        lambda *args, **kwargs: opened.append("opened"),
    )
    monkeypatch.setattr("agent.pipeline._ensure_session_alive", lambda page: None)

    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="habr-responded")
    store = ProcessedStore(tmp_path / "report", today=date.today())
    _process_vacancies(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        processed=store,
    )
    logger.close()

    rows = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"]
    events = [
        json.loads(line)["event"]
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]

    assert opened == []
    assert [item.reason for item in stats.already_sent] == ["отклик уже отправлен"]
    assert rows[0]["status"] == "sent"
    assert rows[0]["reason"] == "отклик уже отправлен"
    assert rows[0]["channel"] == "habr"
    assert "skipped_already_responded" in events


def test_known_feed_card_with_response_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Известная вакансия Habr с карточкой «Вы откликнулись» не меняется."""
    config = load_config(write_config(tmp_path))
    url = "https://career.habr.com/vacancies/1000167337"
    card = VacancyCard(
        title="Java-разработчик",
        company="Ozon",
        url=url,
        already_responded=True,
    )

    def fake_search(
        page: Any, query: str, page_index: int
    ) -> tuple[list[VacancyCard], int | None]:
        del page, query, page_index
        return [card], 1

    opened: list[str] = []
    monkeypatch.setattr("agent.habr_search.open_search_page", fake_search)
    monkeypatch.setattr(
        "agent.pipeline._process_and_remember",
        lambda *args, **kwargs: opened.append("opened"),
    )
    monkeypatch.setattr("agent.pipeline._ensure_session_alive", lambda page: None)

    store = ProcessedStore(tmp_path / "report", today=date.today())
    store.record(
        url=url,
        title=card.title,
        company=card.company,
        status="rejected",
        reason="По отклику пришёл отказ",
        channel="habr",
    )
    before = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"][0]
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="habr-known-responded")
    _process_vacancies(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        processed=store,
    )
    logger.close()

    after = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"][0]
    events = [
        json.loads(line)["event"]
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert opened == []
    assert stats.already_sent == []
    assert after["status"] == "rejected"
    assert after["reason"] == "По отклику пришёл отказ"
    assert after["updated_at"] == before["updated_at"]
    assert "skipped_processed" in events


def test_external_response_record_is_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отклик на сайте компании ждёт ручной обработки, а не считается пропуском."""
    config = load_config(write_config(tmp_path))
    card = VacancyCard(
        title="Java-разработчик",
        company="Ozon",
        url="https://career.habr.com/vacancies/1000167337",
        external_response=True,
    )

    def fake_search(
        page: Any, query: str, page_index: int
    ) -> tuple[list[VacancyCard], int | None]:
        del page, query, page_index
        return [card], 1

    monkeypatch.setattr("agent.habr_search.open_search_page", fake_search)
    monkeypatch.setattr("agent.pipeline._ensure_session_alive", lambda page: None)

    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="habr-external")
    store = ProcessedStore(tmp_path / "report", today=date.today())
    _process_vacancies(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        stats=stats,
        logger=logger,
        processed=store,
    )
    logger.close()

    rows = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"]
    assert rows[0]["status"] == "queued"
    assert rows[0]["reason"] == "Отклик на сайте компании"
    assert rows[0]["channel"] == "external"
    assert stats.skipped == []
    assert [item["reason"] for item in stats.queued] == ["Отклик на сайте компании"]


def test_process_vacancy_returns_external_for_external_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Страница с формой на сайте компании даёт канал отчёта external."""
    config = load_config(write_config(tmp_path))
    card = VacancyCard(
        title="Go-разработчик",
        company="VK",
        url="https://career.habr.com/vacancies/1000169504",
    )
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="",
        blocker="external_form",
    )
    monkeypatch.setattr("agent.habr_vacancy.open_vacancy", lambda page, vacancy: details)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="external-blocker")

    result = _process_vacancy(
        None,  # type: ignore[arg-type]
        config,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        stats,
        logger,
        card,
    )
    logger.close()

    assert result == "external"
    assert [item["url"] for item in stats.queued] == [card.url]


def test_retry_external_response_gets_external_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повтор вакансии внешней формы тоже пишет канал external."""
    config = load_config(write_config(tmp_path))
    card = VacancyCard(
        title="Go-разработчик",
        company="VK",
        url="https://career.habr.com/vacancies/1000169504",
    )

    def fake_process(
        page: Any,
        config: Any,
        profile: Any,
        llm: Any,
        stats: Any,
        logger: Any,
        vacancy: VacancyCard,
    ) -> str:
        del page, config, profile, llm, logger, vacancy
        stats.queued.append({"url": card.url, "reason": "external"})
        return "external"

    monkeypatch.setattr("agent.pipeline._process_vacancy", fake_process)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="retry-external")
    store = ProcessedStore(tmp_path / "report", today=date.today())

    _process_and_remember(
        None,  # type: ignore[arg-type]
        config,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        stats,
        logger,
        store,
        card,
        channel_for(config),
    )
    logger.close()

    rows = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"]
    assert rows[0]["status"] == "queued"
    assert rows[0]["channel"] == "external"


def test_channel_flag_overrides_config(tmp_path: Path) -> None:
    """Флаг CLI сильнее search.channel, без флага канал берётся из конфига."""
    config = load_config(write_config(tmp_path))  # в конфиге habr

    assert channel_for(config).name == HABR
    assert channel_for(config, "hh").name == HH
    assert channel_for(config, "habr").name == HABR


def test_channel_override_uses_own_session(tmp_path: Path) -> None:
    """Переопределённый канал берёт свою сессию: входы сайтов не путаются."""
    config = load_config(write_config(tmp_path))  # в конфиге habr
    state_path, profile_dir = session_settings(config, channel_for(config, "hh"))

    assert state_path.name == "hh-state.json"
    assert profile_dir is None


def test_channel_choice_is_logged_with_source(tmp_path: Path) -> None:
    """В логе видно канал и откуда он взялся: флаг CLI или конфиг."""
    config = load_config(write_config(tmp_path))
    logger = RunLogger(tmp_path / "logs", run_id="channel-choice")

    log_channel_choice(logger, channel_for(config, "hh"), "hh")
    log_channel_choice(logger, channel_for(config), None)
    logger.close()

    events = [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    selected = [item for item in events if item["event"] == "channel_selected"]
    assert [(item["channel"], item["source"]) for item in selected] == [
        (HH, "flag"),
        (HABR, "config"),
    ]


def test_channel_override_reaches_vacancy_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Флаг меняет канал в конфиге: страница открывается селекторами выбранного сайта."""
    config = with_channel(load_config(write_config(tmp_path)), "hh")  # в конфиге habr
    card = VacancyCard(
        title="Java-разработчик",
        company="Ozon",
        url="https://hh.ru/vacancy/1",
    )
    calls: list[str] = []

    def fake_hh(page: Any, vacancy: VacancyCard) -> VacancyDetails:
        del page
        calls.append("hh")
        return VacancyDetails(
            url=vacancy.url,
            title=vacancy.title,
            company=vacancy.company,
            salary="",
            description="",
            already_responded=True,
        )

    def fake_habr(page: Any, vacancy: VacancyCard) -> VacancyDetails:
        del page, vacancy
        calls.append("habr")
        raise AssertionError("вакансия открыта селекторами Habr")

    monkeypatch.setattr("agent.pipeline.open_vacancy", fake_hh)
    monkeypatch.setattr("agent.habr_vacancy.open_vacancy", fake_habr)

    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="channel-page")

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

    assert calls == ["hh"]
    assert [item.reason for item in stats.already_sent] == ["отклик уже отправлен"]


