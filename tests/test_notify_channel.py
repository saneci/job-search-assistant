"""Тесты письма о капче: название сайта берётся из канала."""

from __future__ import annotations

from typing import Any

from agent.config import EmailConfig, NotificationConfig
from agent.notify import notify_captcha


def make_email() -> EmailConfig:
    return EmailConfig(
        enabled=True,
        smtp_host="smtp.example.com",
        sender="from@example.com",
        recipient="to@example.com",
        username="from@example.com",
        password="secret",
    )


def capture_email(monkeypatch: Any) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []

    def fake_send_email(
        config: EmailConfig,
        subject: str,
        text: str,
        recipient: str = "",
    ) -> tuple[bool, str]:
        calls.append((recipient or config.recipient, subject, text))
        return True, "письмо отправлено"

    monkeypatch.setattr("agent.notify.send_email", fake_send_email)
    return calls


def test_habr_captcha_email_mentions_habr(monkeypatch: Any) -> None:
    calls = capture_email(monkeypatch)

    notify_captcha(
        make_email(),
        NotificationConfig(),
        note="Капча в форме отклика.",
        site="career.habr.com",
    )

    subject, body = calls[0][1], calls[0][2]
    assert "career.habr.com" in subject
    assert "career.habr.com" in body
    assert "hh.ru" not in subject
