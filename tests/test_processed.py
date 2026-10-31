
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import agent.processed as processed_module
from agent.processed import ProcessedStore


def test_add_and_find_in_same_run(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.record(
        url="https://hh.ru/vacancy/1",
        title="Java-разработчик",
        company="Ozon",
        status="sent",
    )

    assert store.find("Java-разработчик", "Ozon", "https://hh.ru/vacancy/1") == "2026-10-21"


def test_file_has_header_with_summary(tmp_path: Path) -> None:
    """Файл дня читается как отчёт: дата, время обновления и итоги по статусам."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java-разработчик",
        company="Ozon",
        status="sent",
    )

    payload = json.loads((tmp_path / "2026-10-21.json").read_text(encoding="utf-8"))

    assert payload["date"] == "2026-10-21"
    assert payload["updated_at"]
    assert payload["summary"] == {
        "sent": 1,
        "skipped": 0,
        "queued": 0,
        "excluded": 0,
        "error": 0,
        "rejected": 0,
        "offer": 0,
        "invite": 0,
    }
    assert payload["vacancies"][0]["title"] == "Java-разработчик"
    assert payload["vacancies"][0]["status"] == "sent"
    assert payload["vacancies"][0]["attempts"] == 1
    assert payload["vacancies"][0]["profile"] == ""
    assert payload["vacancies"][0]["created_at"]
    assert payload["vacancies"][0]["updated_at"]


def test_invite_other_resume_marks_only_other_profile(tmp_path: Path) -> None:
    """Приглашение: invite получает запись другого резюме со статусом sent."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    url = "https://hh.ru/vacancy/1"
    store.record(
        url=url,
        title="Java-разработчик",
        company="Ozon",
        status="sent",
        profile="Java Developer",
        channel="hh",
    )
    store.record(
        url=url,
        title="Java-разработчик",
        company="Ozon",
        status="skipped",
        profile="Team Lead",
        channel="hh",
    )

    updated = store.invite_other_resume(
        url=url, title="Java-разработчик", company="Ozon", profile="Team Lead"
    )

    assert updated == ["Java Developer"]
    rows = {
        row["profile"]: row
        for row in json.loads(store.path.read_text(encoding="utf-8"))["vacancies"]
    }
    assert rows["Java Developer"]["status"] == "invite"
    assert rows["Java Developer"]["reason"] == "По отклику пришло приглашение на интервью"
    # Правка статуса не обработка вакансии: счётчик попыток не растёт.
    assert rows["Java Developer"]["attempts"] == 1
    assert rows["Team Lead"]["status"] == "skipped"

    # Факт приглашения фиксируется один раз и не сбрасывается сменой статуса.
    invited_at = rows["Java Developer"]["invited_at"]
    assert invited_at
    store.record(
        url=url,
        title="Java-разработчик",
        company="Ozon",
        status="offer",
        reason="По отклику пришёл оффер",
        profile="Java Developer",
        channel="hh",
    )
    rows = {
        row["profile"]: row
        for row in json.loads(store.path.read_text(encoding="utf-8"))["vacancies"]
    }
    assert rows["Java Developer"]["status"] == "offer"
    assert rows["Java Developer"]["invited_at"] == invited_at


def test_roll_to_switches_report_file(tmp_path: Path) -> None:
    """Через полночь записи идут в файл новой даты, а не в файл начала запуска."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.roll_to(date(2026, 10, 21)) is False
    assert store.roll_to(date(2026, 10, 22)) is True
    assert store.path == tmp_path / "2026-10-22.json"

    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java-разработчик",
        company="Ozon",
        status="skipped",
    )

    assert (tmp_path / "2026-10-22.json").exists()
    assert not (tmp_path / "2026-10-21.json").exists()


def write_day(directory: Path, day: str, rows: list[dict]) -> None:
    """Кладёт файл дня вручную — чтобы проверить разбор готового отчёта."""
    payload = {"date": day, "updated_at": "", "summary": {}, "vacancies": rows}
    (directory / f"{day}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def read_day(directory: Path, day: str) -> dict:
    return json.loads((directory / f"{day}.json").read_text(encoding="utf-8"))


def test_update_moves_vacancy_to_new_day_file(tmp_path: Path) -> None:
    """Повтор на следующую дату переносит запись в новый файл, а не копирует её."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="error")

    store.roll_to(date(2026, 10, 22))
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="sent")

    old = read_day(tmp_path, "2026-10-21")
    new = read_day(tmp_path, "2026-10-22")

    assert old["vacancies"] == []
    assert [row["status"] for row in new["vacancies"]] == ["sent"]
    assert new["vacancies"][0]["attempts"] == 2


