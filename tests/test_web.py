"""Тесты веб-интерфейса: главная, воронка, отчёт и ручной статус offer."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.config import DEFAULT_LOGS_DIR
import web.main as web_main


@pytest.fixture(autouse=True)
def isolate_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Правка статуса пишет событие в журнал: в тестах — во временный каталог."""
    monkeypatch.setattr(web_main, "LOGS_DIR", tmp_path / "logs")


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


def test_record_date_uses_created_at_only() -> None:
    """Период воронки считается по `created_at`: других источников даты нет."""
    record = {
        "created_at": "2026-10-24T10:00:00+03:00",
        "updated_at": "2026-10-25T10:00:00+03:00",
    }

    assert web_main.record_date(record) == date(2026, 10, 24)
    assert web_main.record_date({"updated_at": "2026-10-25T10:00:00+03:00"}) is None
    assert web_main.record_date({}) is None


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
    assert "Период воронки считается по дате первой обработки вакансии в приложении" in response.text


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


def test_report_filters_are_single_row_menus(client: TestClient) -> None:
    """Фильтры отчёта — одна линия: сброс, дата, статус, источник, поиск, счётчик."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert 'class="filters filters--inline"' in html
    ids = ("filterReset", "dateToggle", "statusToggle", "channelToggle",
           "searchInput", "countBadge")
    positions = [html.index(f'id="{element_id}"') for element_id in ids]
    assert positions == sorted(positions), "порядок элементов в линии фильтров изменился"
    assert 'class="dropdown-menu"' in html
    assert 'aria-haspopup="true"' in html
    # Дата переехала из шапки в общую панель фильтров.
    assert "header-filters" not in html
    assert 'id="statusMenu"' in html and 'data-status="skipped"' in html
    assert 'id="channelMenu"' in html and 'data-channel="habr"' in html


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


def test_sent_status_sets_reason(client: TestClient, tmp_path: Path) -> None:
    """Ручной перевод в «Отправлено» не оставляет причину прежнего статуса."""
    path = tmp_path / "2026-10-24.json"
    payload = web_main.read_report(path)
    payload["vacancies"][3]["reason"] = "старая причина прежнего статуса"
    web_main.write_report(path, payload)

    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/4",
            "status": "sent",
        },
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "отклик уже отправлен"
    row = web_main.find_vacancy("https://hh.ru/vacancy/4", None)
    assert row is not None
    assert row["status"] == "sent"
    assert row["reason"] == "отклик уже отправлен"


def test_sent_status_keeps_agent_reason(client: TestClient, tmp_path: Path) -> None:
    """Повторное «Отправлено» не затирает причину, записанную агентом."""
    path = tmp_path / "2026-10-24.json"
    payload = web_main.read_report(path)
    payload["vacancies"][0]["reason"] = "последнее предложение письма"
    web_main.write_report(path, payload)

    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "sent",
        },
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "последнее предложение письма"


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


def test_invite_status_can_be_set_from_sent(client: TestClient) -> None:
    """Приглашение ставится из «Отправлено» и запоминает момент приглашения."""
    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "invite",
        },
    )

    assert response.status_code == 200
    assert response.json()["summary"]["invite"] == 1
    row = web_main.find_vacancy("https://hh.ru/vacancy/1", None)
    assert row is not None
    assert row["status"] == "invite"
    assert row["invited_at"]


def test_invite_requires_sent_status(client: TestClient) -> None:
    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/4",
            "status": "invite",
        },
    )

    assert response.status_code == 409
    assert "только после" in response.json()["detail"]


def test_offer_can_be_set_after_invite(client: TestClient) -> None:
    """Оффер ставится из «Приглашения»: переход invite → offer не терминальный."""
    invite = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "invite",
        },
    )
    offer = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "offer",
        },
    )

    assert invite.status_code == 200
    assert offer.status_code == 200
    assert offer.json()["reason"] == "По отклику пришёл оффер"


def test_report_page_shows_invite_status(client: TestClient) -> None:
    response = client.get("/report")

    assert response.status_code == 200
    assert "invite: 'Приглашение'" in response.text
    assert 'id="sumInvited"' in response.text


def test_metrics_page_shows_only_percent_cards(client: TestClient) -> None:
    """Вверху только проценты: абсолютного счётчика приглашений нет."""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "Конверсия в отклик" in response.text
    assert "Дошло до приглашения" not in response.text


def test_metrics_legend_uses_numbers_for_empty_value(client: TestClient) -> None:
    """Нулевой статус в легенде — число: на графике должен стоять ноль."""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "Приглашения (0)" in response.text
    assert "Приглашения (—)" not in response.text


def test_metrics_fate_segments_show_only_percent(client: TestClient) -> None:
    """Подписи сегментов «Судьбы откликов» — только проценты, без названий статусов."""
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "<span>33.3%</span>" in response.text
    assert "<span>Без ответа:" not in response.text
    assert "<span>Приглашения:" not in response.text
    # Названия статусов остались в легенде под графиком.
    assert "Без ответа (1)" in response.text


def test_metrics_page_filters_by_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Фильтр по резюме оставляет только записи выбранного профиля."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    java = vacancy("1", "sent")
    java["profile"] = "Java Developer"
    lead = vacancy("2", "rejected")
    lead["profile"] = "Team Lead"
    write_report(tmp_path, "2026-10-24.json", [java, lead])
    client = TestClient(web_main.app)

    response = client.get("/metrics", params={"profile": "java developer"})

    assert response.status_code == 200
    assert "1 отклик из 1 вакансии" in response.text
    # Ссылки периода сохраняют выбранное резюме.
    assert "period=month&amp;profile=java%20developer" in response.text
    assert client.get(
        "/api/v1/metrics", params={"profile": "java developer"}
    ).json()["total"] == 1


def test_metrics_profile_filter_labels_include_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """В меню резюме видно источник, а записи без резюме выделены отдельным пунктом."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    java = vacancy("1", "sent")
    java["profile"] = "Java Developer"
    java["channel"] = "hh"
    habr = vacancy("2", "skipped")
    habr["channel"] = "habr"
    write_report(tmp_path, "2026-10-24.json", [java, habr])
    client = TestClient(web_main.app)

    page = client.get("/metrics")

    assert page.status_code == 200
    assert "hh.ru: Java Developer" in page.text
    assert "Habr Career: без резюме" in page.text

    # Пункт «Без резюме» фильтрует записи своего канала.
    filtered = client.get("/api/v1/metrics", params={"profile": "без резюме: habr"})
    assert filtered.json()["total"] == 1


