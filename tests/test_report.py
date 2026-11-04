"""Тесты формирования отчёта."""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from agent.config import EmailConfig
from agent.models import REJECTED_REASON, RunError, SentVacancy, SkippedVacancy
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
    assert "- Отправлено откликов за запуск: 2" in lines
    assert "- Пропущено (не подошло): 1" in lines
    assert "- Отложено (тесты/анкеты): 1" in lines
    assert "- Ошибок при обработке: 1" in lines
    assert "- Причина остановки: достигнут дневной лимит откликов (20)" in lines
    assert any(line.startswith("- Длительность: ") for line in lines)
    assert f"Отчёт сохранён в файле: {report_path}" in report


def test_report_names_single_vacancy() -> None:
    """Заголовок отчёта называет вакансию, если запуск был по одной."""
    stats = RunStats(
        started_at=datetime(2026, 11, 4, 7, 27),
        finished_at=datetime(2026, 11, 4, 7, 30),
    )
    stats.sent = [
        SentVacancy(
            title="Fullstack QA (Java)",
            company="IBS",
            url="https://hh.ru/vacancy/138948221",
            probability=0.98,
        )
    ]
    stats.reviewed = 1

    lines = build_report(stats, vacancy_title="Fullstack QA (Java)").splitlines()

    assert lines[1] == "Отчёт за 2026-11-04 07:27 по вакансии «Fullstack QA (Java)»"


def test_report_names_single_resume() -> None:
    """Заголовок hh-запуска по одной вакансии называет резюме, а не вакансию."""
    stats = RunStats(
        started_at=datetime(2026, 11, 4, 19, 39),
        finished_at=datetime(2026, 11, 4, 19, 41),
    )
    stats.sent = [
        SentVacancy(
            title="Java-разработчик",
            company="Ozon",
            url="https://hh.ru/vacancy/1",
            probability=0.98,
        )
    ]
    stats.reviewed = 1

    lines = build_report(stats, resume_name="Java Developer").splitlines()

    assert lines[1] == "Отчёт за 2026-11-04 19:39 по резюме «Java Developer»"


def test_report_lists_rejections() -> None:
    """Отказы по откликам видно отдельной строкой отчёта."""
    stats = make_stats()
    stats.rejected.append(
        SkippedVacancy(
            title="Java-разработчик",
            company="Клиника",
            url="https://hh.ru/vacancy/136409645",
            reason=REJECTED_REASON,
        )
    )

    lines = build_report(stats).splitlines()

    assert "- Отказы по откликам: 1" in lines


def test_report_skips_empty_rejections_line() -> None:
    """Нет отказов — нет и строки: отчёт не обрастает нулями."""
    lines = build_report(make_stats()).splitlines()

    assert not any(line.startswith("- Отказы по откликам") for line in lines)


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


def test_report_shows_run_id_and_sent_for_day() -> None:
    """Отчёт CLI печатает идентификатор запуска и отклики в файле за день."""
    stats = make_stats()
    stats.run_id = "20261021094456"
    stats.report_sent = 6

    lines = build_report(stats).splitlines()

    assert "- Идентификатор запуска: 20261021094456" in lines
    assert "- Отправлено откликов за запуск: 2" in lines
    assert "- Отправлено откликов за день: 6" in lines


def test_report_shows_day_sent_even_when_equal() -> None:
    """Строка «за день» печатается всегда: по ней итог сходится с веб-отчётом."""
    stats = make_stats()
    stats.report_sent = 2

    lines = build_report(stats).splitlines()

    assert "- Отправлено откликов за день: 2" in lines
