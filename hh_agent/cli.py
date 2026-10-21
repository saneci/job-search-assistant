"""Командный интерфейс приложения."""

from __future__ import annotations

import argparse
import sys

from .browser import BrowserError, BrowserSession
from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from .logging_setup import RunLogger
from .pipeline import run
from .profile import ProfileError
from .queue import QueueError


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    with RunLogger(config.logs_dir) as logger:
        if args.login:
            try:
                with BrowserSession(config.browser, config.state_path, logger) as session:
                    session.login_manually()
            except BrowserError as exc:
                logger.event("login_error", error=str(exc))
                print(f"Не удалось сохранить сессию: {exc}", file=sys.stderr)
                return 1
            return 0

        if not config.llm.api_key:
            print(
                "Не задан AITUNNEL_API_KEY. Скопируйте .env.example в .env и укажите ключ.",
                file=sys.stderr,
            )
            return 2

        try:
            run(config, logger)
        except (BrowserError, ProfileError, QueueError) as exc:
            logger.event("fatal_error", error=f"{type(exc).__name__}: {exc}")
            print(f"Запуск прерван: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            logger.event("interrupted")
            print("Запуск прерван пользователем.", file=sys.stderr)
            return 130

    return 0