def test_metrics_page_keeps_cleared_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой параметр — «ничего не выбрано»: галочки сняты, данные не режутся."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    first = vacancy("1", "sent")
    first["profile"] = "Java Developer"
    second = vacancy("2", "skipped")
    second["profile"] = "Team Lead"
    write_report(tmp_path, "2026-10-24.json", [first, second])
    client = TestClient(web_main.app)

    response = client.get("/metrics", params={"profile": ""})

    assert response.status_code == 200
    assert "Ничего не выбрано" in response.text
    assert "Все" in response.text
    assert "2 вакансии" in response.text


def test_static_theme_is_served(client: TestClient) -> None:
    styles = client.get("/static/base.css")
    script = client.get("/static/script.js")

    assert styles.status_code == 200
    assert ".theme-toggle" in styles.text
    assert script.status_code == 200
    assert "THEME_KEY" in script.text


def test_static_assets_are_cached_and_html_revalidated(client: TestClient) -> None:
    """Статика с версией кэшируется навсегда, HTML перепроверяется, API — без кэша."""
    page = client.get("/report")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert "/static/base.css?v=" in page.text
    assert "/static/report.css?v=" in page.text

    asset = client.get("/static/base.css", params={"v": "1"})
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"

    api = client.get("/api/v1/vacancies")
    assert api.headers["cache-control"] == "no-store"


