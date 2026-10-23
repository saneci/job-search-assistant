"""Тесты email-уведомлений о капче."""

from __future__ import annotations

from agent.config import EmailConfig, NotificationConfig
from agent.notify import (
    notify_captcha,
    notify_test,
    notify_test_targets,
    send_notification,
)


def make_email(enabled: bool = True, password: str = "secret") -> EmailConfig:
    return EmailConfig(
        enabled=enabled,
        smtp_host="smtp.example.com",
        smtp_port=465,
        use_ssl=True,
        sender="from@example.com",
        recipient="to@example.com",
        username="from@example.com",
        password=password,
    )


def make_notify(enabled: bool = True, recipient: str = "") -> NotificationConfig:
    return NotificationConfig(enabled=enabled, recipient=recipient)


def capture_email(monkeypatch) -> list[tuple[str, str, str]]:
    """Подменяет SMTP: копит адрес, тему и текст писем, в сеть не ходит."""
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


def test_send_notification_marks_subject(monkeypatch) -> None:
    calls = capture_email(monkeypatch)

    sent, message = send_notification(make_email(), "to@example.com", "Тема", "Текст")

    assert sent is True
    assert calls[0] == ("to@example.com", "Тема", "Текст")
    assert message == "письмо отправлено"


def test_notify_captcha_contains_vacancy_and_url(monkeypatch) -> None:
    calls = capture_email(monkeypatch)

    sent, _ = notify_captcha(
        make_email(),
        make_notify(),
        title="Java-разработчик",
        company="Ozon",
        url="https://hh.ru/vacancy/777",
        note="Жду решения до 300 с.",
    )

    address, subject, body = calls[0]
    assert sent is True
    assert address == "to@example.com"
    assert subject == "Необходимо пройти капчу на hh.ru"
    assert "Вакансия: Java-разработчик — Ozon" in body
    assert "Ссылка: https://hh.ru/vacancy/777" in body
    assert "Жду решения до 300 с." in body


def test_notify_captcha_without_vacancy(monkeypatch) -> None:
    """Капча на странице поиска: вакансии нет, в письме только ситуация."""
    calls = capture_email(monkeypatch)

    notify_captcha(
        make_email(), make_notify(), note="Капча на странице поиска. Запуск остановлен."
    )

    _, subject, body = calls[0]
    assert subject == "Необходимо пройти капчу на hh.ru"
    assert "Вакансия:" not in body
    assert "Ссылка:" not in body
    assert "Запуск остановлен." in body


def test_notify_captcha_default_note(monkeypatch) -> None:
    calls = capture_email(monkeypatch)

    notify_captcha(make_email(), make_notify())

    assert "введите текст с картинки" in calls[0][2].lower()


def test_notify_captcha_uses_own_recipient(monkeypatch) -> None:
    """Раздельные настройки: у писем о капче может быть свой адрес."""
    calls = capture_email(monkeypatch)

    notify_captcha(make_email(), make_notify(recipient="captcha@example.com"))

    assert calls[0][0] == "captcha@example.com"


def test_notify_captcha_disabled_kind(monkeypatch) -> None:
    """Вид писем выключен — SMTP не трогаем, в пояснении путь ключа."""
    calls = capture_email(monkeypatch)

    sent, message = notify_captcha(make_email(), make_notify(enabled=False))

    assert sent is False
    assert "notifications.captcha.enabled" in message
    assert calls == []


def test_notify_captcha_disabled_channel() -> None:
    """Выключенный канал — понятное пояснение и никаких обращений в сеть."""
    sent, message = notify_captcha(make_email(enabled=False), make_notify())

    assert sent is False
    assert "отключена" in message


def test_notify_captcha_without_password() -> None:
    sent, message = notify_captcha(make_email(password=""), make_notify())

    assert sent is False
    assert "SMTP" in message


def test_notify_test_message(monkeypatch) -> None:
    calls = capture_email(monkeypatch)

    sent, _ = notify_test(make_email(), "to@example.com")

    _, subject, body = calls[0]
    assert sent is True
    assert subject == "Проверка уведомлений"
    assert "Тестовое письмо" in body


def test_test_targets_are_unique_and_respect_switches() -> None:
    channel = make_email()

    assert notify_test_targets(
        channel,
        make_notify(recipient="report@example.com"),
        make_notify(recipient="report@example.com"),
    ) == ("report@example.com",)

    assert notify_test_targets(channel, make_notify(), make_notify()) == ("to@example.com",)
    assert notify_test_targets(channel, make_notify(enabled=False), make_notify(enabled=False)) == ()
    assert notify_test_targets(
        channel,
        make_notify(recipient="report@example.com"),
        make_notify(recipient="captcha@example.com"),
    ) == ("report@example.com", "captcha@example.com")
