"""Командный интерфейс приложения."""

from __future__ import annotations

import argparse
import subprocess
import sys

from .browser import BrowserError, BrowserSession
from .config import DEFAULT_CONFIG_PATH, AppConfig, ConfigError, load_config
from .logging_setup import RunLogger
from .models import RETRYABLE_STATUSES
from .notify import notify_test as send_test_notification
from .notify import notify_test_targets
from .pipeline import run
from .profile import ProfileError

# `--retry` без значения повторяет статусы из retry.statuses.
RETRY_CHOICES = ("all",) + RETRYABLE_STATUSES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hh-agent",
        description="Персональный агент автоматических откликов на hh.ru",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="открыть браузер, войти в аккаунт hh.ru и сохранить сессию",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="путь к YAML-конфигурации (по умолчанию config.yaml в корне проекта)",
    )
    parser.add_argument(
        "--install-browser",
        action="store_true",
        help="установить Chromium для Playwright (не нужен при browser.channel)",
    )
    parser.add_argument(
        "--notify-test",
        action="store_true",
        help="отправить тестовое письмо-уведомление и выйти (проверка SMTP-канала)",
    )
    parser.add_argument(
        "--retry",
        "--re",
        nargs="?",
        const="all",
        choices=RETRY_CHOICES,
        metavar="СТАТУС",
        help=(
            "повторить вакансии из дневных отчётов по прямой ссылке, без поиска; "
            "без значения — статусы из pipeline.retry.statuses, со значением — только он"
        ),
    )
    parser.add_argument(
        "--excluded",
        action="store_true",
        help="синоним «--retry excluded»: только исключённые вакансии (стоп-слова, компании)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    with RunLogger(config.data.paths.logs_dir) as logger:
        if args.install_browser:
            return _install_browser(config)

        if args.notify_test:
            return _notify_test(config, logger)

        if args.login:
            try:
                with BrowserSession(
                    config.browser, config.data.paths.session_state, logger
                ) as session:
                    session.login_manually()
            except BrowserError as exc:
                logger.event("login_error", error=str(exc))
                print(f"Не удалось сохранить сессию: {exc}", file=sys.stderr)
                return 1
            return 0

        if not config.llm.api_key.get_secret_value():
            print(
                "Не задан AITUNNEL_API_KEY. Скопируйте .env.example в .env и укажите ключ.",
                file=sys.stderr,
            )
            return 2

        try:
            run(config, logger, retry="excluded" if args.excluded else args.retry)
        except (BrowserError, ProfileError) as exc:
            logger.event("fatal_error", error=f"{type(exc).__name__}: {exc}")
            print(f"Запуск прерван: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            logger.event("interrupted")
            print("Запуск прерван пользователем.", file=sys.stderr)
            return 130

    return 0


def _notify_test(config: AppConfig, logger: RunLogger) -> int:
    """Проверяет канал уведомлений: письмо на каждый включённый вид писем."""
    targets = notify_test_targets(
        config.notifications.email,
        config.notifications.report,
        config.notifications.captcha,
    )
    if not targets:
        message = (
            "уведомления отключены: notifications.report.enabled и "
            "notifications.captcha.enabled в false"
        )
        logger.event("notify_test", sent=False, message=message)
        print(f"Письмо не отправлено: {message}", file=sys.stderr)
        return 1

    failures: list[str] = []
    for address in targets:
        sent, message = send_test_notification(config.notifications.email, address)
        logger.event("notify_test", sent=sent, message=message)
        if not sent and message not in failures:
            failures.append(message)

    if failures:
        print("Письмо не отправлено: " + "; ".join(failures), file=sys.stderr)
        return 1
    print(f"Тестовые письма отправлены: {len(targets)}.")
    return 0


def _install_browser(config: AppConfig) -> int:
    """Ставит Chromium для Playwright, если не выбран установленный браузер."""
    if config.browser.channel:
        print(
            f"Используется установленный браузер ({config.browser.channel}), "
            "загрузка Chromium не требуется."
        )
        return 0

    print("Устанавливаю Chromium для Playwright (может занять пару минут)...")
    return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