def test_styles_are_split_into_modules(client: TestClient) -> None:
    """Палитра и общие блоки — в base.css, у каждой страницы свой модуль стилей."""
    base = client.get("/static/base.css").text

    for block in (":root", '[data-theme="dark"]', ".header", ".header--row",
                  ".filters", ".filter-btn", ".count-badge"):
        assert block in base

    for page, module in (("/", "index.css"), ("/metrics", "metrics.css"), ("/report", "report.css")):
        text = client.get(page).text
        assert "/static/base.css?v=" in text
        assert f"/static/{module}?v=" in text
        assert ":root" not in text


def test_favicon_is_linked_and_served(client: TestClient) -> None:
    for page in ("/", "/metrics", "/report"):
        response = client.get(page)
        assert response.status_code == 200
        assert 'rel="icon"' in response.text
        assert 'href="/static/favicon.svg?v=' in response.text

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


def test_status_select_colors_come_from_variables(client: TestClient) -> None:
    """Цвета селектов статуса берутся из переменных темы, хардкодов в модуле нет."""
    css = client.get("/static/report.css").text.replace("\r\n", "\n")

    for status in ("sent", "skipped", "queued", "excluded", "error", "rejected", "offer"):
        assert f".status-select.{status} {{\n    --select-tone: var(--color-{status});" in css
    assert ".status-select.invite {\n    --select-tone: var(--color-invite-ink);" in css
    assert "color-mix(in srgb, var(--select-tone, var(--color-base))" in css
    for hardcoded in ("#e8f5e9", "#fff3e0", "#e3f2fd", "#f3e5f5", "#fce4ec", "#1e3a24"):
        assert hardcoded not in css
    assert '[data-theme="dark"] .status-select' not in css


def test_table_cells_wrap_long_text(client: TestClient) -> None:
    """Длинные причины и названия компаний переносятся внутри ячейки, а не наезжают на соседнюю."""
    css = client.get("/static/report.css").text

    assert "overflow-wrap: break-word" in css


def test_report_page_has_archived_filter_and_warning(client: TestClient) -> None:
    """«В архиве» есть в фильтре статуса, а перевод в архив просит подтверждения."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert 'data-status="archived"' in html
    assert "В архиве" in html
    # Архив терминальный: подтверждение то же, что у отказа и оффера.
    assert "Зафиксировать архив?" in html
    assert "терминальный" in html
    assert "confirm: 'Зафиксировать архив'" in html


def test_archived_vacancy_shows_archive_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Причина архивной записи архивная, даже если в файле сохранилась старая."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    item = vacancy("1", "error")
    item["reason"] = "VacancyParseError: старая ошибка разбора"
    item["archived"] = True
    write_report(tmp_path, "2026-10-24.json", [item])
    client = TestClient(web_main.app)

    response = client.get("/api/v1/vacancies")

    assert response.status_code == 200
    row = response.json()["vacancies"][0]
    assert row["reason"] == web_main.ARCHIVE_REASON
    assert row["status"] == "error"
    assert row["archived"] is True
    # Файл отчёта не переписан: архивная причина подставляется при чтении.
    stored = json.loads((tmp_path / "2026-10-24.json").read_text(encoding="utf-8"))
    assert stored["vacancies"][0]["reason"] == "VacancyParseError: старая ошибка разбора"


def test_archive_warning_explains_status_and_reason(client: TestClient) -> None:
    """Подтверждение архива объясняет, что меняется, а что остаётся прежним."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert "статус останется прежним, причина станет архивной" in html
    assert "Обычный статус снимает признак архива" not in html


