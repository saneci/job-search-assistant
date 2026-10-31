"""Тесты веб-интерфейса: главная, воронка, отчёт и ручной статус offer."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import web.main as web_main


def write_report(directory: Path, name: str, vacancies: list[dict[str, Any]]) -> None:
    payload = {
        "date": name.removesuffix(".json"),
        "updated_at": "2026-10-24T12:00:00+03:00",
        "summary": {},
        "vacancies": vacancies,
    }
    (directory / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def vacancy(url: str, status: str) -> dict[str, Any]:
    return {
        "url": f"https://hh.ru/vacancy/{url}",
        "title": f"Вакансия {url}",
        "company": "Компания",
        "status": status,
        "attempts": 1,
        "created_at": "2026-10-24T10:00:00+03:00",
        "updated_at": "2026-10-24T10:00:00+03:00",
    }


def vacancy_at(url: str, status: str, at: str) -> dict[str, Any]:
    item = vacancy(url, status)
    item["created_at"] = at
    item["updated_at"] = at
    return item


def vacancy_with_dates(
    url: str, status: str, created_at: str, updated_at: str
) -> dict[str, Any]:
    item = vacancy(url, status)
    item["created_at"] = created_at
    item["updated_at"] = updated_at
    return item


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    write_report(
        tmp_path,
        "2026-10-24.json",
        [
            vacancy("1", "sent"),
            vacancy("2", "rejected"),
            vacancy("3", "offer"),
            vacancy("4", "skipped"),
        ],
    )
    return TestClient(web_main.app)


def test_index_is_served_by_server(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Мониторинг поиска работы" in response.text
    assert 'href="/metrics"' in response.text
    assert 'href="/report"' in response.text
    assert "В отчётах" in response.text


def test_metrics_page_uses_report_data(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "Воронка поиска работы" in response.text
    assert "75.0%" in response.text  # 3 отклика из 4
    assert "33.3%" in response.text  # оффер и отказ по 1 из 3
    assert "conic-gradient(" in response.text
    assert "{%" not in response.text
    assert "В отчётах:" not in response.text


def test_metrics_api(client: TestClient) -> None:
    response = client.get("/api/v1/metrics")

    assert response.status_code == 200
    metrics = response.json()
    assert metrics["total"] == 4
    assert metrics["responses"] == 3
    assert metrics["offers"] == 1
    assert metrics["rates"]["response"] == 75.0


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("year", (date(2026, 1, 1), date(2026, 12, 31))),
        ("quarter", (date(2026, 10, 1), date(2026, 12, 31))),
        ("month", (date(2026, 10, 1), date(2026, 10, 31))),
        ("week", (date(2026, 10, 19), date(2026, 10, 25))),
        ("day", (date(2026, 10, 24), date(2026, 10, 24))),
    ],
)
def test_period_bounds(period: str, expected: tuple[date, date]) -> None:
    assert web_main.period_bounds(period, date(2026, 10, 24)) == expected


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("year", "2026"),
        ("quarter", "IV квартал 2026"),
        ("month", "Октябрь 2026"),
        ("week", "19–25 октября 2026"),
        ("day", "24 октября 2026"),
    ],
)
def test_period_label(period: str, expected: str) -> None:
    start, end = web_main.period_bounds(period, date(2026, 10, 24))
    assert web_main.period_label(period, start, end) == expected


def test_metrics_page_has_period_filter(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "Период:" in response.text
    for label in ("Год", "Квартал", "Месяц", "Неделя", "День"):
        assert f">{label}</a>" in response.text
    assert "filter-btn active" in response.text


def test_metrics_page_explains_period_basis(client: TestClient) -> None:
    """Пользователь видит, по какой дате считается период воронки."""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "Период воронки считается по дате первой обработки вакансии в приложением." in response.text


def test_metrics_api_filters_by_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    write_report(
        tmp_path,
        "2026-10-24.json",
        [vacancy_at("1", "sent", "2026-10-24T10:00:00+03:00")],
    )
    write_report(
        tmp_path,
        "2026-09-30.json",
        [vacancy_at("2", "skipped", "2026-09-30T10:00:00+03:00")],
    )
    write_report(
        tmp_path,
        "2025-12-31.json",
        [vacancy_at("3", "excluded", "2025-12-31T10:00:00+03:00")],
    )
    client = TestClient(web_main.app)

    expected = {"year": 2, "quarter": 1, "month": 1, "week": 1, "day": 1}
    for period, total in expected.items():
        response = client.get(f"/api/v1/metrics?period={period}")
        assert response.status_code == 200
        assert response.json()["total"] == total

    assert client.get("/api/v1/metrics?period=decade").status_code == 422
    assert client.get("/metrics?period=decade").status_code == 422


def test_metrics_period_uses_creation_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ответ по старому отклику остаётся в когорте месяца создания."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    write_report(
        tmp_path,
        "2026-10-24.json",
        [
            vacancy_with_dates(
                "1",
                "offer",
                "2026-09-30T10:00:00+03:00",
                "2026-10-24T10:00:00+03:00",
            ),
            vacancy_with_dates(
                "2",
                "sent",
                "2026-10-24T10:00:00+03:00",
                "2026-10-24T10:00:00+03:00",
            ),
        ],
    )
    client = TestClient(web_main.app)

    metrics = client.get("/api/v1/metrics?period=month").json()

    assert metrics["total"] == 1
    assert metrics["responses"] == 1
    assert metrics["offers"] == 0


def test_metrics_header_shows_date_like_report(client: TestClient) -> None:
    """Дата в шапке воронки — как колонка «Дата» отчёта: yyyy-mm-dd hh:mm:ss."""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "Обновлено: 2026-10-24 12:00:00" in response.text


def test_report_page_counts_sent_status_as_otpravleno(client: TestClient) -> None:
    """Карточка статуса «Отправлено» не подменяет общее число откликов."""
    response = client.get("/report")

    assert response.status_code == 200
    assert '<div class="label">Отправлено</div>' in response.text
    assert '<div class="label">Отклики</div>' not in response.text


def test_report_page_has_offer_status(client: TestClient) -> None:
    response = client.get("/report")

    assert response.status_code == 200
    assert "sumOffered" in response.text
    assert "Оффер" in response.text
    assert "<th>Дата</th>" in response.text
    assert "<th>Время</th>" not in response.text
    assert "formatDateTime" in response.text


def test_report_page_warns_about_terminal_status(client: TestClient) -> None:
    response = client.get("/report")

    assert response.status_code == 200
    assert 'id="terminalDialog"' in response.text
    assert "необратимая операция" in response.text
    assert "TERMINAL_STATUSES" in response.text


def test_update_status_keeps_created_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ручная смена статуса не переписывает дату создания записи."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    today = tmp_path / "today.json"
    monkeypatch.setattr(web_main, "current_report_path", lambda: today)
    created_at = "2026-09-30T10:00:00+03:00"
    write_report(
        tmp_path,
        "2026-10-24.json",
        [vacancy_with_dates("1", "sent", created_at, created_at)],
    )
    client = TestClient(web_main.app)

    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "offer",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["created_at"] == created_at
    assert result["updated_at"] != created_at

    data = json.loads(today.read_text(encoding="utf-8"))
    row = data["vacancies"][0]
    assert row["created_at"] == created_at
    assert row["updated_at"] == result["updated_at"]


def test_update_status_preserves_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ручная смена статуса не теряет резюме, с которым шёл отклик."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    today = tmp_path / "today.json"
    monkeypatch.setattr(web_main, "current_report_path", lambda: today)
    item = vacancy_with_dates(
        "1", "sent", "2026-10-24T10:00:00+03:00", "2026-10-24T10:00:00+03:00"
    )
    item["profile"] = "Java Developer"
    write_report(tmp_path, "2026-10-24.json", [item])
    client = TestClient(web_main.app)

    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "offer",
        },
    )

    assert response.status_code == 200
    assert response.json()["profile"] == "Java Developer"

    data = json.loads(today.read_text(encoding="utf-8"))
    assert data["vacancies"][0]["profile"] == "Java Developer"


def test_update_status_keeps_habr_without_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Правка статуса Habr не добавляет пустое поле profile."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    today = tmp_path / "today.json"
    monkeypatch.setattr(web_main, "current_report_path", lambda: today)
    item = vacancy_with_dates(
        "1", "sent", "2026-10-24T10:00:00+03:00", "2026-10-24T10:00:00+03:00"
    )
    item["url"] = "https://career.habr.com/vacancies/1"
    item["channel"] = "habr"
    item.pop("profile", None)
    write_report(tmp_path, "2026-10-24.json", [item])
    client = TestClient(web_main.app)

    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://career.habr.com/vacancies/1",
            "status": "offer",
        },
    )

    assert response.status_code == 200
    data = json.loads(today.read_text(encoding="utf-8"))
    assert "profile" not in data["vacancies"][0]


def test_update_status_changes_only_selected_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Правка статуса относится к записи выбранного резюме, а не ко всем."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(web_main, "current_report_path", lambda: tmp_path / "today.json")
    first = vacancy("1", "sent")
    first["profile"] = "Java Developer"
    second = vacancy("1", "sent")
    second["profile"] = "Team Lead"
    write_report(tmp_path, "2026-10-24.json", [first, second])
    client = TestClient(web_main.app)

    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "profile": "Team Lead",
            "status": "offer",
        },
    )

    assert response.status_code == 200
    old = json.loads((tmp_path / "2026-10-24.json").read_text(encoding="utf-8"))
    today = json.loads((tmp_path / "today.json").read_text(encoding="utf-8"))
    assert [(row["profile"], row["status"]) for row in old["vacancies"]] == [
        ("Java Developer", "sent")
    ]
    assert [(row["profile"], row["status"]) for row in today["vacancies"]] == [
        ("Team Lead", "offer")
    ]


def test_offer_status_can_be_set_manually(client: TestClient) -> None:
    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "offer",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["summary"]["offer"] == 2
    assert result["reason"] == "По отклику пришёл оффер"


def test_offer_requires_sent_status(client: TestClient) -> None:
    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/4",
            "status": "offer",
        },
    )

    assert response.status_code == 409
    assert "только после" in response.json()["detail"]


@pytest.mark.parametrize(
    "url",
    ["https://hh.ru/vacancy/2", "https://hh.ru/vacancy/3"],
)
def test_terminal_status_cannot_be_changed(client: TestClient, url: str) -> None:
    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": url,
            "status": "sent",
        },
    )

    assert response.status_code == 409
    assert "терминальный" in response.json()["detail"]


def test_rejected_status_is_terminal(client: TestClient) -> None:
    first = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "rejected",
        },
    )
    second = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "sent",
        },
    )

    assert first.status_code == 200
    assert first.json()["reason"] == "По отклику пришёл отказ"
    assert second.status_code == 409
    assert "терминальный" in second.json()["detail"]


def test_static_theme_is_served(client: TestClient) -> None:
    styles = client.get("/static/styles.css")
    script = client.get("/static/script.js")

    assert styles.status_code == 200
    assert ".theme-toggle" in styles.text
    assert script.status_code == 200
    assert "THEME_KEY" in script.text


def test_common_styles_live_in_one_file(client: TestClient) -> None:
    """Палитра и общие блоки — в styles.css, без копий в шаблонах."""
    styles = client.get("/static/styles.css").text

    for block in (":root", '[data-theme="dark"]', ".header", ".header--row",
                  ".filters", ".filter-btn", ".count-badge"):
        assert block in styles

    for page in ("/", "/metrics", "/report"):
        text = client.get(page).text
        assert 'href="/static/styles.css"' in text
        assert ":root" not in text


def test_favicon_is_linked_and_served(client: TestClient) -> None:
    for page in ("/", "/metrics", "/report"):
        response = client.get(page)
        assert response.status_code == 200
        assert 'rel="icon"' in response.text
        assert 'href="/static/favicon.svg"' in response.text

    icon = client.get("/static/favicon.svg")
    assert icon.status_code == 200
    assert "image/svg+xml" in icon.headers["content-type"]
    assert "<svg" in icon.text


def test_old_index_link_redirects(client: TestClient) -> None:
    response = client.get("/index.html", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/"


def test_empty_reports_show_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    client = TestClient(web_main.app)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "нет данных" in response.text
    assert client.get("/api/v1/metrics").json()["total"] == 0
