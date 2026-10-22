"""Тесты загрузки конфигурации."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.config import ConfigError, load_config, resolve_area_id


def write_config(tmp_path: Path, **overrides: Any) -> Path:
    data: dict[str, Any] = {
        "search_query": "Java backend engineer",
        "area": "Москва",
        "limit_per_run": 5,
        "delay_between_actions": {"from": 0, "to": 0},
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    # JSON — допустимое подмножество YAML, поэтому конфиг читается тем же парсером.
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_minimal_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))

    assert config.search.queries == ("Java backend engineer",)
    assert config.search.area_id == 1
    assert config.search.limit_per_run == 5
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
    assert config.cache_path.name == "cache.json"
    assert config.resume.name == ""
    assert config.resume.boost_interval_hours == 4


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
    assert config.search.experience == ("between1And3",)
    assert config.search.schedule == "remote"
    assert config.search.exclude_keywords == ("senior", "lead")
    assert config.search.delay.next() >= 3


def test_browser_channel_default_is_playwright_chromium(tmp_path: Path) -> None:
    assert load_config(write_config(tmp_path)).browser.channel is None


def test_exclude_companies(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, exclude_companies=["Сбер", "Тинькофф"]))

    assert config.search.exclude_companies == ("Сбер", "Тинькофф")


def test_exclude_companies_default_empty(tmp_path: Path) -> None:
    assert load_config(write_config(tmp_path)).search.exclude_companies == ()


def test_cover_letter_signature(tmp_path: Path) -> None:
    signature = "С уважением,\nАлександр Гладской\nПочта: a.gladskoy@gmail.com"
    config = load_config(write_config(tmp_path, cover_letter_signature=signature))

    assert config.cover_letter_signature == signature


def test_cover_letter_signature_default_empty(tmp_path: Path) -> None:
    assert load_config(write_config(tmp_path)).cover_letter_signature == ""


def test_system_prompt_path_from_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, llm={"system_prompt_path": "SYSTEM_PROMPT.md"}))

    assert config.llm.system_prompt_path is not None
    assert config.llm.system_prompt_path.name == "SYSTEM_PROMPT.md"


def test_system_prompt_path_outside_project(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, llm={"system_prompt_path": "/outside/prompt.md"}))


def test_system_prompt_path_default_is_project_file(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))

    assert config.llm.system_prompt_path is not None
    assert config.llm.system_prompt_path.name == "SYSTEM_PROMPT.md"
    assert config.llm.system_prompt_path.is_file()


def test_system_prompt_path_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="файл с системным промптом не найден"):
        load_config(write_config(tmp_path, llm={"system_prompt_path": "no_such_prompt.md"}))


def test_browser_channel_from_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, browser={"channel": "chrome", "headless": True}))

    assert config.browser.channel == "chrome"
    assert config.browser.headless is True


def test_empty_browser_channel_means_bundled_chromium(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, browser={"channel": ""}))

    assert config.browser.channel is None


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


@pytest.mark.parametrize("key", ["search_query", "area", "limit_per_run", "delay_between_actions"])
def test_missing_required(tmp_path: Path, key: str) -> None:
    data: dict[str, Any] = {
        "search_query": "Java",
        "area": "Москва",
        "limit_per_run": 5,
        "delay_between_actions": {"from": 0, "to": 0},
    }
    data.pop(key)
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_legacy_daily_limit_key_is_supported(tmp_path: Path) -> None:
    # Старые конфиги с ключом из ТЗ (daily_limit) продолжают работать.
    data = {
        "search_query": "Java",
        "area": "Москва",
        "daily_limit": 7,
        "delay_between_actions": {"from": 0, "to": 0},
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert load_config(path).search.limit_per_run == 7


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("search_query: [не закрытый список\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Не удалось разобрать"):
        load_config(path)


def test_invalid_experience(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, experience="twenty years"))


def test_experience_list(tmp_path: Path) -> None:
    """Несколько значений опыта = несколько чекбоксов в фильтре «Опыт»."""
    config = load_config(
        write_config(tmp_path, experience=["noExperience", "between1And3", "between3And6"])
    )

    assert config.search.experience == ("noExperience", "between1And3", "between3And6")


def test_experience_list_without_duplicates(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, experience=["between3And6", "between3And6"]))

    assert config.search.experience == ("between3And6",)


def test_experience_empty_list_means_no_filter(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, experience=[]))

    assert config.search.experience == ()


def test_experience_list_rejects_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="between1And3"):
        load_config(write_config(tmp_path, experience=["between1And3", "twenty years"]))


def test_experience_must_be_string_or_list(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="строкой или списком строк"):
        load_config(write_config(tmp_path, experience={"value": "between1And3"}))


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


def test_decision_criteria_defaults(tmp_path: Path) -> None:
    criteria = load_config(write_config(tmp_path)).llm.decision.criteria

    assert set(criteria) == {"подходит", "не подходит"}
    assert "70%" in criteria["подходит"]


def test_decision_criteria_from_config(tmp_path: Path) -> None:
    criteria = {"подходит": "Мой критерий", "не подходит": "Мой антикритерий"}

    config = load_config(write_config(tmp_path, llm={"decision": {"criteria": criteria}}))

    assert config.llm.decision.criteria == criteria


def test_decision_criteria_unknown_label(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="неизвестный вариант"):
        load_config(write_config(tmp_path, llm={"decision": {"criteria": {"может быть": "x"}}}))


def test_decision_criteria_must_be_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="должен быть словарём"):
        load_config(write_config(tmp_path, llm={"decision": {"criteria": ["подходит"]}}))


def test_explain_rejections_default_false(tmp_path: Path) -> None:
    assert load_config(write_config(tmp_path)).llm.decision.explain_rejections is False


def test_resume_section(tmp_path: Path) -> None:
    """Название резюме и интервал подъёма берутся из конфига."""
    config = load_config(
        write_config(
            tmp_path,
            resume={"name": "Java-разработчик", "boost_interval_hours": 6},
        )
    )

    assert config.resume.name == "Java-разработчик"
    assert config.resume.boost_interval_hours == 6


def test_resume_interval_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="resume.boost_interval_hours"):
        load_config(write_config(tmp_path, resume={"boost_interval_hours": 0}))


def test_cache_path_from_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, paths={"cache": "data/my-cache.json"}))

    assert config.cache_path.name == "my-cache.json"


def test_captcha_wait_seconds_default(tmp_path: Path) -> None:
    assert load_config(write_config(tmp_path)).browser.captcha_wait_seconds == 120


def test_captcha_wait_seconds_from_config(tmp_path: Path) -> None:
    """Сколько ждать ручного решения капчи — задаётся в конфиге."""
    config = load_config(write_config(tmp_path, browser={"captcha_wait_seconds": 30}))

    assert config.browser.captcha_wait_seconds == 30


def test_captcha_wait_seconds_cannot_be_negative(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="captcha_wait_seconds"):
        load_config(write_config(tmp_path, browser={"captcha_wait_seconds": -5}))


def test_legacy_queue_path_is_ignored(tmp_path: Path) -> None:
    """Старый ключ paths.queue не ломает загрузку: очередь больше не используется."""
    config = load_config(write_config(tmp_path, paths={"queue": "data/report/queue.json"}))

    assert config.cache_path.name == "cache.json"


def test_explain_rejections_enabled(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, llm={"decision": {"explain_rejections": True}}))

    assert config.llm.decision.explain_rejections is True


def test_explain_rejections_must_be_bool(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="true или false"):
        load_config(write_config(tmp_path, llm={"decision": {"explain_rejections": "да"}}))


def test_report_dir_default(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))

    assert config.report_dir.name == "report"
    assert config.report_dir.parent.name == "data"


def test_report_dir_from_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, paths={"report_dir": "data/my-report"}))

    assert config.report_dir.name == "my-report"


def test_report_dir_accepts_legacy_key(tmp_path: Path) -> None:
    """Старый ключ paths.processed_dir продолжает работать."""
    config = load_config(write_config(tmp_path, paths={"processed_dir": "data/legacy"}))

    assert config.report_dir.name == "legacy"


def test_notifications_default_to_every_kind_on(tmp_path: Path) -> None:
    """Старые конфиги без разделов report/captcha работают как раньше."""
    config = load_config(write_config(tmp_path))

    assert config.report_notify.enabled is True
    assert config.captcha_notify.enabled is True
    assert config.report_notify.target(config.email) == config.email.recipient
    assert config.captcha_notify.target(config.email) == config.email.recipient


def test_notifications_kinds_have_own_switch_and_recipient(tmp_path: Path) -> None:
    """Отчёт и капча настраиваются раздельно: каждый вид — свой выключатель и адрес."""
    config = load_config(
        write_config(
            tmp_path,
            notifications={
                "email": {"recipient": "common@example.com"},
                "report": {"enabled": False},
                "captcha": {"recipient": "captcha@example.com"},
            },
        )
    )

    assert config.report_notify.enabled is False
    assert config.report_notify.target(config.email) == "common@example.com"
    assert config.captcha_notify.enabled is True
    assert config.captcha_notify.target(config.email) == "captcha@example.com"


def test_notifications_kind_enabled_must_be_bool(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="notifications.captcha.enabled"):
        load_config(write_config(tmp_path, notifications={"captcha": {"enabled": "да"}}))