def test_rejected_warning_explains_consequence(client: TestClient) -> None:
    """Подтверждение отказа объясняет, что меняется, а что уже нельзя сделать."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert "Вакансия получит статус «Отказ»" in html
    assert "причина станет «По отклику пришёл отказ»" in html
    assert "перевести вакансию в другой статус будет нельзя" in html


def test_date_filter_uses_dropdown_option_style(client: TestClient) -> None:
    """Меню «Дата» — те же пункты с галочкой, что у статуса и источника."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert 'id="dateAll"' in html
    assert 'class="dropdown-option" type="button" id="dateAll"' in html
    assert 'class="dropdown-check"' not in html
    assert 'type="checkbox"' not in html


def test_status_cell_uses_rounded_dropdown_menu(client: TestClient) -> None:
    """Статус в строке — своё меню со скруглением, а не нативный список."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    # Нативный <select> браузер рисует прямыми углами, поэтому его нет.
    assert "createElement('select')" not in html
    assert "function createStatusCell" in html
    assert "function openStatusMenu" in html
    assert "className = 'dropdown-menu status-menu'" in html


def test_archive_status_is_terminal_in_menu(client: TestClient) -> None:
    """«В архиве» терминальный: пункт есть, но из архива выйти нельзя."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert "new Set(['rejected', 'offer', 'archived'])" in html
    # Архивная строка не открывает меню.
    assert "TERMINAL_STATUSES.has(baseStatus) || Boolean(v.archived)" in html


