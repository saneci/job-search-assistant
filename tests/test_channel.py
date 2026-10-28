"""Тесты выбора канала: hh.ru и career.habr.com."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.channel import HABR, HH, channel_name_for_url, habr_channel
from agent.config import load_config
from agent.pipeline import channel_for


def write_config(tmp_path: Path, search: dict[str, Any]) -> Path:
    """Минимальный конфиг: канал и обязательные поля pipeline."""
    data = {
        "search": search,
        "pipeline": {"limit_per_run": 1, "delay_between_actions": {"from": 0, "to": 0}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_channel_name_for_url() -> None:
    assert channel_name_for_url("https://hh.ru/vacancy/1") == HH
    assert channel_name_for_url("https://career.habr.com/vacancies/1") == HABR
    assert channel_name_for_url("") == HH


def test_habr_channel_description(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, {"channel": "habr"}))
    channel = habr_channel(config)

    assert channel.name == HABR
    assert channel.site_label == "career.habr.com"
    assert channel.per_page == 25
    assert channel.queries() == ("",)
    assert channel.supports_resume_boost is False
    assert channel.uses_persistent_profile is True
    assert channel.queue_reasons["captcha"].startswith("career.habr.com")


def test_hh_channel_respond_accepts_captcha_waiter_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pipeline передаёт ожидание капчи по имени: обёртка канала обязана его принимать."""
    config = load_config(write_config(tmp_path, {"queries": ["java"], "area": 1}))
    calls: dict[str, Any] = {}

    def fake_respond(page: Any, letter: str, captcha_waiter: Any = None) -> Any:
        del page, letter
        calls["waiter"] = captcha_waiter
        return "sent"

    monkeypatch.setattr("agent.pipeline.respond", fake_respond)
    channel = channel_for(config)

    def waiter() -> bool:
        return True

    result = channel.respond(page=None, letter="Здравствуйте!", captcha_waiter=waiter)

    assert result == "sent"
    assert calls["waiter"] is waiter


def test_habr_pages_plan_respects_limit(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, {"channel": "habr", "pages_limit": 4}))
    channel = habr_channel(config)

    assert channel.pages_plan(168) == 4
    assert channel.pages_plan(10) == 1


def test_habr_labels_are_about_suitable_feed(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, {"channel": "habr"}))
    labels = habr_channel(config).labels("")

    assert labels.start == "в ленте «Подходящие»"
    assert labels.subject == "В ленте «Подходящие»"
    assert labels.source == "из ленты «Подходящие»"
    assert labels.title == "Лента «Подходящие»"
