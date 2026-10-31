"""Тесты командной строки: справка, флаги повтора и проверка уведомлений."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.cli import (
    NOT_RETRYABLE_HELP,
    NOT_RETRYABLE_STATUSES,
    RETRY_ALL,
    RETRY_CHOICES,
    RETRY_STATUS_HELP,
    _notify_test,
    build_parser,
    CliUsageError,
    excluded_help,
    excluded_help_requested,
    general_help,
    help_requested,
    help_topic,
    main,
    retry_help,
    retry_help_requested,
)
from agent.config import load_config
from agent.hh_vacancy import ResumeNotFoundError
from agent.logging_setup import RunLogger

GENERAL_HELP_TEXT = """Персональный ассистент для поиска работы, который отсеивает нерелевантное и оставляет только то, что стоит вашего внимания

Опции:
  -h, --help                  показать справку и выйти
  --install-browser           установить Chromium для Playwright (не нужен при browser.channel)
  --login                     открыть браузер, войти на сайт выбранного канала и сохранить сессию
  --config <FILE>             путь к YAML-конфигурации, по умолчанию: config.yaml в корне проекта
  --channel, --ch <CHANNEL>   канал запуска: hh или habr, по умолчанию: search.channel из config.yaml
  -r, --retry, --re [STATUS]  повторить обработку вакансий, без поиска
  --excluded                  повторить обработку только исключённых вакансий, синоним --retry excluded
  --notify-test               отправить тестовое оповещение о завершении работы по включённым видам уведомлений

Для получения справки по конкретной опции нужно после опции ввести --help или -h, если документация есть она будет показана, пример: ./run.sh --retry -h

Examples:
  ./run.sh
  ./run.sh --channel habr
  ./run.sh --channel habr --retry
  ./run.sh --channel habr --re
  ./run.sh --retry queued
  ./run.sh --login --ch habr"""

RETRY_HELP_TEXT = """Повторение обработки вакансий по списку прямых ссылок из отчётов, без поиска

Опции (принимается одно значение):
  (без значения)  статусы из config.yaml: pipeline.retry.statuses (по умолчанию error)
  error           сбой обработки: страница не открылась, ошибка браузера или парсинга
  queued          требуется ручное действие пользователя: тест, анкета, внешняя форма, капча, пустое письмо
  excluded        исключена по значению из config.yaml: search.exclude_companies или search.exclude_keywords
  skipped         не подошла по оценке LLM-Decision, либо вакансия в архиве или недоступна по другим причинам

Для терминальных статусов повтор не производится:
  sent      отклик уже отправлен
  rejected  пришёл отказ по отправленному отклику
  offer     по отклику получен оффер
  invite    пришло приглашение на собеседование"""

EXCLUDED_HELP_TEXT = """Повторение обработки только исключённых вакансий, без поиска

Синоним: --retry excluded

Берутся вакансии со статусом excluded из дневных отчётов data/report.
Статус excluded ставится по значениям config.yaml:
  search.exclude_companies  компании-исключения
  search.exclude_keywords   стоп-слова в названии вакансии

Пока компания или стоп-слово остаются в config.yaml, повтор снова завершится статусом excluded.

