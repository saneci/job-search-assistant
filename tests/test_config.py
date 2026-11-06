"""Тесты загрузки конфигурации: дефолты, отсев неверных значений, приоритет источников.

Схема проверяет параметры при старте (fail-fast), поэтому здесь только то, что
нельзя проверить запуском: значения дефолтов, контракт сообщений об ошибках
и порядок источников env → config.local.yaml → config.yaml → дефолты.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.config import DEFAULT_DATA_DIR, ConfigError, load_config, resolve_area_id


def test_default_data_dir_uses_new_app_name() -> None:
    """Каталог данных называется по актуальному имени приложения."""
    assert DEFAULT_DATA_DIR.name == "job-search-assistant"


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Накладывает extra на base: вложенные словари сливаются."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def write_config(
    tmp_path: Path,
    updates: dict[str, Any] | None = None,
    name: str = "config.yaml",
) -> Path:
    data: dict[str, Any] = {
        "search": {"queries": "Java backend engineer", "area": "Москва"},
        "pipeline": {
            "limit_per_run": 5,
            "delay_between_actions": {"from": 0, "to": 0},
        },
    }
    _merge(data, updates or {})
    path = tmp_path / name
    # JSON — допустимое подмножество YAML, поэтому конфиг читается тем же парсером.
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def value_of(config: Any, field: str) -> Any:
    """Значение по пути вида `llm.decision.threshold`."""
    value = config
    for part in field.split("."):
        value = getattr(value, part)
    return value


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("search.area_id", 1),
        ("search.per_page", 100),
        ("search.pages_limit", None),
        ("pipeline.limit_per_run", 5),
        ("pipeline.max_vacancies", 200),
        ("pipeline.resume.boost_interval_hours", 4),
        ("pipeline.retry.statuses", ("error", "queued", "excluded")),
        ("data.logs_retention_days", 30),
        ("data.paths.logs_dir.name", "logs"),
        ("llm.base_url", "https://api.aitunnel.ru/v1"),
        ("llm.timeout_seconds", 120.0),
        ("llm.decision.threshold", 0.8),
        ("llm.decision.model", "gpt-6-luna-decisions"),
        ("llm.generation.model", "gpt-6-luna-pro"),
        ("llm.ocr.enabled", True),
        ("llm.ocr.model", ""),
        ("llm.ocr.max_images", 3),
        ("llm.ocr.max_image_mb", 5.0),
        ("llm.decision.explain_rejections", False),
        ("llm.cover_letter.max_length", 1500),
        ("llm.cover_letter.pet_projects", ()),
        ("browser.headless", False),
        ("browser.captcha_wait_seconds", 120),
        ("notifications.email.enabled", False),
    ],
)
def test_defaults(tmp_path: Path, field: str, expected: Any) -> None:
    assert value_of(load_config(write_config(tmp_path)), field) == expected


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"search": {"area": None}}, "search.area"),
        ({"search": {"queries": "   "}}, "search.queries"),
        ({"pipeline": {"limit_per_run": 0}}, "pipeline.limit_per_run"),
        ({"search": {"per_page": 30}}, "search.per_page"),
        ({"search": {"pages_limit": 0}}, "search.pages_limit"),
        (
            {"pipeline": {"delay_between_actions": {"from": 5, "to": 1}}},
            "pipeline.delay_between_actions",
        ),
        ({"search": {"experience": ["senior"]}}, "search.experience"),
        ({"search": {"schedule": "ночью"}}, "search.schedule"),
        ({"pipeline": {"retry": {"statuses": ["sent"]}}}, "pipeline.retry.statuses"),
        ({"llm": {"decision": {"threshold": 1.5}}}, "llm.decision.threshold"),
        ({"llm": {"decision": {"explain_rejections": "да"}}}, "explain_rejections"),
        ({"llm": {"ocr": {"enabled": "да"}}}, "llm.ocr.enabled"),
        (
            {"notifications": {"captcha": {"enabled": "да"}}},
            "notifications.captcha.enabled",
        ),
        ({"llm": {"system_prompt_path": "no_such_prompt.md"}}, "llm.system_prompt_path"),
    ],
)
def test_invalid_values_fail_fast(tmp_path: Path, updates: dict[str, Any], error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        load_config(write_config(tmp_path, updates))


def test_data_paths_are_resolved_from_user_data_dir(tmp_path: Path) -> None:
    """Относительный путь к данным считается от каталога пользователя."""
    config = load_config(write_config(tmp_path, {"data": {"paths": {"logs_dir": "logs"}}}))

    assert config.data.paths.logs_dir == (DEFAULT_DATA_DIR / "logs").resolve()


def test_old_data_prefix_is_stripped(tmp_path: Path) -> None:
    """Старый префикс `data/` не создаёт вложенный каталог в данных пользователя."""
    config = load_config(
        write_config(tmp_path, {"data": {"paths": {"logs_dir": "data/logs"}}})
    )

    assert config.data.paths.logs_dir == (DEFAULT_DATA_DIR / "logs").resolve()


def test_flat_legacy_key_is_rejected(tmp_path: Path) -> None:
    """Формат один: старый плоский ключ — ошибка, а не тихий пропуск."""
    with pytest.raises(ConfigError, match="limit_per_run"):
        load_config(write_config(tmp_path, {"limit_per_run": 3}))


def test_prefilter_lives_inside_pipeline(tmp_path: Path) -> None:
    """Префильтр читается из `pipeline.prefilter`, плоский ключ `prefilter` — ошибка."""
    config = load_config(
        write_config(tmp_path, {"pipeline": {"prefilter": {"enabled": True}}})
    )

    assert config.pipeline.prefilter.enabled is True

    with pytest.raises(ConfigError, match="prefilter"):
        load_config(write_config(tmp_path, {"prefilter": {"enabled": True}}))


def test_local_config_overrides_base(tmp_path: Path) -> None:
    """config.local.yaml с личными данными перекрывает config.yaml."""
    write_config(tmp_path)
    (tmp_path / "config.local.yaml").write_text(
        json.dumps(
            {"pipeline": {"limit_per_run": 9}, "search": {"queries": ["Kotlin"]}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path / "config.yaml")

    assert config.pipeline.limit_per_run == 9
    assert config.search.queries == ("Kotlin",)
    assert config.search.area == "Москва"


def test_custom_config_replaces_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--config <FILE>` заменяет config.local.yaml, а системный config.yaml остаётся базой."""
    system = write_config(tmp_path, name="system.yaml")
    (tmp_path / "config.local.yaml").write_text(
        json.dumps({"search": {"queries": ["Kotlin"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    custom = tmp_path / "profile.yaml"
    custom.write_text(
        json.dumps({"pipeline": {"limit_per_run": 9}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr("agent.config.DEFAULT_CONFIG_PATH", system)

    config = load_config(custom, merge_system=True)

    assert config.pipeline.limit_per_run == 9
    assert config.search.area == "Москва"
    assert config.pipeline.delay_between_actions.minimum == 0
    assert config.search.queries == ("Java backend engineer",)
    assert config.source_path == system


def test_environment_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение выше файлов: вложенные ключи через `__`, секреты — своими именами."""
    monkeypatch.setenv("PIPELINE__LIMIT_PER_RUN", "7")
    monkeypatch.setenv("LLM__DECISION__THRESHOLD", "0.9")
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-aitunnel-test")

    config = load_config(write_config(tmp_path))

    assert config.pipeline.limit_per_run == 7
    assert config.llm.decision.threshold == 0.9
    assert config.llm.api_key.get_secret_value() == "sk-aitunnel-test"
    assert "sk-aitunnel-test" not in repr(config.llm)  # секрет не печатается


def test_unknown_environment_variables_do_not_break_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """В окружении и .env есть служебные ключи — они не считаются параметрами конфига."""
    monkeypatch.setenv("AITUNNEL_API_KEY", "sk-aitunnel-test")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-test")
    monkeypatch.setenv("SOME_UNRELATED_SETTING", "1")

    config = load_config(write_config(tmp_path))

    assert config.llm.api_key.get_secret_value() == "sk-aitunnel-test"
    assert config.notifications.email.password.get_secret_value() == "smtp-test"


def test_personal_fields_are_not_read_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Подпись с контактами — личное: из окружения не читается, только из YAML."""
    monkeypatch.setenv("LLM__COVER_LETTER__SIGNATURE", "ИЗ ОКРУЖЕНИЯ")

    config = load_config(
        write_config(tmp_path, {"llm": {"cover_letter": {"signature": "ИЗ ФАЙЛА"}}})
    )

    assert config.llm.cover_letter.signature == "ИЗ ФАЙЛА"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Москва", 1), ("москва", 1), ("Россия", 113), ("113", 113), (113, 113)],
)
def test_resolve_area_id(value: Any, expected: int) -> None:
    assert resolve_area_id(value) == expected


def test_unknown_area_raises() -> None:
    with pytest.raises(ConfigError):
        resolve_area_id("Марс")


def test_pet_projects_from_config(tmp_path: Path) -> None:
    config = load_config(
        write_config(
            tmp_path,
            {
                "llm": {
                    "cover_letter": {
                        "pet-projects": [
                            " https://github.com/a/b ",
                            "https://github.com/a/b",
                            "",
                        ]
                    }
                }
            },
        )
    )

    assert config.llm.cover_letter.pet_projects == ("https://github.com/a/b",)


def test_pet_projects_must_be_list(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="pet-projects"):
        load_config(
            write_config(tmp_path, {"llm": {"cover_letter": {"pet-projects": 42}}})
        )


def test_ocr_section_is_read_from_config(tmp_path: Path) -> None:
    """Раздел llm.ocr настраивается в конфиге: выключатель, модель и лимиты картинок."""
    config = load_config(
        write_config(
            tmp_path,
            {
                "llm": {
                    "ocr": {
                        "enabled": False,
                        "model": "gpt-6.1-terra",
                        "max_images": 5,
                        "max_image_mb": 2,
                    }
                }
            },
        )
    )

    assert config.llm.ocr.enabled is False
    assert config.llm.ocr.model == "gpt-6.1-terra"
    assert config.llm.ocr.max_images == 5
    assert config.llm.ocr.max_image_mb == 2.0


def test_pet_projects_are_not_read_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM__COVER_LETTER__PET_PROJECTS", "https://github.com/env")

    config = load_config(write_config(tmp_path))

    assert config.llm.cover_letter.pet_projects == ()