def test_repeat_within_day_keeps_single_row(tmp_path: Path) -> None:
    """Две обработки одной вакансии за день — одна строка с двумя попытками."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="queued")
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="sent")

    payload = read_day(tmp_path, "2026-10-21")

    assert len(payload["vacancies"]) == 1
    assert payload["vacancies"][0]["attempts"] == 2
    assert payload["summary"]["sent"] == 1
    assert payload["summary"]["queued"] == 0


def test_copied_rows_collapse_when_updated(tmp_path: Path) -> None:
    """Если в файлах оказались копии, обновление оставляет одну запись."""
    row = {
        "url": "https://hh.ru/vacancy/1",
        "title": "Java",
        "company": "Ozon",
        "status": "queued",
        "attempts": 1,
        "created_at": "2026-10-21T10:00:00+03:00",
        "updated_at": "2026-10-21T10:00:00+03:00",
    }
    write_day(tmp_path, "2026-10-21", [dict(row)])
    write_day(tmp_path, "2026-10-22", [dict(row), dict(row)])

    store = ProcessedStore(tmp_path, today=date(2026, 10, 22))
    store.load()
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="sent")

    assert store.dropped_copies == 2  # копия внутри файла дня и копия из старого файла
    assert read_day(tmp_path, "2026-10-21")["vacancies"] == []
    assert len(read_day(tmp_path, "2026-10-22")["vacancies"]) == 1
    assert read_day(tmp_path, "2026-10-22")["vacancies"][0]["attempts"] == 2


def test_pending_takes_newest_copy(tmp_path: Path) -> None:
    """Для повтора берётся свежая копия записи, а не самая старая."""
    row = {
        "url": "https://hh.ru/vacancy/1",
        "title": "Java",
        "company": "Ozon",
        "status": "error",
        "attempts": 1,
        "created_at": "2026-10-20T10:00:00+03:00",
        "updated_at": "2026-10-20T10:00:00+03:00",
    }
    write_day(tmp_path, "2026-10-20", [dict(row)])
    write_day(
        tmp_path,
        "2026-10-21",
        [{**row, "attempts": 2, "updated_at": "2026-10-21T10:00:00+03:00"}],
    )

    store = ProcessedStore(tmp_path, today=date(2026, 10, 22))
    store.load()

    assert [item["attempts"] for item in store.pending(("error",))] == [2]


def test_new_run_skips_previous_vacancy(tmp_path: Path) -> None:
    ProcessedStore(tmp_path, today=date(2026, 10, 21)).record(
        url="https://hh.ru/vacancy/1",
        title="Java-разработчик",
        company="Ozon",
        status="skipped",
    )

    next_day = ProcessedStore(tmp_path, today=date(2026, 10, 22))
    next_day.load()

    assert next_day.find("Java-разработчик", "Ozon", "https://hh.ru/vacancy/2") == "2026-10-21"


def test_history_reads_all_date_files(tmp_path: Path) -> None:
    ProcessedStore(tmp_path, today=date(2026, 10, 20)).record(
        url="https://hh.ru/vacancy/1",
        title="Первая",
        company="Ozon",
        status="sent",
    )
    ProcessedStore(tmp_path, today=date(2026, 10, 21)).record(
        url="https://hh.ru/vacancy/2",
        title="Вторая",
        company="Ozon",
        status="queued",
    )

    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.load()

    assert store.find("Первая", "Ozon", "") == "2026-10-20"
    assert store.find("Вторая", "Ozon", "") == "2026-10-21"


def test_same_vacancy_is_updated_not_duplicated(tmp_path: Path) -> None:
    """Повтор (--retry) обновляет запись и увеличивает счётчик попыток."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="error")
    assert store.record(url="https://hh.ru/vacancy/2", title="Java", company="Ozon", status="sent")

    payload = json.loads((tmp_path / "2026-10-21.json").read_text(encoding="utf-8"))

    assert len(payload["vacancies"]) == 1
    assert payload["vacancies"][0]["status"] == "sent"
    assert payload["vacancies"][0]["attempts"] == 2


