"""Тесты источника профиля по каналам: у hh профиль из резюме, у habr — заглушка."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.config import AppConfig, load_config
from agent.logging_setup import RunLogger
from agent.pipeline import channel_for, load_profile
from agent.profile import Profile, ProfileError


def make_config(tmp_path: Path, channel: str) -> AppConfig:
    """Конфиг канала: у hh нужны запросы и регион, у habr они не нужны."""
    search: dict[str, Any] = {"channel": channel}
    if channel == "hh":
        search.update({"queries": "Java", "area": "Москва"})
    data = {
        "search": search,
        "pipeline": {"limit_per_run": 1, "delay_between_actions": {"from": 0, "to": 0}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    config = load_config(path)
    paths = config.data.paths.model_copy(update={"logs_dir": tmp_path / "logs"})
    return config.model_copy(update={"data": config.data.model_copy(update={"paths": paths})})


def read_events(tmp_path: Path) -> list[dict[str, Any]]:
    """Все записи журнала запуска в порядке записи."""
    events: list[dict[str, Any]] = []
    for path in sorted((tmp_path / "logs").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def test_habr_profile_is_a_stub(tmp_path: Path) -> None:
    """Профиль Habr не собирается: заглушка останавливает запуск понятной ошибкой."""
    config = make_config(tmp_path, "habr")
    logger = RunLogger(tmp_path / "logs", run_id="habr-profile")

    with pytest.raises(ProfileError, match="career.habr.com"):
        load_profile(None, config, channel_for(config), logger)  # type: ignore[arg-type]
    logger.close()

    events = [event["event"] for event in read_events(tmp_path)]
    assert "habr_profile_not_implemented" in events


def test_hh_profile_comes_from_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Для hh профиль берётся из резюме, заглушка Habr не вызывается."""
    config = make_config(tmp_path, "hh")
    logger = RunLogger(tmp_path / "logs", run_id="hh-profile")
    expected = Profile(text="Java 21, Kafka")

    monkeypatch.setattr("agent.pipeline.load_resume_profile", lambda page, cfg, log: expected)
    monkeypatch.setattr(
        "agent.pipeline.load_habr_profile",
        lambda *args: pytest.fail("для hh заглушка Habr не должна вызываться"),
    )

    assert load_profile(None, config, channel_for(config), logger) is expected  # type: ignore[arg-type]
    logger.close()
