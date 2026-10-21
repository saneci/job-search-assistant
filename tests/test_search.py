"""Тесты поискового URL и фильтра по стоп-словам."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from hh_agent.config import DelayRange, SearchConfig
from hh_agent.hh_search import build_search_url, is_excluded


def make_search(**overrides: object) -> SearchConfig:
    base: dict[str, object] = {
        "queries": ("Java backend engineer",),
        "area": "Москва",
        "area_id": 1,
        "salary_from": None,
        "experience": None,
        "schedule": None,
        "exclude_keywords": (),
        "daily_limit": 20,
        "delay": DelayRange(3, 8),
        "pages_limit": 5,
        "per_page": 50,
        "max_vacancies": 200,
    }
    base.update(overrides)
    return SearchConfig(**base)  # type: ignore[arg-type]


def test_build_search_url_minimal() -> None:
    url = build_search_url(make_search(), "Java backend engineer", 0)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "hh.ru"
    assert parsed.path == "/search/vacancy"
    assert params["text"] == ["Java backend engineer"]
    assert params["area"] == ["1"]
    assert params["page"] == ["0"]
    assert params["items_on_page"] == ["50"]
    assert "salary" not in params
    assert "experience" not in params


def test_build_search_url_with_filters() -> None:
    search = make_search(
        area_id=113,
        salary_from=150000,
        experience="between1And3",
        schedule="remote",
    )

    params = parse_qs(urlparse(build_search_url(search, "Java", 2)).query)

    assert params["area"] == ["113"]
    assert params["page"] == ["2"]
    assert params["salary"] == ["150000"]
    assert params["only_with_salary"] == ["true"]
    assert params["experience"] == ["between1And3"]
    assert params["schedule"] == ["remote"]


def test_is_excluded_matches_case_insensitive() -> None:
    assert is_excluded("Senior Java-разработчик", ("senior",)) == "senior"
    assert is_excluded("SENIOR Java", ("senior",)) == "senior"


def test_is_excluded_matches_non_latin_keyword() -> None:
    assert is_excluded("1С-разработчик", ("1С",)) == "1С"


def test_is_excluded_returns_none() -> None:
    assert is_excluded("Java-разработчик", ("senior", "lead")) is None
    assert is_excluded("Java-разработчик", ()) is None
