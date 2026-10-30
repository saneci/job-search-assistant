"""Тесты канала поиска в конфигурации: значения, обязательность, лишние ключи."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.config import ConfigError, load_config


def write_config(tmp_path: Path, search: dict[str, Any] | None = None) -> Path:
    data: dict[str, Any] = {
        "search": search if search is not None else {"queries": "Java", "area": "Москва"},
        "pipeline": {"limit_per_run": 1, "delay_between_actions": {"from": 0, "to": 0}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_channel_defaults_to_hh(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))

    assert config.search.channel == "hh"
    assert config.data.paths.session_state.name == "hh-state.json"
    assert config.data.paths.habr_session_state.name == "habr-state.json"
    assert config.data.paths.habr_profile.name == "habr-profile"


def test_habr_channel_needs_no_queries_and_area(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, {"channel": "habr"}))

    assert config.search.channel == "habr"
    assert config.search.queries == ()
    assert config.search.area == ""


def test_hh_channel_still_requires_area(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="search.area"):
        load_config(write_config(tmp_path, {"channel": "hh", "queries": "Java"}))


def test_unknown_channel_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="search.channel"):
        load_config(write_config(tmp_path, {"channel": "superjob", "queries": "Java", "area": "Москва"}))


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """Вложенные модели тоже запрещают неизвестные ключи, как обещает документация."""
    with pytest.raises(ConfigError, match="неизвестный параметр"):
        load_config(
            write_config(
                tmp_path,
                {"queries": "Java", "area": "Москва", "chanel": "hh"},
            )
        )
