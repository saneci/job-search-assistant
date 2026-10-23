"""Тесты формирования отчёта."""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from agent.config import EmailConfig
from agent.models import RunError, SentVacancy, SkippedVacancy
from agent.report import RunStats, build_report, send_email


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
                reason="Стоп-слово в названии: 1С",
            )
        ],
        queued=[{"url": "https://hh.ru/vacancy/4", "title": "Java", "company": "VK", "reason": "test"}],
        errors=[RunError(url="https://hh.ru/vacancy/5", title="Go", message="timeout")],
        stop_reason="достигнут дневной лимит откликов (20)",
        reviewed=12,
    )


def test_build_report(tmp_path: Path) -> None:
    report_path = tmp_path / "report" / "2026-10-21.json"

    report = build_report(make_stats(), report_path)
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
    assert f"Отчёт сохранён в файле: {report_path}" in report


def test_report_does_not_repeat_sent_vacancies() -> None:
    """Список отправленных живёт в файле дня — в отчёте его не дублируем."""
    report = build_report(make_stats())

    assert "Последние отправленные" not in report
    assert "Ozon" not in report
    assert "https://hh.ru/vacancy/1" not in report


def test_build_report_without_path() -> None:
    """Консоль печатает путь к файлу дня отдельной строкой с меткой времени."""
    report = build_report(make_stats(), None)

    assert "Отчёт сохранён в файле" not in report
    assert not report.endswith("\n")


def test_build_report_without_sent() -> None:
    stats = RunStats(started_at=datetime(2026, 10, 21, 9, 44, 56))

    report = build_report(stats, Path("report.json"))

    assert "Отправленных откликов нет." in report


def test_build_report_counts_excluded_vacancies() -> None:
    """Исключённые вакансии — отдельная строка: их видно и можно повторить."""
    stats = make_stats()
    stats.excluded.append(
        SkippedVacancy(
            title="Java-разработчик",
            company="Сбер. IT",
            url="https://hh.ru/vacancy/6",
            reason="Исключённая компания: Сбер. IT",
        )
    )

    report = build_report(stats)

    assert "- Исключённые вакансии: 1" in report


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


def test_send_email_explains_dropped_connection(monkeypatch) -> None:
    """Провайдер рвёт соединение на AUTH — подсказываем сменить smtp_host."""

    class BrokenServer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr("agent.report.smtplib.SMTP_SSL", BrokenServer)
    config = EmailConfig(
        enabled=True,
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
    assert "smtp.yandex.ru" in message
    assert "закрыл соединение" in message


def test_send_email_uses_kind_recipient(monkeypatch) -> None:
    """Адрес вида писем перекрывает общий notifications.email.recipient."""
    delivered: list[str] = []

    class FakeSMTP:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def login(self, username: str, password: str) -> None:
            pass

        def send_message(self, message: EmailMessage) -> None:
            delivered.append(str(message["To"]))

    monkeypatch.setattr("agent.report.smtplib.SMTP_SSL", FakeSMTP)
    config = EmailConfig(
        enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        use_ssl=True,
        sender="from@example.com",
        recipient="common@example.com",
        username="from@example.com",
        password="secret",
    )

    sent, message = send_email(config, "subject", "body", recipient="captcha@example.com")

    assert sent is True
    assert delivered == ["captcha@example.com"]
    # Адрес — контакт, в логи и сообщения он не попадает.
    assert "captcha@example.com" not in message
