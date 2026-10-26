"""Командный интерфейс приложения."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from typing import TextIO

from .browser import BrowserError, BrowserSession
from .config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    load_config,
)
from .logging_setup import RunLogger
from .models import (
    RETRYABLE_STATUSES,
    STATUSES,
    STATUS_OFFER,
    STATUS_REJECTED,
    STATUS_SENT,
)
from .notify import notify_test as send_test_notification
from .notify import notify_test_targets
from .pipeline import RETRY_ALL, run
from .profile import ProfileError

# `--retry` без значения повторяет статусы из retry.statuses.
RETRY_CHOICES = (RETRY_ALL,) + RETRYABLE_STATUSES

# Как в справке выглядит вызов `--retry` без значения.
RETRY_ALL_LABEL = "[NO OPS]"

# Флаги повтора и справки: по ним выбирается, какую справку показать.
RETRY_FLAGS = ("--retry", "--re")
EXCLUDED_FLAGS = ("--excluded",)
HELP_FLAGS = ("-h", "--help")

# Цвета повторяют тему argparse в Python 3.14+: пользователь видит один
# стиль и в справке, и в сообщениях об ошибках.
_COLOR_RESET = "\x1b[0m"
_COLOR_HEADING = "\x1b[1;34m"
_COLOR_OPTION = "\x1b[1;36m"
_COLOR_LABEL = "\x1b[1;33m"
_COLOR_ERROR = "\x1b[1;35m"


def _color_enabled(stream: TextIO | None = None) -> bool:
    """Можно ли включать ANSI-цвета для потока."""
    stream = sys.stdout if stream is None else stream
    if not sys.flags.ignore_environment:
        if os.environ.get("PYTHON_COLORS") == "0":
            return False
        if os.environ.get("PYTHON_COLORS") == "1":
            return True
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        if not stream.isatty():
            return False
    except (AttributeError, OSError):
        return False
    if sys.platform == "win32":
        try:
            import nt

            if not nt._supports_virtual_terminal():
                return False
        except (ImportError, AttributeError):
            return False
    return True


def _paint(text: str, color: str, enabled: bool) -> str:
    """Окрашивает текст, если цвет включён."""
    return f"{color}{text}{_COLOR_RESET}" if enabled else text


def _option_rows(
    rows: Sequence[tuple[str, str]],
    *,
    color: bool,
    label_color: str = _COLOR_OPTION,
) -> str:
    """Строки «опция  описание»: подписи выровнены по самой длинной."""
    width = max(len(label) for label, _ in rows)
    return "\n".join(
        f"  {_paint(label.ljust(width), label_color, color)}  {description}"
        for label, description in rows
    )


# Что значит каждый статус, который принимает `--retry`. Ключи — ровно
# RETRY_CHOICES: новый статус в models.RETRYABLE_STATUSES нельзя оставить
# без описания (проверяется тестом).
RETRY_STATUS_HELP = {
    RETRY_ALL: (
        "статусы из config.yaml: pipeline.retry.statuses "
        "(по умолчанию error)"
    ),
    "error": "сбой обработки: страница не открылась, ошибка браузера или парсинга",
    "queued": (
        "требуется ручное действие пользователя: тест, анкета, внешняя форма, "
        "капча, пустое письмо"
    ),
    "excluded": (
        "исключена по значению из config.yaml: search.exclude_companies "
        "или search.exclude_keywords"
    ),
    "skipped": (
        "не подошла по оценке LLM-Decision, либо вакансия в архиве или "
        "недоступна по другим причинам"
    ),
}

# Статусы отчёта, которые повторять нельзя: отклик уже ушёл или решение
# терминальное. Список выводится из статусов отчёта, а не переписывается руками.
NOT_RETRYABLE_STATUSES = tuple(
    status for status in STATUSES if status not in RETRYABLE_STATUSES
)
NOT_RETRYABLE_HELP = {
    STATUS_SENT: "отклик уже отправлен",
    STATUS_REJECTED: "пришёл отказ по отправленному отклику",
    STATUS_OFFER: "по отклику получен оффер",
}


def retry_statuses_help(color: bool | None = None) -> str:
    """Статусы, которые принимает `--retry`, — по строке на статус.

    Внутренний ключ `all` (статусы из конфига) показывается как `[NO OPS]`:
    именно так вызывается `--retry` без значения.
    """
    if color is None:
        color = _color_enabled()
    rows = [
        (RETRY_ALL_LABEL if status == RETRY_ALL else status, status)
        for status in RETRY_CHOICES
    ]
    return _option_rows(
        [(label, RETRY_STATUS_HELP[status]) for label, status in rows],
        color=color,
        label_color=_COLOR_LABEL,
    )


GENERAL_OPTIONS = (
    ("--help, -h", "показать справку и выйти"),
    (
        "--install-browser",
        "установить Chromium для Playwright, переопределяет значение из "
        "config.yaml: browser.channel",
    ),
    (
        "--login",
        "открыть браузер, войти в аккаунт hh.ru и сохранить сессию в data/state.json",
    ),
    (
        "--config CONFIG",
        "путь к YAML-конфигурации (по умолчанию config.yaml в корне проекта)",
    ),
    (
        "--[NO OPS]",
        "выполнить обычный цикл программы: поиск → оценка → отклики → отчёт",
    ),
    ("--retry, --re [СТАТУС]", "повторить обработку вакансий, без поиска"),
    (
        "--excluded",
        "повторить обработку только исключённых вакансий, синоним --retry excluded",
    ),
    (
        "--notify-test",
        "отправить тестовое оповещение о завершении работы по включённым "
        "видам уведомлений",
    ),
)


def general_help(color: bool | None = None) -> str:
    """Общая справка: команды запуска и подсказка про справку по опции."""
    if color is None:
        color = _color_enabled()
    rows = _option_rows(GENERAL_OPTIONS, color=color)
    return (
        f"{_paint('Персональный агент автоматических откликов на hh.ru', _COLOR_HEADING, color)}\n"
        "\n"
        f"{_paint('Опции:', _COLOR_HEADING, color)}\n"
        f"{rows}\n"
        "\n"
        "Для получения справки по конкретной опции нужно после опции ввести "
        "--help или -h, если документация есть она будет показана, пример: "
        "./run.sh --retry -h"
    )


def retry_help(color: bool | None = None) -> str:
    """Справка `--retry --help`: статусы повтора и неповторяемые статусы."""
    if color is None:
        color = _color_enabled()
    terminal = _option_rows(
        [(status, NOT_RETRYABLE_HELP[status]) for status in NOT_RETRYABLE_STATUSES],
        color=color,
        label_color=_COLOR_LABEL,
    )
    return (
        f"{_paint('Повторение обработки вакансий по списку прямых ссылок из отчётов, без поиска', _COLOR_HEADING, color)}\n"
        "\n"
        f"{_paint('Опции (принимается одно значение):', _COLOR_HEADING, color)}\n"
        f"{retry_statuses_help(color)}\n"
        "\n"
        f"{_paint('Для терминальных статусов повтор не производится:', _COLOR_HEADING, color)}\n"
        f"{terminal}"
    )


def excluded_help(color: bool | None = None) -> str:
    """Справка `--excluded --help`: только про исключённые вакансии."""
    if color is None:
        color = _color_enabled()
    config_rows = _option_rows(
        (
            ("search.exclude_companies", "компании-исключения"),
            ("search.exclude_keywords", "стоп-слова в названии вакансии"),
        ),
        color=color,
        label_color=_COLOR_LABEL,
    )
    return (
        f"{_paint('Повторение обработки только исключённых вакансий, без поиска', _COLOR_HEADING, color)}\n"
        "\n"
        f"{_paint('Синоним:', _COLOR_HEADING, color)} --retry excluded\n"
        "\n"
        "Берутся вакансии со статусом excluded из дневных отчётов data/report.\n"
        f"{_paint('Статус excluded ставится по значениям config.yaml:', _COLOR_HEADING, color)}\n"
        f"{config_rows}\n"
        "\n"
        "Пока компания или стоп-слово остаются в config.yaml, повтор снова "
        "завершится статусом excluded.\n"
        "\n"
        "Полная справка по повторам: ./run.sh --retry -h"
    )


def _first_flag(argv: Sequence[str], flags: tuple[str, ...]) -> int | None:
    """Позиция первого флага в argv; `--retry=queued` считается флагом `--retry`."""
    for index, token in enumerate(argv):
        if token in flags:
            return index
        if any(token.startswith(f"{flag}=") for flag in flags):
            return index
    return None


def help_requested(argv: Sequence[str]) -> bool:
    """Запрошена ли справка: `--help` или `-h` в любом месте аргументов."""
    return _first_flag(argv, HELP_FLAGS) is not None


def help_topic(argv: Sequence[str]) -> str:
    """Какую справку показать: general, retry или excluded.

    Справка конкретной опции показывается, только если эта опция стоит до
    `--help`/`-h`: `--retry -h` — про повтор, `-h --retry` — общая.
    """
    help_at = _first_flag(argv, HELP_FLAGS)
    if help_at is None:
        return "general"
    for token in reversed(argv[:help_at]):
        flag = token.split("=", 1)[0]
        if flag in EXCLUDED_FLAGS:
            return "excluded"
        if flag in RETRY_FLAGS:
            return "retry"
    return "general"


def retry_help_requested(argv: Sequence[str]) -> bool:
    """Запрошена ли справка именно по `--retry`/`--re`."""
    return help_topic(argv) == "retry"


def excluded_help_requested(argv: Sequence[str]) -> bool:
    """Запрошена ли справка именно по `--excluded`."""
    return help_topic(argv) == "excluded"


def help_text(argv: Sequence[str], color: bool | None = None) -> str:
    """Справка для набора аргументов: общая или по конкретной опции."""
    topic = help_topic(argv)
    if topic == "retry":
        return retry_help(color)
    if topic == "excluded":
        return excluded_help(color)
    return general_help(color)


class CliUsageError(RuntimeError):
    """Некорректные аргументы командной строки."""

    def __init__(
        self,
        reason: str,
        *,
        usage: str = "./run.sh [ОПЦИИ]",
        hint: str = "",
        help_hint: str = "./run.sh --help",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.usage = usage
        self.hint = hint
        self.help_hint = help_hint


def _translate_parser_error(message: str) -> CliUsageError:
    """Переводит типовые ошибки argparse в понятный русский текст."""
    if message.startswith("unrecognized arguments: "):
        value = message.removeprefix("unrecognized arguments: ")
        kind = "неизвестная опция" if value.lstrip().startswith("-") else "лишний аргумент"
        return CliUsageError(f"{kind}: {value}")
    if message.startswith("argument --config: expected one argument"):
        return CliUsageError(
            "опция --config требует значение",
            usage="./run.sh --config CONFIG",
        )
    return CliUsageError(message)


class _CliArgumentParser(argparse.ArgumentParser):
    """Парсер, который сообщает об ошибках в едином стиле приложения."""

    def error(self, message: str) -> None:
        raise _translate_parser_error(message)

    def _check_value(self, action: argparse.Action, value: object) -> None:
        if action.dest == "retry" and value not in action.choices:
            raise CliUsageError(
                f"неизвестный статус повтора: {value}",
                usage="./run.sh --retry [СТАТУС]",
                hint="Допустимые статусы: " + ", ".join(RETRYABLE_STATUSES),
                help_hint="./run.sh --retry -h",
            )
        super()._check_value(action, value)


def error_text(error: CliUsageError, color: bool | None = None) -> str:
    """Единый текст ошибки: что не так, как запускать и где справка."""
    if color is None:
        color = _color_enabled(sys.stderr)
    lines = [f"{_paint('Ошибка:', _COLOR_ERROR, color)} {error.reason}"]
    if error.hint:
        lines += ["", error.hint]
    lines += [
        "",
        f"{_paint('Использование:', _COLOR_HEADING, color)} {error.usage}",
        f"{_paint('Справка:', _COLOR_HEADING, color)} {error.help_hint}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = _CliArgumentParser(
        prog="run.py",
        description="Персональный агент автоматических откликов на hh.ru",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        help=argparse.SUPPRESS,
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
        const=RETRY_ALL,
        choices=RETRY_CHOICES,
        metavar="СТАТУС",
        help=(
            "повторить вакансии из дневных отчётов по прямой ссылке, без поиска; "
            "без значения — статусы из pipeline.retry.statuses, со значением — только он "
            "(статусы: --retry --help)"
        ),
    )
    parser.add_argument(
        "--excluded",
        action="store_true",
        help="синоним «--retry excluded»: только исключённые вакансии (стоп-слова, компании)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if help_requested(arguments):
        print(help_text(arguments))
        return 0
    try:
        args = build_parser().parse_args(arguments)
    except CliUsageError as exc:
        print(error_text(exc), file=sys.stderr)
        return 2

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