Полная справка по повторам: ./run.sh --retry -h"""


@pytest.fixture(autouse=True)
def plain_output(monkeypatch) -> None:
    """В тестах сравниваем текст без ANSI; цвет проверяется отдельно."""
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("PYTHON_COLORS", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("TERM", raising=False)


def make_config(tmp_path: Path, notifications: dict | None = None):
    data = {
        "search": {"queries": "Java", "area": "Москва"},
        "pipeline": {
            "limit_per_run": 1,
            "delay_between_actions": {"from": 0, "to": 0},
        },
        "notifications": notifications
        or {
            "email": {
                "enabled": True,
                "smtp_host": "smtp.example.com",
                "sender": "from@example.com",
                "recipient": "to@example.com",
            }
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return load_config(path)


def read_events(logger: RunLogger) -> list[dict]:
    return [
        json.loads(line)
        for line in logger.events_path.read_text(encoding="utf-8").splitlines()
    ]


def test_parser_has_notify_test_flag() -> None:
    assert build_parser().parse_args(["--notify-test"]).notify_test is True


def test_parser_notify_test_defaults_to_false() -> None:
    assert build_parser().parse_args([]).notify_test is False


def test_parser_retry_flag_and_aliases() -> None:
    parser = build_parser()

    assert parser.parse_args(["--retry"]).retry == RETRY_ALL
    assert parser.parse_args(["-r"]).retry == RETRY_ALL
    assert parser.parse_args(["--retry", "all"]).retry == RETRY_ALL
    assert parser.parse_args(["--retry", "queued"]).retry == "queued"
    assert parser.parse_args(["--re"]).retry == RETRY_ALL
    assert parser.parse_args(["--excluded"]).excluded is True
    assert parser.parse_args([]).retry is None
    assert parser.parse_args([]).excluded is False


def test_parser_channel_flag_and_aliases() -> None:
    parser = build_parser()

    assert parser.parse_args([]).channel is None
    assert parser.parse_args(["--channel", "habr"]).channel == "habr"
    assert parser.parse_args(["--ch", "hh"]).channel == "hh"


def test_no_ops_option_is_removed() -> None:
    """`--no-ops` удалён: обычный цикл выполняется без этой опции."""
    with pytest.raises(CliUsageError):
        build_parser().parse_args(["--no-ops"])


def test_help_descriptions_cover_every_status() -> None:
    """У каждого статуса из --retry есть описание, лишних описаний нет."""
    assert set(RETRY_STATUS_HELP) == set(RETRY_CHOICES)
    assert set(NOT_RETRYABLE_HELP) == set(NOT_RETRYABLE_STATUSES)


def test_general_help_matches_spec() -> None:
    """Общая справка — ровно заданный текст, без usage от argparse."""
    assert general_help(color=False) == GENERAL_HELP_TEXT
    assert "usage:" not in general_help(color=False)


def test_config_uses_file_placeholder() -> None:
    """Путь к конфигу показывается как подставляемое значение, а не CONFIG."""
    assert "--config <FILE>" in build_parser().format_help()


def test_channel_uses_placeholder() -> None:
    """Канал показывается как подставляемое значение, а не КАНАЛ."""
    assert "--channel, --ch <CHANNEL>" in build_parser().format_help()
    assert "--channel, --ch <CHANNEL>" in general_help(color=False)


def test_general_help_examples_include_channel_with_retry() -> None:
    """В примерах общей справки есть канал вместе с повтором."""
    text = general_help(color=False)

    assert "./run.sh --channel habr --retry" in text
    assert "./run.sh --channel habr --re" in text


def test_retry_help_matches_spec() -> None:
    """Справка повтора — ровно заданный текст, без старых разделов."""
    assert retry_help(color=False) == RETRY_HELP_TEXT
    assert "Режимы работы:" not in retry_help(color=False)
    assert "Что делает повтор:" not in retry_help(color=False)


def test_excluded_help_matches_spec() -> None:
    """У `--excluded` своя узкая справка, а не справка всех повторов."""
    text = excluded_help(color=False)

    assert text == EXCLUDED_HELP_TEXT
    assert "Синоним: --retry excluded" in text
    assert "Для терминальных статусов" not in text
    assert "sent" not in text


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["-h"],
        ["--help", "--retry"],
        ["--install-browser", "-h"],
        ["--notify-test", "--help"],
        ["--config", "config.yaml", "-h"],
    ],
)
def test_general_help_for_any_help_flag(argv: list[str], capsys) -> None:
    """Любая команда с --help/-h без опции со своей справкой печатает общую."""
    assert main(argv) == 0
    assert capsys.readouterr().out == GENERAL_HELP_TEXT + "\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["--retry", "--help"],
        ["--retry", "-h"],
        ["--retry", "queued", "--help"],
        ["--re", "-h"],
        ["--retry=queued", "--help"],
    ],
)
def test_retry_help_after_retry_flag(argv: list[str], capsys) -> None:
    """`--retry --help` печатает справку по повторам и выходит с кодом 0."""
    assert main(argv) == 0
    assert capsys.readouterr().out == RETRY_HELP_TEXT + "\n"


@pytest.mark.parametrize("argv", [["--excluded", "--help"], ["--excluded", "-h"]])
def test_excluded_help_after_excluded_flag(argv: list[str], capsys) -> None:
    """`--excluded --help` печатает узкую справку про исключённые вакансии."""
    assert main(argv) == 0
    assert capsys.readouterr().out == EXCLUDED_HELP_TEXT + "\n"


@pytest.mark.parametrize("argv", [["--login", "--help"], ["--login", "-h"]])
def test_login_help_after_login_flag(argv: list[str], capsys) -> None:
    """`--login --help` объясняет вход и показывает канал."""
    assert main(argv) == 0
    out = capsys.readouterr().out

    assert "Вход на сайт выбранного канала с сохранением сессии" in out
    assert "--channel, --ch <CHANNEL>" in out
    assert "./run.sh --login --channel habr" in out


@pytest.mark.parametrize("argv", [["--channel", "--help"], ["--ch", "-h"]])
def test_channel_help_after_channel_flag(argv: list[str], capsys) -> None:
    """`--channel --help` объясняет значения канала и примеры запуска."""
    assert main(argv) == 0
    out = capsys.readouterr().out

    assert "Канал запуска: hh.ru или career.habr.com" in out
    assert "hh" in out and "habr" in out
    assert "./run.sh --channel habr --retry" in out


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--retry", "--help"], "retry"),
        (["--retry", "-h"], "retry"),
        (["--retry=error", "-h"], "retry"),
        (["--re", "--help"], "retry"),
        (["-r", "-h"], "retry"),
        (["--excluded", "--help"], "excluded"),
        (["--excluded", "-h"], "excluded"),
        (["--login", "--help"], "login"),
        (["--channel", "-h"], "channel"),
        (["--ch", "habr", "--help"], "channel"),
        (["--retry", "--login", "-h"], "login"),
        (["--login", "--channel", "habr", "-h"], "channel"),
        (["--help", "--retry"], "general"),
        (["--help", "--excluded"], "general"),
        (["--help", "--login"], "general"),
        (["--help"], "general"),
        (["--retry"], "general"),
        (["--excluded"], "general"),
        ([], "general"),
    ],
)
def test_help_topic_order(argv: list[str], expected: str) -> None:
    """Справка опции — только когда опция идёт перед флагом справки."""
    assert help_topic(argv) == expected


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--retry", "--help"], True),
        (["--retry=error", "-h"], True),
        (["--excluded", "--help"], False),
        (["--help", "--retry"], False),
        (["--retry"], False),
        ([], False),
    ],
)
def test_retry_help_requested_order(argv: list[str], expected: bool) -> None:
    """Справка повторов — только для --retry/--re перед флагом справки."""
    assert retry_help_requested(argv) is expected


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--excluded", "--help"], True),
        (["--excluded", "-h"], True),
        (["--retry", "--help"], False),
        (["--help", "--excluded"], False),
        ([], False),
    ],
)
def test_excluded_help_requested_order(argv: list[str], expected: bool) -> None:
    """Справка исключённых — только для --excluded перед флагом справки."""
    assert excluded_help_requested(argv) is expected


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--help"], True),
        (["-h"], True),
        (["--login", "--help"], True),
        (["--retry", "-h"], True),
        (["--retry"], False),
        ([], False),
    ],
)
def test_help_requested_in_any_position(argv: list[str], expected: bool) -> None:
    """`--help` и `-h` работают в любом месте аргументов."""
    assert help_requested(argv) is expected


def test_unknown_option_is_reported_in_russian(capsys) -> None:
    """Опечатка в опции — понятное сообщение и подсказка, а не usage argparse."""
    assert main(["--adsfa"]) == 2
    err = capsys.readouterr().err

    assert "Ошибка: неизвестная опция: --adsfa" in err
    assert "Использование: ./run.sh [OPTIONS]" in err
    assert "Справка: ./run.sh --help" in err
    assert "usage:" not in err


def test_unknown_retry_status_lists_allowed_values(capsys) -> None:
    """Неверный статус — перечень допустимых значений, без внутреннего all."""
    assert main(["--retry", "decade"]) == 2
    err = capsys.readouterr().err

    assert "Ошибка: неизвестный статус повтора: decade" in err
    assert "Допустимые статусы: error, queued, excluded, skipped" in err
    assert "Использование: ./run.sh --retry [STATUS]" in err
    assert " all" not in err


def test_unknown_channel_lists_allowed_values(capsys) -> None:
    """Неверный канал — перечень допустимых значений и подсказка по справке."""
    assert main(["--channel", "superjob"]) == 2
    err = capsys.readouterr().err

    assert "Ошибка: неизвестный канал: superjob" in err
    assert "Допустимые каналы: hh, habr" in err
    assert "Использование: ./run.sh --channel <CHANNEL>" in err
    assert "Справка: ./run.sh --help" in err


def test_config_option_without_value(capsys) -> None:
    """`--config` без значения — понятная ошибка про значение."""
    assert main(["--config"]) == 2
    err = capsys.readouterr().err

    assert "Ошибка: опция --config требует значение" in err
    assert "Использование: ./run.sh --config <FILE>" in err


def test_help_uses_color_when_forced(monkeypatch, capsys) -> None:
    """Цвет включается по FORCE_COLOR; справка и ошибки оформлены одинаково."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    assert main(["--help"]) == 0
    general = capsys.readouterr().out
    assert "\x1b[1;36m-h, --help" in general

    assert main(["--retry", "--help"]) == 0
    retry = capsys.readouterr().out
    assert "\x1b[1;34mОпции (принимается одно значение):\x1b[0m" in retry

    assert main(["--adsfa"]) == 2
    err = capsys.readouterr().err
    assert "\x1b[1;35mОшибка:" in err


