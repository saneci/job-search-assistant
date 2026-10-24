"""Тесты поискового URL и фильтра по стоп-словам."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from agent.config import DelayRange, SearchConfig
from agent.hh_search import (
    HH_MAX_PAGES,
    SCROLL_EMPTY_SECONDS,
    SCROLL_STABLE_SECONDS,
    _needs_scroll,
    build_search_url,
    expected_cards,
    is_company_excluded,
    is_excluded,
    pages_plan,
    parse_total,
    vacancy_id,
    vacancy_keys,
)


def make_search(**overrides: object) -> SearchConfig:
    base: dict[str, object] = {
        "queries": ("Java backend engineer",),
        "area": "Москва",
        "area_id": 1,
        "salary_from": None,
        "experience": (),
        "schedule": None,
        "exclude_keywords": (),
        "exclude_companies": (),
        "limit_per_run": 20,
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
        experience=("between1And3",),
        schedule="remote",
    )

    params = parse_qs(urlparse(build_search_url(search, "Java", 2)).query)

    assert params["area"] == ["113"]
    assert params["page"] == ["2"]
    assert params["salary"] == ["150000"]
    assert params["only_with_salary"] == ["true"]
    assert params["experience"] == ["between1And3"]
    assert params["schedule"] == ["remote"]


def test_build_search_url_with_multiple_experience() -> None:
    """Несколько значений опыта уходят повторяющимся параметром — hh отмечает несколько чекбоксов."""
    search = make_search(experience=("noExperience", "between1And3", "between3And6"))

    url = build_search_url(search, "Java", 0)
    params = parse_qs(urlparse(url).query)

    assert params["experience"] == ["noExperience", "between1And3", "between3And6"]
    assert url.count("experience=") == 3


def test_is_excluded_matches_case_insensitive() -> None:
    assert is_excluded("Senior Java-разработчик", ("senior",)) == "senior"
    assert is_excluded("SENIOR Java", ("senior",)) == "senior"


def test_is_excluded_matches_non_latin_keyword() -> None:
    assert is_excluded("1С-разработчик", ("1С",)) == "1С"


def test_is_excluded_returns_none() -> None:
    assert is_excluded("Java-разработчик", ("senior", "lead")) is None
    assert is_excluded("Java-разработчик", ()) is None


def test_is_company_excluded_matches_substring() -> None:
    assert is_company_excluded("Сбер. IT", ("Сбер",)) == "Сбер"
    assert is_company_excluded("ПАО «Сбербанк»", ("сбер",)) == "сбер"


def test_is_company_excluded_returns_none() -> None:
    assert is_company_excluded("Ozon", ("Сбер", "Тинькофф")) is None
    assert is_company_excluded("", ("Сбер",)) is None


def test_vacancy_keys_contain_id_and_title() -> None:
    keys = vacancy_keys(
        "Разработчик Java", "ООО Мираполис", "https://hh.ru/vacancy/123456?query=java"
    )

    assert keys == ("id:123456", "text:разработчик java ооо мираполис")


def test_vacancy_keys_ignore_trailing_slash_and_query() -> None:
    first = vacancy_keys("Java", "Ozon", "https://hh.ru/vacancy/1?query=a")
    second = vacancy_keys("Java", "Ozon", "https://hh.ru/vacancy/1/")

    assert first == second


def test_vacancy_keys_dedupe_same_title_and_company_with_different_ids() -> None:
    """Работодатель публикует одну вакансию несколько раз — это дубликат."""
    first = vacancy_keys("Java - разработчик", "СберЛизинг", "https://hh.ru/vacancy/1")
    second = vacancy_keys("Java - разработчик", "СберЛизинг", "https://hh.ru/vacancy/2")

    assert first[0] != second[0]
    assert first[1] == second[1]


def test_vacancy_keys_without_id() -> None:
    keys = vacancy_keys("Java", "Ozon", "https://hh.ru/vacancy")

    assert keys == ("text:java ozon",)


def test_vacancy_keys_normalize_case_and_yo() -> None:
    assert vacancy_keys("Разработчик", "Ё-Компания", "") == vacancy_keys(
        "разработчик", "е-компания", ""
    )


def test_parse_total_from_search_header() -> None:
    assert parse_total("<div>Найдено 44 вакансии</div>") == 44
    assert parse_total("Найдена 1 вакансия") == 1
    assert parse_total("Найдено 1 234 вакансии") == 1234
    assert parse_total("Найдено&nbsp;7&nbsp;вакансий") == 7
    assert parse_total("Ничего не найдено") is None
    assert parse_total("") is None


def test_per_page_goes_to_url_as_is() -> None:
    """hh.ru отдаёт столько карточек, сколько попросили: 20, 50 или 100."""
    for per_page in (20, 50, 100):
        params = parse_qs(
            urlparse(build_search_url(make_search(per_page=per_page), "Java", 0)).query
        )
        assert params["items_on_page"] == [str(per_page)]


def test_vacancy_id_extracts_number() -> None:
    assert vacancy_id("https://hh.ru/vacancy/1234356?query=java") == "1234356"
    assert vacancy_id("https://hh.ru/vacancy/1234356/") == "1234356"
    assert vacancy_id("https://hh.ru/search/vacancy") is None
    assert vacancy_id("") is None


def test_pages_plan_counts_pages_from_total() -> None:
    """Страниц ровно столько, сколько нужно под найденное число вакансий."""
    assert pages_plan(546, 100) == 6
    assert pages_plan(546, 20) == 28
    assert pages_plan(400, 20) == 20
    assert pages_plan(401, 20) == 21
    assert pages_plan(1, 100) == 1


def test_pages_plan_without_total_stops_at_empty_page() -> None:
    """Число найденных неизвестно — идём до первой пустой страницы."""
    assert pages_plan(None, 100) == HH_MAX_PAGES
    assert pages_plan(0, 100) == HH_MAX_PAGES


def test_pages_plan_keeps_safety_cap() -> None:
    """Предохранитель: hh.ru не отдаёт больше HH_MAX_PAGES страниц."""
    assert pages_plan(10_000, 100) == HH_MAX_PAGES


def test_needs_scroll_until_page_is_full() -> None:
    """Карточки догружаются прокруткой, пока их не станет столько, сколько заказали."""
    assert _needs_scroll(20, 100, 0.0) is True
    assert _needs_scroll(100, 100, 0.0) is False
    # Прирост был только что — ждём дальше, даже если карточек пока 20.
    assert _needs_scroll(20, 100, SCROLL_STABLE_SECONDS / 2) is True
    # Дольше SCROLL_STABLE_SECONDS прироста нет — страница кончилась.
    assert _needs_scroll(20, 100, SCROLL_STABLE_SECONDS) is False
    assert _needs_scroll(100, 20, 0.0) is False


def test_expected_cards_counts_last_page_rest() -> None:
    """350 найденных при per_page 100 — это 100, 100, 100 и 50 карточек."""
    assert [expected_cards(350, 100, index) for index in range(4)] == [100, 100, 100, 50]


def test_expected_cards_without_total_and_out_of_range() -> None:
    """Без «Найдено N» ждём полный per_page; за пределами выдачи — ноль."""
    assert expected_cards(None, 100, 0) == 100
    assert expected_cards(0, 100, 0) == 0
    assert expected_cards(50, 100, 2) == 0


def test_needs_scroll_gives_up_on_empty_page_sooner() -> None:
    """Пустая страница не должна держать запуск: ждём её пару секунд."""
    assert _needs_scroll(0, 100, SCROLL_EMPTY_SECONDS / 2) is True
    assert _needs_scroll(0, 100, SCROLL_EMPTY_SECONDS) is False
