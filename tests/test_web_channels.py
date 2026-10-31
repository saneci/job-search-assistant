"""Тесты веб-интерфейса: канал вакансии в метриках и отчёте."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import web.main as web_main


def write_report(directory: Path, vacancies: list[dict[str, Any]]) -> None:
    payload = {
        "date": "2026-10-24",
        "updated_at": "2026-10-24T12:00:00+03:00",
        "summary": {},
        "vacancies": vacancies,
    }
    (directory / "2026-10-24.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def vacancy(url: str, channel: str) -> dict[str, Any]:
    return {
        "url": url,
        "title": "Java-разработчик",
        "company": "Ozon",
        "status": "sent",
        "attempts": 1,
        "created_at": "2026-10-24T10:00:00+03:00",
        "updated_at": "2026-10-24T10:00:00+03:00",
        "channel": channel,
    }


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    write_report(
        tmp_path,
        [
            vacancy("https://hh.ru/vacancy/1", "hh"),
            vacancy("https://career.habr.com/vacancies/2", "habr"),
            vacancy("https://career.habr.com/vacancies/3", "external"),
        ],
    )
    return TestClient(web_main.app)


def test_metrics_api_has_channels(client: TestClient) -> None:
    response = client.get("/api/v1/metrics", params={"period": "year"})

    assert response.status_code == 200
    channels = {item["key"]: item["count"] for item in response.json()["channels"]}
    assert channels == {"hh": 1, "habr": 1, "external": 1}


def test_report_table_columns_are_in_reading_order(client: TestClient) -> None:
    """Колонки идут от резюме к дате, причина стоит сразу после компании."""
    text = client.get("/report").text
    headers = [
        "<th>Резюме</th>",
        "<th>Вакансия</th>",
        "<th>Компания</th>",
        "<th>Причина / Комментарий</th>",
        "<th>Статус</th>",
        "<th>Дата</th>",
    ]
    positions = [text.index(header) for header in headers]

    assert positions == sorted(positions)
    assert "profile-cell" in text


def test_report_page_filters_by_channel(client: TestClient) -> None:
    """Канал выбирается фильтром по источнику: отдельной метки в строке нет."""
    response = client.get("/report")

    assert response.status_code == 200
    assert 'id="channelGroup"' in response.text
    assert 'data-channel="habr"' in response.text
    assert 'data-channel="external"' in response.text
    assert 'id="filterReset"' in response.text
    # Кнопка видна всегда, но без выбранных условий она неактивна.
    assert 'id="filterReset" type="button" disabled' in response.text
    # Сброс стоит в начале строки с поиском: до поля ввода.
    assert response.text.index('id="filterReset"') < response.text.index('id="searchInput"')
    assert "channel-badge" not in response.text