def test_no_color_environment_disables_color(monkeypatch, capsys) -> None:
    """NO_COLOR важнее FORCE_COLOR: вывод остаётся монохромным."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")

    assert main(["--help"]) == 0
    assert "\x1b[" not in capsys.readouterr().out


def test_main_passes_channel_and_retry_to_run(tmp_path: Path, monkeypatch) -> None:
    """Флаг канала доходит до запуска вместе с --retry, браузер не поднимается."""
    data = {
        "search": {"queries": "Java", "area": "Москва"},
        "pipeline": {"limit_per_run": 1, "delay_between_actions": {"from": 0, "to": 0}},
        "llm": {"api_key_env": "TEST_API_KEY"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    captured: dict[str, object] = {}

    class FakeLogger:
        def __enter__(self) -> "FakeLogger":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

    def fake_run(config, logger, retry=None, channel_name=None):
        del config, logger
        captured["channel"] = channel_name
        captured["retry"] = retry

    monkeypatch.setattr("agent.cli.RunLogger", lambda *args, **kwargs: FakeLogger())
    monkeypatch.setattr("agent.cli.run", fake_run)

    assert main(["--config", str(path), "--ch", "habr", "--retry"]) == 0
    assert captured == {"channel": "habr", "retry": RETRY_ALL}


def test_main_stops_on_resume_not_found(tmp_path: Path, monkeypatch, capsys) -> None:
    """Нет резюме в форме: код 1 и понятное сообщение без трейсбека."""
    data = {
        "search": {"queries": "Java", "area": "Москва"},
        "pipeline": {"limit_per_run": 1, "delay_between_actions": {"from": 0, "to": 0}},
        "llm": {"api_key_env": "TEST_API_KEY"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("TEST_API_KEY", "test-key")

    class FakeLogger:
        def __enter__(self) -> "FakeLogger":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

    def fake_run(*args, **kwargs):
        del args, kwargs
        raise ResumeNotFoundError("Team Lead")

    monkeypatch.setattr("agent.cli.RunLogger", lambda *args, **kwargs: FakeLogger())
    monkeypatch.setattr("agent.cli.run", fake_run)

    assert main(["--config", str(path)]) == 1
    err = capsys.readouterr().err
    assert "Запуск прерван" in err
    assert "Team Lead" in err
    assert "Traceback" not in err


def test_notify_test_sends_to_each_enabled_kind(tmp_path: Path, monkeypatch, capsys) -> None:
    """Раздельные адреса отчёта и капчи — по тестовому письму на каждый."""
    notifications = {
        "email": {
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "sender": "from@example.com",
            "recipient": "common@example.com",
        },
        "report": {"recipient": "report@example.com"},
        "captcha": {"recipient": "captcha@example.com"},
    }
    config = make_config(tmp_path, notifications)
    logger = RunLogger(tmp_path / "logs", run_id="notify")
    sent_to: list[str] = []

    def fake_send(channel, recipient):
        sent_to.append(recipient)
        return True, "письмо отправлено"

    monkeypatch.setattr("agent.cli.send_test_notification", fake_send)

    assert _notify_test(config, logger) == 0
    logger.close()

    assert sent_to == ["report@example.com", "captcha@example.com"]
    assert "2" in capsys.readouterr().out
    assert [item["sent"] for item in read_events(logger)] == [True, True]


def test_notify_test_dedupes_same_address(tmp_path: Path, monkeypatch) -> None:
    """Одинаковый адрес у отчёта и капчи — одно тестовое письмо."""
    config = make_config(tmp_path)
    logger = RunLogger(tmp_path / "logs", run_id="notify")
    sent_to: list[str] = []

    def fake_send(channel, recipient):
        sent_to.append(recipient)
        return True, "письмо отправлено"

    monkeypatch.setattr("agent.cli.send_test_notification", fake_send)

    assert _notify_test(config, logger) == 0
    logger.close()

    assert sent_to == ["to@example.com"]


def test_notify_test_reports_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    config = make_config(tmp_path)
    logger = RunLogger(tmp_path / "logs", run_id="notify")
    monkeypatch.setattr(
        "agent.cli.send_test_notification",
        lambda channel, recipient: (False, "не удалось отправить email: timeout"),
    )

    assert _notify_test(config, logger) == 1
    logger.close()

    assert "timeout" in capsys.readouterr().err
    events = read_events(logger)
    assert events[-1]["event"] == "notify_test"
    assert events[-1]["sent"] is False


def test_notify_test_with_all_kinds_disabled(tmp_path: Path, capsys) -> None:
    notifications = {
        "email": {
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "sender": "from@example.com",
            "recipient": "to@example.com",
        },
        "report": {"enabled": False},
        "captcha": {"enabled": False},
    }
    config = make_config(tmp_path, notifications)
    logger = RunLogger(tmp_path / "logs", run_id="notify")

    assert _notify_test(config, logger) == 1
    logger.close()

    assert "уведомления отключены" in capsys.readouterr().err
    assert read_events(logger)[-1]["sent"] is False
