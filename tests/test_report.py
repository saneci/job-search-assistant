"""Тесты формирования отчёта."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from agent.config import EmailConfig
from agent.models import RunError, SentVacancy, SkippedVacancy
from agent.report import RunStats, build_details, build_report, send_email, write_details


def make_stats() -> RunStats:
    started_at = datetime(2026, 10, 21, 9, 44, 56)
    return RunStats(
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=12),
        sent=[
            SentVacancy(
                title="Python разработчик",
                company="Ozon",
                url="https://hh.ru/vacancy/1",
                probability=0.93,
            ),
            SentVacancy(
                title="Backend engineer",
                company="Яндекс",
                url="https://hh.ru/vacancy/2",
                probability=0.88,
            ),
        ],
        skipped=[
            SkippedVacancy(
                title="1С разработчик",
                company="Ромашка",
                url="https://hh.ru/vacancy/3",
                reason="стоп-слово в названии: 1С",
            )
        ],
        queued=[{"url": "https://hh.ru/vacancy/4", "title": "Java", "company": "VK", "reason": "test"}],
        errors=[RunError(url="https://hh.ru/vacancy/5", title="Go", message="timeout")],
        stop_reason="достигнут дневной лимит откликов (20)",
        reviewed=12,
    )


def test_build_report(tmp_path: Path) -> None:
    details_path = tmp_path / ".logs" / "2026-10-21-094456.json"

    report = build_report(make_stats(), details_path)
    lines = report.splitlines()

    assert lines[0] == "=" * 33
    assert lines[1] == "Отчёт за 2026-10-21 09:44"
    assert lines[2] == "=" * 33
    assert "- Обработано вакансий: 12" in lines
    assert "- Отправлено откликов: 2" in lines
    assert "- Пропущено (не подошло): 1" in lines
    assert "- Отложено (тесты/анкеты): 1" in lines
    assert "- Ошибок при обработке: 1" in lines
    assert "- Причина остановки: достигнут дневной лимит откликов (20)" in lines
    assert any(line.startswith("- Длительность: ") for line in lines)
    assert "1. Python разработчик — Ozon" in report
    assert "https://hh.ru/vacancy/1" in report
    assert str(details_path) in report


def test_build_report_without_sent() -> None:
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 44, 56))

    report = build_report(stats, Path("details.json"))

    assert "Отправленных откликов нет." in report


def test_build_details_and_write(tmp_path: Path) -> None:
    details_path = tmp_path / "run.json"

    payload = build_details(make_stats(), "2026-10-21-094456")
    write_details(details_path, payload)

    assert payload["summary"] == {
        "reviewed": 12,
        "sent": 2,
        "skipped": 1,
        "queued": 1,
        "errors": 1,
    }
    assert payload["sent"][0]["probability"] == 0.93
    assert details_path.exists()
    assert '"run_id": "2026-10-21-094456"' in details_path.read_text(encoding="utf-8")


def test_send_email_disabled() -> None:
    config = EmailConfig(
        enabled=False,
        smtp_host="smtp.example.com",
        smtp_port=465,
        use_ssl=True,
        sender="from@example.com",
        recipient="to@example.com",
        username="from@example.com",
        password="secret",
    )

    sent, message = send_email(config, "subject", "body")

    assert sent is False
    assert "отключена" in message


def test_send_email_missing_credentials() -> None:
    config = EmailConfig(
        enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        use_ssl=True,
        sender="from@example.com",
        recipient="to@example.com",
        username="from@example.com",
        password="",
    )

    sent, message = send_email(config, "subject", "body")

    assert sent is False
    assert "SMTP" in message
