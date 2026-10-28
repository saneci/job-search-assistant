"""Email-уведомления о событиях, которые требуют вмешательства человека.

Канал тот же, что у отчёта (`notifications.email`), а нужно ли присылать письма
о капче и на какой адрес, решает раздел `notifications.captcha`. Письмо
короткое — только вакансия и ссылка, без профиля, письма и контактов.
Отправка best-effort: её сбой не останавливает запуск.
"""

from __future__ import annotations

from .config import EmailConfig, NotificationConfig
from .report import send_email


def send_notification(
    channel: EmailConfig,
    recipient: str,
    subject: str,
    text: str,
) -> tuple[bool, str]:
    """Отправляет короткое письмо через SMTP-канал."""
    return send_email(channel, subject, text, recipient=recipient)


def notify_captcha(
    channel: EmailConfig,
    settings: NotificationConfig,
    *,
    title: str = "",
    company: str = "",
    url: str = "",
    note: str = "",
    site: str = "hh.ru",
) -> tuple[bool, str]:
    """Письмо «на сайте нужна капча»: где она и что сделать человеку.

    Пустые поля не печатаются: у капчи на странице поиска вакансии может не быть.
    """
    if not settings.enabled:
        return False, "уведомления о капче отключены (notifications.captcha.enabled: false)"

    label = f"{title} — {company}" if title and company else title
    subject = f"Необходимо пройти капчу на {site}"

    lines = [f"{site} просит пройти капчу.", ""]
    if label:
        lines.append(f"Вакансия: {label}")
    if url:
        lines.append(f"Ссылка: {url}")
    if label or url:
        lines.append("")
    lines.append(note or "Откройте окно браузера и введите текст с картинки.")
    body = "\n".join(lines)
    return send_notification(channel, settings.target(channel), subject, body)


def notify_test_targets(
    channel: EmailConfig,
    *kinds: NotificationConfig,
) -> tuple[str, ...]:
    """Адреса для проверки канала: по одному на включённый вид писем, без повторов."""
    addresses: list[str] = []
    for kind in kinds:
        if not kind.enabled:
            continue
        address = kind.target(channel)
        if address and address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def notify_test(channel: EmailConfig, recipient: str) -> tuple[bool, str]:
    """Тестовое письмо на конкретный адрес: проверка канала без браузера и LLM."""
    text = "\n".join(
        [
            "Тестовое письмо от hh-agent.",
            "",
            "Если вы его видите, канал уведомлений настроен.",
        ]
    )
    return send_notification(channel, recipient, "Проверка уведомлений", text)

