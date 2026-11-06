"""Тесты конвейера на канале Habr: выбор канала, сессии и лента без запросов."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from agent.channel import HABR, HH
from agent.config import load_config
from agent.llm import DecisionResult, GenerationResult
from agent.logging_setup import RunLogger
from agent.models import NOT_FIT_REPORT_REASON, VacancyCard, VacancyDetails
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
from agent.profile import Profile
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


class FakeLLM:
    """LLM без сети: Decision и Generation возвращают заданные ответы."""

    def __init__(
        self,
        *,
        fits: bool = True,
        letter: str = "Здравствуйте! Готов обсудить вакансию.",
        reason: str = "Профиль совпадает с требованиями вакансии.",
        rejection: str = "",
    ) -> None:
        self.fits = fits
        self.letter = letter
        self.reason = reason
        self.rejection = rejection

    def decide(self, profile: Any, vacancy: Any) -> DecisionResult:
        del profile, vacancy
        probability = 0.95 if self.fits else 0.2
        return DecisionResult(
            fits=self.fits,
            probability=probability,
            threshold=0.8,
            probabilities={"подходит": probability, "не подходит": 1 - probability},
            chosen="подходит" if self.fits else "не подходит",
        )

    def generate(
        self,
        profile: Any,
        vacancy: Any,
        max_length: int = 1500,
        signature: str = "",
        **kwargs: Any,
    ) -> GenerationResult:
        del profile, vacancy, max_length, signature, kwargs
        return GenerationResult(
            verdict="подходит" if self.fits else "не подходит",
            reason=self.reason,
            cover_letter=self.letter,
        )

    def explain_rejection(
        self, profile: Any, vacancy: Any, probability: float, threshold: float
    ) -> str:
        del profile, vacancy, probability, threshold
        return self.rejection


def enable_rejection_explanations(path: Path) -> None:
    """Включает объяснение отказов в конфиге теста, как в рабочем config.yaml."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data["llm"] = {"decision": {"explain_rejections": True}}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


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


def test_habr_company_site_vacancy_passes_llm_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Откликнуться у компании»: LLM и письмо до очереди, канал остаётся habr."""
    config = load_config(write_config(tmp_path))
    card = VacancyCard(
        title="Java-разработчик",
        company="Ozon",
        url="https://career.habr.com/vacancies/1000167337",
    )
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="Java, Spring, PostgreSQL.",
        blocker="external_form",
    )

    def fake_search(
        page: Any, query: str, page_index: int
    ) -> tuple[list[VacancyCard], int | None]:
        del page, query, page_index
        return [card], 1

    monkeypatch.setattr("agent.habr_search.open_search_page", fake_search)
    monkeypatch.setattr(
        "agent.habr_vacancy.open_vacancy", lambda page, vacancy: details
    )
    monkeypatch.setattr("agent.pipeline._ensure_session_alive", lambda page: None)

    llm = FakeLLM()
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="habr-company-site")
    store = ProcessedStore(tmp_path / "report", today=date.today())
    _process_vacancies(
        page=None,  # type: ignore[arg-type]
        config=config,
        profile=Profile(
            text="Java", examples="", style_rules="", name="Профиль Habr"
        ),
        llm=llm,
        stats=stats,
        logger=logger,
        processed=store,
    )
    logger.close()

    row = json.loads(store.path.read_text(encoding="utf-8"))["vacancies"][0]
    assert row["status"] == "queued"
    assert row["reason"] == "Отклик нужно отправить на сайте компании"
    assert row["message"] == llm.reason
    assert row["channel"] == "habr"
    assert row["cover_letter"] == llm.letter
    assert row["profile"] == "Профиль Habr"


def test_habr_company_site_blocker_keeps_letter_and_habr_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Страница с откликом на сайте компании: короткая причина, вердикт и письмо."""
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
        description="Go, PostgreSQL.",
        blocker="external_form",
    )
    monkeypatch.setattr("agent.habr_vacancy.open_vacancy", lambda page, vacancy: details)
    llm = FakeLLM()
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="external-blocker")

    result = _process_vacancy(
        None,  # type: ignore[arg-type]
        config,
        Profile(text="Go", examples="", style_rules=""),
        llm,
        stats,
        logger,
        card,
    )
    logger.close()

    assert result is None
    assert [item["url"] for item in stats.queued] == [card.url]
    assert stats.queued[0]["reason"] == "Отклик нужно отправить на сайте компании"
    assert stats.queued[0]["message"] == llm.reason
    assert stats.cover_letters[card.url] == llm.letter


def test_habr_company_site_vacancy_not_fit_is_skipped_with_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Неподходящая вакансия: короткая метка и полный вердикт LLM, письма нет."""
    path = write_config(tmp_path)
    enable_rejection_explanations(path)
    config = load_config(path)
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
        description="Go, PostgreSQL.",
        blocker="external_form",
    )
    verdict = "Профиль не совпадает: в вакансии требуется опыт Kafka."
    monkeypatch.setattr("agent.habr_vacancy.open_vacancy", lambda page, vacancy: details)
    llm = FakeLLM(fits=False, rejection=verdict)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="external-not-fit")

    result = _process_vacancy(
        None,  # type: ignore[arg-type]
        config,
        Profile(text="Java", examples="", style_rules=""),
        llm,
        stats,
        logger,
        card,
    )
    logger.close()

    assert result is None
    assert [(item.report_reason, item.reason) for item in stats.skipped] == [
        (NOT_FIT_REPORT_REASON, verdict)
    ]
    assert stats.queued == []
    assert stats.cover_letters == {}


def test_hh_company_site_blocker_stays_external_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """У hh.ru отклик на сайте работодателя остаётся каналом отчёта external."""
    config = load_config(write_config(tmp_path, channel="hh"))
    card = VacancyCard(
        title="Go-разработчик",
        company="VK",
        url="https://hh.ru/vacancy/1000169504",
    )
    details = VacancyDetails(
        url=card.url,
        title=card.title,
        company=card.company,
        salary="",
        description="Go, PostgreSQL.",
        blocker="external_form",
    )
    monkeypatch.setattr("agent.pipeline.open_vacancy", lambda page, vacancy: details)
    stats = make_stats()
    logger = RunLogger(tmp_path / "logs", run_id="hh-external-blocker")

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
    assert [item["reason"] for item in stats.queued] == [
        "Отклик нужно отправить на сайте работодателя"
    ]


def test_retry_habr_company_site_vacancy_stays_habr_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повтор вакансии с откликом на сайте компании пишет канал habr."""
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
    ) -> None:
        del page, config, profile, llm, logger, vacancy
        stats.queued.append({"url": card.url, "reason": "external"})

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
    assert rows[0]["channel"] == "habr"


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


