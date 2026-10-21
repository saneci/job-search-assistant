"""Отчёт о запуске: текст, детальный JSON и отправка на email."""

from __future__ import annotations

import json
import smtplib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .config import EmailConfig
from .models import RunError, SentVacancy, SkippedVacancy


@dataclass
class RunStats:
    """Итоги одного запуска."""

    started_at: datetime
    finished_at: datetime | None = None
    sent: list[SentVacancy] = field(default_factory=list)
    skipped: list[SkippedVacancy] = field(default_factory=list)
    queued: list[dict[str, Any]] = field(default_factory=list)
    errors: list[RunError] = field(default_factory=list)
    stop_reason: str = ""

    @property
    def sent_count(self) -> int:
        return len(self.sent)


def build_report(stats: RunStats, details_path: Path, sent_limit: int = 3) -> str:
    """Формирует текст отчёта по разделу 3.6 требований."""
    finished = stats.finished_at or datetime.now()
    lines = [
        f"Отчёт за {stats.started_at.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Отправлено откликов: {len(stats.sent)}",
        f"Пропущено (не подошло): {len(stats.skipped)}",
        f"Отложено (тесты/анкеты): {len(stats.queued)}",
    ]
    if stats.errors:
        lines.append(f"Ошибок при обработке: {len(stats.errors)}")
    if stats.stop_reason:
        lines.append(f"Причина остановки: {stats.stop_reason}")
    lines.append(f"Длительность: {_duration(stats.started_at, finished)}")
    lines.append("")

    if stats.sent:
        lines.append("Последние отправленные:")
        for index, vacancy in enumerate(stats.sent[-sent_limit:], start=1):
            lines.append(f"{index}. {vacancy.title} — {vacancy.company or 'компания не указана'}")
            lines.append(f"   {vacancy.url}")
    else:
        lines.append("Отправленных откликов нет.")

    lines.append("")
    lines.append(f"Файл с деталями: {details_path}")
    return "\n".join(lines)


def build_details(stats: RunStats, run_id: str) -> dict[str, Any]:
    """Собирает детальный JSON запуска."""
    return {
        "run_id": run_id,
        "started_at": stats.started_at.isoformat(timespec="seconds"),
        "finished_at": stats.finished_at.isoformat(timespec="seconds") if stats.finished_at else None,
        "stop_reason": stats.stop_reason,
        "summary": {
            "sent": len(stats.sent),
            "skipped": len(stats.skipped),
            "queued": len(stats.queued),
            "errors": len(stats.errors),
        },
        "sent": [asdict(item) for item in stats.sent],
        "skipped": [asdict(item) for item in stats.skipped],
        "queued": stats.queued,
        "errors": [asdict(item) for item in stats.errors],
    }


def write_details(path: Path, payload: dict[str, Any]) -> None:
    """Сохраняет детальный JSON запуска."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def send_email(
    config: EmailConfig,
    subject: str,
    body: str,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """Отправляет отчёт на email через SMTP. Возвращает (успех, пояснение)."""
    if not config.enabled:
        return False, "отправка отчёта на email отключена (notifications.email.enabled: false)"

    required = {
        "notifications.email.smtp_host": config.smtp_host,
        "notifications.email.sender": config.sender,
        "notifications.email.recipient": config.recipient,
        "SMTP_PASSWORD": config.password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        return False, "не заданы параметры SMTP: " + ", ".join(missing)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = config.recipient
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
                server.login(config.username, config.password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        return False, f"не удалось отправить email: {exc}"

    return True, f"отчёт отправлен на {config.recipient}"


def _duration(started_at: datetime, finished_at: datetime) -> str:
    seconds = max(int((finished_at - started_at).total_seconds()), 0)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {seconds} с"
    return f"{seconds} с"