def test_date_menu_collects_several_dates(client: TestClient) -> None:
    """Меню «Дата» выбирает несколько дат подряд и применяет выбор при закрытии."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert "let dateDraft = null;" in html
    assert "function startDateSelection" in html
    assert "function commitDateSelection" in html
    assert "sameDateSelection" in html
    # Меню закрывается, когда мышь уходит из меню и кнопки.
    assert "dateDropdown.root.addEventListener('mouseleave'" in html


def test_filter_menus_close_when_mouse_leaves(client: TestClient) -> None:
    """Меню статуса и источника закрываются, когда мышь ушла с меню и кнопки."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert "DROPDOWNS.forEach(dropdown => {" in html
    assert "if (dropdown === dateDropdown) return;" in html
    assert "dropdown.root.addEventListener('mouseleave'" in html


def test_status_menu_closes_when_mouse_leaves(client: TestClient) -> None:
    """Меню статуса в строке закрывается при уходе мыши, если пункт не выбран."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert "menu.addEventListener('mouseleave'" in html
    assert "control.addEventListener('mouseleave'" in html
    assert "if (!statusMenu || statusMenu.element !== menu) return;" in html
    assert "if (menu.matches(':hover') || control.matches(':hover')) return;" in html


def test_dropdown_menu_scrolls_inside_rounded_panel(client: TestClient) -> None:
    """Прокрутка идёт во внутренней части меню: полоса не срезает скругление."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert html.count('class="dropdown-scroll"') == 3
    css = Path("web/static/report.css").read_text(encoding="utf-8")
    assert ".dropdown-menu::-webkit-scrollbar" not in css
    assert ".dropdown-scroll::-webkit-scrollbar-track" in css


def test_dropdown_scrollbar_does_not_square_corners(client: TestClient) -> None:
    """Классическая полоса с кнопками не возвращается: она закрывает скругления."""
    css = client.get("/static/report.css").text

    base_block = css.split(".dropdown-scroll {", 1)[1].split("}", 1)[0]
    assert "scrollbar-width" not in base_block
    assert "scrollbar-color" not in base_block
    # Для Firefox тонкая полоса остаётся: своих псевдоэлементов прокрутки там нет.
    assert "@supports not selector(::-webkit-scrollbar)" in css


def test_status_menu_scroll_does_not_close_menu(client: TestClient) -> None:
    """Меню статуса вмещает все пункты, а прокрутка внутри него его не закрывает."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert "if (target instanceof Element && target.closest('.status-menu')) return;" in html
    css = client.get("/static/report.css").text
    # Высота меню: девять статусов помещаются без прокрутки, на низком экране остаётся прокрутка.
    assert "max-height: min(380px, calc(100vh - 16px))" in css
    assert "max-height: min(368px, calc(100vh - 28px))" in css


def test_summary_cards_skip_archived_vacancies(client: TestClient) -> None:
    """Карточки сводки не считают архивные записи: у них остался прежний статус."""
    response = client.get("/report")

    assert response.status_code == 200
    html = response.text
    assert "summaryCounts" in html
    # Итоги карточек берутся из загруженных записей, а не из серверного summary.
    assert "renderSummary(data.summary)" not in html


def test_update_status_marks_vacancy_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«В архиве» — признак, а не статус: статус и счётчик попыток не меняются."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    today = tmp_path / "today.json"
    monkeypatch.setattr(web_main, "current_report_path", lambda: today)
    write_report(tmp_path, "2026-10-24.json", [vacancy("1", "skipped")])
    client = TestClient(web_main.app)

    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "archived",
        },
    )

    assert response.status_code == 200
    assert response.json()["archived"] is True

    row = json.loads(today.read_text(encoding="utf-8"))["vacancies"][0]
    assert row["status"] == "skipped"
    assert row["archived"] is True
    # У архивной записи всегда одна и та же причина.
    assert row["reason"] == web_main.ARCHIVE_REASON
    # Перевод в архив — не попытка отклика: счётчик не растёт.
    assert row["attempts"] == 1


def test_update_status_from_archive_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«В архиве» терминальный: вернуть запись в другой статус нельзя."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    today = tmp_path / "today.json"
    monkeypatch.setattr(web_main, "current_report_path", lambda: today)
    item = vacancy("1", "skipped")
    item["archived"] = True
    write_report(tmp_path, "2026-10-24.json", [item])
    client = TestClient(web_main.app)

    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "queued",
        },
    )

    assert response.status_code == 409
    assert "терминальный" in response.json()["detail"]
    # Запись не тронута и не перенесена в отчёт сегодняшней даты.
    assert not today.exists()
    row = json.loads((tmp_path / "2026-10-24.json").read_text(encoding="utf-8"))[
        "vacancies"
    ][0]
    assert row["status"] == "skipped"
    assert row["archived"] is True


def test_update_status_writes_ui_event_to_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ручная правка статуса попадает в jsonl-журнал агента."""
    monkeypatch.setattr(web_main, "REPORTS_DIR", tmp_path)
    today = tmp_path / "today.json"
    monkeypatch.setattr(web_main, "current_report_path", lambda: today)
    write_report(tmp_path, "2026-10-24.json", [vacancy("1", "sent")])
    client = TestClient(web_main.app)

    response = client.post(
        "/api/v1/update-status",
        json={
            "report_name": "2026-10-24.json",
            "url": "https://hh.ru/vacancy/1",
            "status": "rejected",
        },
    )

    assert response.status_code == 200
    journal = web_main.LOGS_DIR / f"{date.today().isoformat()}.jsonl"
    events = [
        json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["event"] == "ui_status_update"
    assert events[-1]["run_id"] == web_main.WEB_RUN_ID
    assert re.fullmatch(r"\d{14}", events[-1]["run_id"])
    assert events[-1]["url"] == "https://hh.ru/vacancy/1"
    assert events[-1]["old_status"] == "sent"
    assert events[-1]["new_status"] == "rejected"
    assert "profile" not in events[-1]
    assert "company" not in events[-1]


def test_web_journal_is_written_to_project_logs_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Журнал правок лежит в data/logs: там же журналы агента и чистка по сроку."""
    monkeypatch.undo()
    assert web_main.LOGS_DIR == web_main.PROJECT_ROOT / "data" / "logs"
    assert web_main.LOGS_DIR == DEFAULT_LOGS_DIR
