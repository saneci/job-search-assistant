"""Отчёт о запуске: текст, письмо на email и SMTP-транспорт."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from email.utils import formataddr

from .config import EmailConfig
from .models import RunError, SentVacancy, SkippedVacancy


@dataclass
class RunStats:
    """Итоги одного запуска.

    `report_sent` — отклики в файле дня на момент завершения запуска: в нём есть
    и отклики прошлых запусков этого дня, поэтому значение отличается от `sent`.
    """

    started_at: datetime
    finished_at: datetime | None = None
    sent: list[SentVacancy] = field(default_factory=list)
    skipped: list[SkippedVacancy] = field(default_factory=list)
    excluded: list[SkippedVacancy] = field(default_factory=list)
    rejected: list[SkippedVacancy] = field(default_factory=list)
    already_sent: list[SkippedVacancy] = field(default_factory=list)
    queued: list[dict[str, Any]] = field(default_factory=list)
    errors: list[RunError] = field(default_factory=list)
    questionary: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    cover_letters: dict[str, str] = field(default_factory=dict)
    stop_reason: str = ""
    reviewed: int = 0
    run_id: str = ""
    report_sent: int | None = None

    @property
    def sent_count(self) -> int:
        return len(self.sent)


def build_report(
    stats: RunStats,
    report_path: Path | None = None,
    vacancy_title: str = "",
    resume_name: str = "",
    channel_name: str = "",
    profile_name: str = "",
) -> str:
    """Формирует текст отчёта для консоли и письма.

    С `report_path=None` путь к файлу дня не добавляется — его печатает
    консоль отдельной строкой с меткой времени. В заголовок добавляется
    резюме для hh или вакансия для Habr, когда запуск обработал ровно одну.
    `channel_name` и `profile_name` печатаются отдельными строками в начале
    отчёта: по ним видно, где и с каким профилем шёл запуск.
    """
    finished = stats.finished_at or datetime.now()
    separator = "=" * 33
    header = f"Отчёт за {stats.started_at.strftime('%Y-%m-%d %H:%M')}"
    if resume_name:
        header += f" по резюме «{resume_name}»"
    elif vacancy_title:
        header += f" по вакансии «{vacancy_title}»"
    lines = [separator, header, separator]
    if channel_name:
        lines.append(f"- Канал поиска: {channel_name}")
    if profile_name:
        lines.append(f"- Профиль: {profile_name}")
    lines += [
        f"- Обработано вакансий: {stats.reviewed}",
        f"- Отправлено откликов за запуск: {len(stats.sent)}",
    ]
    if stats.report_sent is not None:
        lines.append(f"- Отправлено откликов за день: {stats.report_sent}")
    lines.append(f"- Пропущено (не подошло): {len(stats.skipped)}")
    if stats.excluded:
        lines.append(f"- Исключённые вакансии: {len(stats.excluded)}")
    if stats.rejected:
        lines.append(f"- Отказы по откликам: {len(stats.rejected)}")
    lines.append(f"- Отложено (тесты/анкеты): {len(stats.queued)}")
    if stats.errors:
        lines.append(f"- Ошибок при обработке: {len(stats.errors)}")
    if stats.stop_reason:
        lines.append(f"- Причина остановки: {stats.stop_reason}")
    lines.append(f"- Длительность: {_duration(stats.started_at, finished)}")
    if stats.run_id:
        lines.append(f"- Идентификатор запуска: {stats.run_id}")

    if not stats.sent:
        lines.append("")
        lines.append("Отправленных откликов нет.")

    if report_path is not None:
        lines.append("")
        lines.append(f"Отчёт сохранён в файле: {report_path}")
    return "\n".join(lines)


def send_email(
    config: EmailConfig,
    subject: str,
    body: str,
    timeout: float = 30.0,
    recipient: str = "",
) -> tuple[bool, str]:
    """Отправляет письмо через SMTP. Возвращает (успех, пояснение).

    `recipient` — адрес конкретного вида писем (отчёт, капча); пусто — общий
    из `notifications.email.recipient`.
    """
    if not config.enabled:
        return False, "отправка email отключена (notifications.email.enabled: false)"

    target = recipient or config.recipient
    password = config.password.get_secret_value()
    required = {
        "notifications.email.smtp_host": config.smtp_host,
        "notifications.email.sender": config.sender,
        "notifications.email.recipient": target,
        "SMTP_PASSWORD": password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        return False, "не заданы параметры SMTP: " + ", ".join(missing)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("Job Search Assistant", config.sender))
    message["To"] = target
    message.set_content(body)

    try:
        if config.use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                config.smtp_host, config.smtp_port, timeout=timeout
            )
        else:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=timeout)
        with server:
            if not config.use_ssl:
                server.starttls()
            if config.username:
                server.login(config.username, password)
            server.send_message(message)
    except smtplib.SMTPServerDisconnected as exc:
        return False, (
            f"SMTP-сервер закрыл соединение ({exc}). Обычно это блокировка входа "
            "по SMTP у провайдера: попробуйте другой smtp_host (например, smtp.yandex.ru) "
            "и пароль приложения для него."
        )
    except (OSError, smtplib.SMTPException) as exc:
        return False, f"не удалось отправить email: {exc}"

    return True, "письмо отправлено"


def _duration(started_at: datetime, finished_at: datetime) -> str:
    seconds = max(int((finished_at - started_at).total_seconds()), 0)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {seconds} с"
    return f"{seconds} с"