def test_empty_vacancy_is_not_stored(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert not store.record(url="", title="", company="", status="sent")
    assert store.find("", "", "") is None


def test_broken_file_does_not_break_load(tmp_path: Path) -> None:
    (tmp_path / "2026-10-20.json").write_text("{не json", encoding="utf-8")

    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.load() == 0


def test_report_files_are_never_deleted(tmp_path: Path) -> None:
    """Отчёты — история для аналитики: старые файлы дня остаются на месте."""
    old = tmp_path / "2025-01-01.json"
    old.write_text("[]", encoding="utf-8")

    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.load()
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="sent")

    assert old.exists()
    assert sorted(path.name for path in tmp_path.glob("*.json")) == [
        "2025-01-01.json",
        "2026-10-21.json",
    ]


def test_skipped_record_has_reason(tmp_path: Path) -> None:
    """По файлу дня видно, почему вакансию пропустили."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="skipped",
        reason="вероятность 0.57 не выше порога 0.80",
    )

    records = json.loads((tmp_path / "2026-10-21.json").read_text(encoding="utf-8"))["vacancies"]

    assert records[0]["status"] == "skipped"
    assert records[0]["reason"] == "вероятность 0.57 не выше порога 0.80"


def test_queued_record_has_reason(tmp_path: Path) -> None:
    """Отложенная вакансия тоже объясняет, почему её не взяли в работу."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="queued",
        reason="Для отклика на вакансию необходимо пройти тестирование",
    )

    records = json.loads((tmp_path / "2026-10-21.json").read_text(encoding="utf-8"))["vacancies"]

    assert records[0]["status"] == "queued"
    assert records[0]["reason"] == "Для отклика на вакансию необходимо пройти тестирование"


def test_sent_record_has_no_reason(tmp_path: Path) -> None:
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="sent")

    records = json.loads((tmp_path / "2026-10-21.json").read_text(encoding="utf-8"))["vacancies"]

    assert "reason" not in records[0]


def test_foreign_file_in_report_dir_is_ignored(tmp_path: Path) -> None:
    """Посторонний файл в каталоге отчётов историей вакансий не считается."""
    foreign = [
        {"url": "https://hh.ru/vacancy/9", "title": "Java", "company": "Ozon", "reason": "test"}
    ]
    (tmp_path / "queue.json").write_text(json.dumps(foreign, ensure_ascii=False), encoding="utf-8")
    ProcessedStore(tmp_path, today=date(2026, 10, 21)).record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Sber",
        status="sent",
    )

    store = ProcessedStore(tmp_path, today=date(2026, 10, 22))
    store.load()

    assert store.find("Java", "Ozon", "https://hh.ru/vacancy/9") is None
    assert store.find("Java", "Sber", "https://hh.ru/vacancy/1") == "2026-10-21"


