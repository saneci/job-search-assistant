"""Тесты загрузки конфигурации."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from hh_agent.config import ConfigError, load_config, resolve_area_id


def write_config(tmp_path: Path, **overrides: Any) -> Path:
    data: dict[str, Any] = {
        "search_query": "Java backend engineer",
        "area": "Москва",
        "daily_limit": 5,
        "delay_between_actions": {"from": 0, "to": 0},
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_minimal_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))

    assert config.search.queries == ("Java backend engineer",)
    assert config.search.area_id == 1
    assert config.search.daily_limit == 5
    assert config.search.delay.minimum == 0
    assert config.search.delay.maximum == 0
    assert config.search.salary_from is None
    assert config.cover_letter_max_length == 1500
    assert config.llm.decision.model == "gpt-6-luna-decisions"
    assert config.llm.decision.threshold == 0.8
    assert config.llm.generation.model == "gpt-6-luna-pro"
    assert config.browser.headless is False
    assert config.email.enabled is False
    assert config.state_path.name == "state.json"
    assert config.queue_path.name == "queue.json"


def test_query_list(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, search_query=["Java", "Kotlin", " "]))

    assert config.search.queries == ("Java", "Kotlin")


def test_full_search_config(tmp_path: Path) -> None:
    config = load_config(
        write_config(
            tmp_path,
            salary_from=150000,
            experience="between1And3",
            schedule="remote",
            exclude_keywords=["senior", "lead"],
            delay_between_actions={"from": 3, "to": 8},
        )
    )

    assert config.search.salary_from == 150000
    assert config.search.experience == "between1And3"
    assert config.search.schedule == "remote"
    assert config.search.exclude_keywords == ("senior", "lead")
    assert config.search.delay.next() >= 3


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Москва", 1),
        ("москва", 1),
        ("Россия", 113),
        ("Санкт-Петербург", 2),
        ("113", 113),
        (113, 113),
    ],
)
def test_resolve_area_id(value: Any, expected: int) -> None:
    assert resolve_area_id(value) == expected


def test_unknown_area() -> None:
    with pytest.raises(ConfigError):
        resolve_area_id("Марс")


@pytest.mark.parametrize("key", ["search_query", "area", "daily_limit", "delay_between_actions"])
def test_missing_required(tmp_path: Path, key: str) -> None:
    data: dict[str, Any] = {
        "search_query": "Java",
        "area": "Москва",
        "daily_limit": 5,
        "delay_between_actions": {"from": 0, "to": 0},
    }
    data.pop(key)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_invalid_experience(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, experience="twenty years"))


def test_invalid_delay(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, delay_between_actions={"from": 9, "to": 3}))


def test_paths_outside_project(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, paths={"profile": "/outside/profile.md"}))


def test_api_key_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-aitunnel-test")
    config = load_config(write_config(tmp_path))

    assert config.llm.api_key == "sk-aitunnel-test"


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")
