"""Тесты командной строки: флаг проверки канала уведомлений."""

from __future__ import annotations

import json
from pathlib import Path

from agent.cli import _notify_test, build_parser
from agent.config import load_config
from agent.logging_setup import RunLogger


def make_config(tmp_path: Path, notifications: dict | None = None):
    data = {
        "search_query": "Java",
        "area": "Москва",
        "limit_per_run": 1,
        "delay_between_actions": {"from": 0, "to": 0},
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

    assert parser.parse_args(["--retry"]).retry == "all"
    assert parser.parse_args(["--retry", "queued"]).retry == "queued"
    assert parser.parse_args(["--re"]).retry == "all"
    assert parser.parse_args(["--excluded"]).excluded is True
    assert parser.parse_args([]).retry is None
    assert parser.parse_args([]).excluded is False


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