def test_report_of_old_format_is_ignored(tmp_path: Path) -> None:
    """Старый формат (просто массив) не поддерживаем: такие файлы не индексируются.

    Вакансии из них будут пройдены заново — это осознанное решение.
    """
    (tmp_path / "2026-10-20.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://hh.ru/vacancy/1",
                    "title": "Java",
                    "company": "Ozon",
                    "status": "queued",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    assert store.load() == 0
    assert store.find("Java", "Ozon", "https://hh.ru/vacancy/1") is None


def test_pending_selects_statuses_for_retry(tmp_path: Path) -> None:
    """Для --retry берём только те статусы, которые имеет смысл повторить."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="error")
    store.record(url="https://hh.ru/vacancy/2", title="Go", company="VK", status="sent")
    store.record(url="https://hh.ru/vacancy/3", title="Kotlin", company="Yandex", status="excluded")

    urls = {item["url"] for item in store.pending(["error", "excluded"])}

    assert urls == {"https://hh.ru/vacancy/1", "https://hh.ru/vacancy/3"}
    assert store.pending(["queued"]) == []


def test_retry_moves_record_to_today_report(tmp_path: Path) -> None:
    """Повтор в другой день обновляет статус и переносит запись в файл сегодня."""
    ProcessedStore(tmp_path, today=date(2026, 10, 21)).record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="error",
        reason="TimeoutError: timeout",
    )

    store = ProcessedStore(tmp_path, today=date(2026, 10, 22))
    store.load()
    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="sent")

    yesterday = json.loads((tmp_path / "2026-10-21.json").read_text(encoding="utf-8"))
    today = json.loads((tmp_path / "2026-10-22.json").read_text(encoding="utf-8"))

    assert yesterday["vacancies"] == []
    assert today["vacancies"][0]["status"] == "sent"
    assert today["vacancies"][0]["attempts"] == 2
    assert "reason" not in today["vacancies"][0]
    assert today["summary"]["sent"] == 1


def test_error_record_keeps_message(tmp_path: Path) -> None:
    """По причине ошибки видно, что повторять и о чём писать в отчёте."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="error",
        reason="TimeoutError: timeout",
    )

    payload = json.loads((tmp_path / "2026-10-21.json").read_text(encoding="utf-8"))

    assert payload["vacancies"][0]["reason"] == "TimeoutError: timeout"
    assert payload["summary"]["error"] == 1


def test_new_record_has_created_and_updated_at(tmp_path: Path) -> None:
    """Новая запись сразу получает дату создания и дату последнего изменения."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="sent",
    )

    row = read_day(tmp_path, "2026-10-21")["vacancies"][0]

    assert row["created_at"] == row["updated_at"]


def test_profile_is_written_and_preserved_on_update(tmp_path: Path) -> None:
    """Резюме попадает в отчёт и не теряется при обновлении без profile."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="error",
        profile="Java Developer",
    )

    row = read_day(tmp_path, "2026-10-21")["vacancies"][0]
    assert row["profile"] == "Java Developer"

    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="sent",
    )

    row = read_day(tmp_path, "2026-10-21")["vacancies"][0]
    assert row["profile"] == "Java Developer"


@pytest.mark.parametrize("channel", ["habr", "external"])
def test_habr_records_do_not_store_empty_profile(tmp_path: Path, channel: str) -> None:
    """У Habr и внешней формы нет резюме: пустое поле не пишется."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url="https://career.habr.com/vacancies/1",
        title="Java",
        company="Ozon",
        status="skipped",
        profile="",
        channel=channel,
    )

    row = read_day(tmp_path, "2026-10-21")["vacancies"][0]
    assert "profile" not in row


def test_created_at_survives_move_and_update(tmp_path: Path) -> None:
    """При переносе в другой файл дата создания не меняется, дата изменения — да."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="error",
    )
    created_at = read_day(tmp_path, "2026-10-21")["vacancies"][0]["created_at"]

    store.roll_to(date(2026, 10, 22))
    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="sent",
    )

    row = read_day(tmp_path, "2026-10-22")["vacancies"][0]

    assert row["created_at"] == created_at
    assert row["updated_at"]


def test_legacy_record_gets_created_at_from_processing_date(tmp_path: Path) -> None:
    """У старой записи без created_at датой создания становится дата обработки."""
    processing_date = "2026-10-20T00:00:00+03:00"
    write_day(
        tmp_path,
        "2026-10-20",
        [
            {
                "url": "https://hh.ru/vacancy/1",
                "title": "Java",
                "company": "Ozon",
                "status": "error",
                "attempts": 1,
                "updated_at": processing_date,
            }
        ],
    )

    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.load()
    store.record(
        url="https://hh.ru/vacancy/1",
        title="Java",
        company="Ozon",
        status="sent",
    )

    row = read_day(tmp_path, "2026-10-21")["vacancies"][0]

    assert row["created_at"] == processing_date
    assert row["updated_at"]


