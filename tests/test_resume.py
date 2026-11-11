"""Тесты подъёма резюме в поиске: кэш, интервал и работа со страницей."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from agent.config import AppConfig, load_config
from agent.logging_setup import RunLogger
from agent.resume import (
    RESUMES_URL,
    STATUS_BOOSTED,
    STATUS_DISABLED,
    STATUS_FRESH,
    STATUS_UNAVAILABLE,
    ResumeCache,
    boost_resume,
)

NOW = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)


class FakeLocator:
    """Минимальная замена Locator для проверки поиска элементов."""

    def __init__(self, visible: bool = True, enabled: bool = True) -> None:
        self.visible = visible
        self.enabled = enabled
        self.clicked = False

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        return 1 if self.visible else 0

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled

    def click(self) -> None:
        self.clicked = True


class FakePage:
    """Страница hh.ru: ссылка на резюме и кнопка «Поднять в поиске»."""

    def __init__(
        self,
        resume_name: str,
        has_resume: bool = True,
        has_button: bool = True,
        goto_error: Exception | None = None,
    ) -> None:
        self.url = ""
        self._resume_name = resume_name
        self.resume_link = FakeLocator(visible=has_resume)
        self.boost_button = FakeLocator(visible=has_button)
        self.goto_error = goto_error
        self.visited: list[str] = []
        self.timeouts: list[int] = []

    def goto(self, url: str, **kwargs: Any) -> None:
        if self.goto_error is not None:
            raise self.goto_error
        self.url = url
        self.visited.append(url)

    def get_by_role(self, role: str, name: Any = None) -> FakeLocator:
        if role == "link" and isinstance(name, str) and name == self._resume_name:
            return self.resume_link
        if role in ("button", "link") and hasattr(name, "search"):
            if name.search("Поднять в поиске"):
                return self.boost_button
        return FakeLocator(visible=False)

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(visible=False)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(visible=False)

    def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        return None

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.timeouts.append(milliseconds)


def make_config(tmp_path: Path, name: str = "Java-разработчик", hours: int = 4) -> AppConfig:
    data = {
        "search": {"queries": "Java", "area": "Москва"},
        "pipeline": {
            "limit_per_run": 1,
            "delay_between_actions": {"from": 0, "to": 0},
            "resume": {"name": name, "boost_interval_hours": hours},
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    config = load_config(path)
    paths = config.data.paths.model_copy(update={"cache": tmp_path / "cache.json"})
    data = config.data.model_copy(update={"paths": paths})
    return config.model_copy(update={"data": data})


def make_logger(tmp_path: Path) -> RunLogger:
    return RunLogger(tmp_path / "logs", run_id="resume")


def test_cache_mark_and_reload(tmp_path: Path) -> None:
    cache = ResumeCache(tmp_path / "cache.json")

    at = cache.mark("Java-разработчик", NOW)

    assert at == NOW
    reloaded = ResumeCache(tmp_path / "cache.json")
    assert reloaded.last_boost("Java-разработчик") == NOW
    assert json.loads((tmp_path / "cache.json").read_text(encoding="utf-8")) == {
        "Java-разработчик": "2026-10-22T10:00:00+00:00"
    }


def test_cache_due_after_interval(tmp_path: Path) -> None:
    cache = ResumeCache(tmp_path / "cache.json")
    cache.mark("Java", NOW)

    assert not cache.due("Java", 4, NOW + timedelta(hours=3, minutes=59))
    assert cache.due("Java", 4, NOW + timedelta(hours=4))


def test_cache_due_without_mark(tmp_path: Path) -> None:
    assert ResumeCache(tmp_path / "cache.json").due("Java", 4, NOW)


def test_cache_broken_file_is_empty(tmp_path: Path) -> None:
    (tmp_path / "cache.json").write_text("{не json", encoding="utf-8")

    cache = ResumeCache(tmp_path / "cache.json")

    assert cache.load() == {}
    assert cache.due("Java", 4, NOW)


def test_boost_without_resume_name_is_disabled(tmp_path: Path) -> None:
    config = make_config(tmp_path, name="")
    page = FakePage("Java")

    status = boost_resume(page, config, make_logger(tmp_path))  # type: ignore[arg-type]

    assert status == STATUS_DISABLED
    assert page.visited == []
    assert not (tmp_path / "cache.json").exists()


def test_boost_clicks_button_and_marks_cache(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    page = FakePage("Java-разработчик")

    status = boost_resume(page, config, make_logger(tmp_path))  # type: ignore[arg-type]

    assert status == STATUS_BOOSTED
    assert page.visited == [RESUMES_URL]
    assert page.resume_link.clicked
    assert page.boost_button.clicked
    assert ResumeCache(config.data.paths.cache).last_boost("Java-разработчик") is not None


def test_boost_is_skipped_when_cache_is_fresh(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    ResumeCache(config.data.paths.cache).mark("Java-разработчик", datetime.now().astimezone())
    page = FakePage("Java-разработчик")

    status = boost_resume(page, config, make_logger(tmp_path))  # type: ignore[arg-type]

    assert status == STATUS_FRESH
    assert page.visited == []
    assert not page.boost_button.clicked


def test_boost_without_button_marks_check_time(tmp_path: Path) -> None:
    """Кнопки нет: время проверки сохраняется, страница не открывается каждый запуск."""
    config = make_config(tmp_path)
    page = FakePage("Java-разработчик", has_button=False)

    status = boost_resume(page, config, make_logger(tmp_path))  # type: ignore[arg-type]

    assert status == STATUS_UNAVAILABLE
    assert ResumeCache(config.data.paths.cache).last_boost("Java-разработчик") is not None

    again = FakePage("Java-разработчик", has_button=False)
    second = boost_resume(again, config, make_logger(tmp_path))  # type: ignore[arg-type]

    assert second == STATUS_FRESH
    assert again.visited == []


def test_boost_without_resume_link_is_unavailable(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    page = FakePage("Java-разработчик", has_resume=False)

    status = boost_resume(page, config, make_logger(tmp_path))  # type: ignore[arg-type]

    assert status == STATUS_UNAVAILABLE
    assert not page.boost_button.clicked
    assert ResumeCache(config.data.paths.cache).last_boost("Java-разработчик") is None


def test_boost_survives_browser_error(tmp_path: Path) -> None:
    """Ошибка браузера не останавливает запуск: агент идёт дальше без подъёма."""
    config = make_config(tmp_path)
    page = FakePage("Java-разработчик", goto_error=PlaywrightError("timeout"))

    status = boost_resume(page, config, make_logger(tmp_path))  # type: ignore[arg-type]

    assert status == STATUS_UNAVAILABLE