def test_report_updated_at_changes_on_rewrite(
    tmp_path: Path, monkeypatch
) -> None:
    """Дата обновления шапки меняется при каждой перезаписи файла отчёта."""
    times = iter(
        [
            "2026-10-21T10:00:00+03:00",
            "2026-10-21T10:00:01+03:00",
            "2026-10-21T10:00:02+03:00",
            "2026-10-21T10:00:03+03:00",
        ]
    )
    monkeypatch.setattr(processed_module, "_now", lambda: next(times))
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))

    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="error")
    first = read_day(tmp_path, "2026-10-21")["updated_at"]

    store.record(url="https://hh.ru/vacancy/1", title="Java", company="Ozon", status="sent")
    second = read_day(tmp_path, "2026-10-21")["updated_at"]

    assert first == "2026-10-21T10:00:01+03:00"
    assert second == "2026-10-21T10:00:03+03:00"


def test_same_vacancy_with_different_profiles_is_two_records(tmp_path: Path) -> None:
    """Разными резюме можно отвечать на одну вакансию: записи независимы."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    url = "https://hh.ru/vacancy/1"

    store.record(
        url=url,
        title="Java",
        company="Ozon",
        status="sent",
        profile="Java Developer",
    )
    store.record(
        url=url,
        title="Java",
        company="Ozon",
        status="queued",
        profile="Team Lead",
    )

    rows = read_day(tmp_path, "2026-10-21")["vacancies"]
    assert [(row["profile"], row["status"]) for row in rows] == [
        ("Java Developer", "sent"),
        ("Team Lead", "queued"),
    ]
    assert store.find("Java", "Ozon", url, "Java Developer") == "2026-10-21"
    assert store.find("Java", "Ozon", url, "Team Lead") == "2026-10-21"
    assert store.find("Java", "Ozon", url, "Data Scientist") is None


def test_retry_updates_only_its_profile(tmp_path: Path) -> None:
    """Повтор с одним резюме не трогает запись другого резюме."""
    url = "https://hh.ru/vacancy/1"
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.record(
        url=url,
        title="Java",
        company="Ozon",
        status="error",
        profile="Java Developer",
    )
    store.record(
        url=url,
        title="Java",
        company="Ozon",
        status="error",
        profile="Team Lead",
    )

    store.roll_to(date(2026, 10, 22))
    store.record(
        url=url,
        title="Java",
        company="Ozon",
        status="sent",
        profile="Java Developer",
    )

    yesterday = read_day(tmp_path, "2026-10-21")["vacancies"]
    today = read_day(tmp_path, "2026-10-22")["vacancies"]

    assert [(row["profile"], row["status"]) for row in yesterday] == [("Team Lead", "error")]
    assert [(row["profile"], row["status"], row["attempts"]) for row in today] == [
        ("Java Developer", "sent", 2)
    ]


def test_load_keeps_records_of_different_profiles(tmp_path: Path) -> None:
    """Загрузка истории не считает записи разных резюме копиями."""
    url = "https://hh.ru/vacancy/1"
    row = {
        "url": url,
        "title": "Java",
        "company": "Ozon",
        "status": "error",
        "attempts": 1,
        "created_at": "2026-10-20T10:00:00+03:00",
        "updated_at": "2026-10-20T10:00:00+03:00",
    }
    write_day(
        tmp_path,
        "2026-10-20",
        [dict(row, profile="Java Developer"), dict(row, profile="Team Lead")],
    )

    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    store.load()

    assert store.dropped_copies == 0
    assert store.find("Java", "Ozon", url, "Java Developer") == "2026-10-20"
    assert store.find("Java", "Ozon", url, "Team Lead") == "2026-10-20"


def test_profile_key_ignores_case_and_extra_spaces(tmp_path: Path) -> None:
    """Профиль сравнивается без учёта регистра и лишних пробелов."""
    store = ProcessedStore(tmp_path, today=date(2026, 10, 21))
    url = "https://hh.ru/vacancy/1"

    store.record(
        url=url,
        title="Java",
        company="Ozon",
        status="error",
        profile="Java Developer",
    )
    store.record(
        url=url,
        title="Java",
        company="Ozon",
        status="sent",
        profile="  java   developer ",
    )

    rows = read_day(tmp_path, "2026-10-21")["vacancies"]
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2
