"""Дневной отчёт о вакансиях: файл на дату (data/report) и память о пройденном."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from .hh_search import vacancy_keys
from .models import STATUSES

DATE_FORMAT = "%Y-%m-%d"


class ProcessedError(RuntimeError):
    """Не удалось прочитать или сохранить дневной отчёт о вакансиях."""


class ProcessedStore:
    """Помнит, какие вакансии уже прошли, и ведёт дневной отчёт о них.

    Файлы лежат в ``data/report`` по одному на дату: ``2026-10-21.json``.
    Формат — шапка (дата, время обновления, итоги по статусам) и плоский
    список ``vacancies``: одна запись — один блок, поэтому файл читается как
    отчёт, а поиск и повторы идут по полю ``status``. Формат один: файлы
    другого вида (например, старые массивы) не индексируются — вакансии из них
    будут пройдены заново. При загрузке читаются все файлы дат, поэтому
    вакансия не будет обработана повторно ни завтра, ни через месяц. Прочие
    файлы каталога (не вида «ГГГГ-ММ-ДД.json») игнорируются.

    Одна вакансия — одна запись по URL. При загрузке старые файлы не
    переписываются, а при обновлении запись переносится в отчёт текущей даты.

    У записи две даты: `created_at` — дата обработки вакансии, она не меняется
    при переносах между файлами и сменах статуса, и `updated_at` — когда запись
    последний раз менялась. Для старых записей без `created_at` используется
    дата обработки, сохранённая в самой записи.
    """

    def __init__(self, directory: Path, today: date | None = None) -> None:
        self.directory = directory
        self.today = today or date.today()
        self._seen: dict[str, str] = {}
        self._records: dict[str, tuple[Path, dict[str, Any]]] = {}
        # Сколько копий пропущено в памяти при загрузке истории (файлы не меняются).
        self.dropped_copies = 0

    @property
    def path(self) -> Path:
        """Файл сегодняшней даты."""
        return self.directory / f"{self.today.strftime(DATE_FORMAT)}.json"

    def roll_to(self, today: date) -> bool:
        """Переключает отчёт на файл новой даты; True, если дата сменилась.

        Запуск может идти через полночь: записи должны попасть в файл той
        даты, когда они сделаны, а не в файл начала запуска.
        """
        if today == self.today:
            return False
        self.today = today
        return True

    def load(self) -> int:
        """Читает историю из всех файлов дат; возвращает число ключей.

        Файлы читаются от свежих к старым, поэтому для поиска и повторов
        используется самая новая запись вакансии. Старые файлы остаются
        историей и не переписываются.
        """
        self._seen.clear()
        self._records.clear()
        self.dropped_copies = 0
        if not self.directory.is_dir():
            return 0
        for file in sorted(self.directory.glob("*.json"), reverse=True):
            day = _file_date(file)
            if day is None:
                continue
            for record in _read(file):
                keys = vacancy_keys(
                    str(record.get("title", "")),
                    str(record.get("company", "")),
                    str(record.get("url", "")),
                )
                if keys and any(key in self._records for key in keys):
                    self.dropped_copies += 1
                    continue
                for key in keys:
                    self._seen.setdefault(key, day.strftime(DATE_FORMAT))
                    self._records[key] = (file, record)
        return len(self._seen)

    def find(self, title: str, company: str, url: str) -> str | None:
        """Дата, когда вакансия уже обрабатывалась, или None."""
        for key in vacancy_keys(title, company, url):
            day = self._seen.get(key)
            if day:
                return day
        return None

    def pending(self, statuses: Sequence[str]) -> list[dict[str, Any]]:
        """Записи со статусами, которые нужно повторить (свежие сверху)."""
        wanted = set(statuses)
        found: list[dict[str, Any]] = []
        seen: set[int] = set()
        for _path, record in self._records.values():
            if id(record) in seen:
                continue
            seen.add(id(record))
            if str(record.get("status", "")) in wanted:
                found.append(record)
        found.sort(key=_record_time, reverse=True)
        return found


    def record(
        self,
        *,
        url: str,
        title: str,
        company: str,
        status: str,
        reason: str = "",
        profile: str | None = None,
    ) -> bool:
        """Записывает вакансию в отчёт дня; известную запись обновляет (повтор).

        `profile` — название резюме, с которым обрабатывалась вакансия.
        Возвращает False, если вакансию нечем опознать при следующем запуске:
        нет ни ссылки, ни названия с компанией.
        """
        keys = vacancy_keys(title, company, url)
        if not keys:
            return False
        known = next((self._records[key] for key in keys if key in self._records), None)
        if known is not None:
            return self._update(
                known[1], status=status, reason=reason, profile=profile
            )

        now = _now()
        item: dict[str, Any] = {
            "url": url,
            "title": title,
            "company": company,
            "status": status,
            "attempts": 1,
            "profile": profile or "",
            "created_at": now,
            "updated_at": now,
        }
        if reason:
            item["reason"] = reason

        items = _read(self.path)
        items.append(item)
        _write(self.path, items)

        day = self.today.strftime(DATE_FORMAT)
        for key in keys:
            self._seen.setdefault(key, day)
            self._records.setdefault(key, (self.path, item))
        return True

    def _update(
        self,
        item: dict[str, Any],
        *,
        status: str,
        reason: str,
        profile: str | None = None,
    ) -> bool:
        """Обновляет запись о вакансии: повтор увеличивает счётчик попыток.

        По URL должна остаться одна запись. Поэтому вакансия удаляется из всех
        дневных файлов, а обновлённая запись попадает в отчёт сегодняшней даты.
        """
        now = _now()
        if profile is not None:
            item["profile"] = profile
        # Дата создания фиксируется один раз. Для старых записей без неё берём
        # дату обработки из самой записи, а не дату файла.
        item["created_at"] = str(
            item.get("created_at") or item.get("updated_at") or now
        )
        item["status"] = status
        item["attempts"] = int(item.get("attempts", 1)) + 1
        item["updated_at"] = now
        if reason:
            item["reason"] = reason
        else:
            item.pop("reason", None)

        for file in self.directory.glob("*.json"):
            if file == self.path:
                continue
            rows = _read(file)
            kept = [row for row in rows if not _same_vacancy(row, item)]
            if len(kept) != len(rows):
                _write(file, kept)

        today: list[dict[str, Any]] = []
        replaced = False
        for row in _read(self.path):
            if _same_vacancy(row, item):
                if not replaced:
                    today.append(item)
                    replaced = True
                continue
            today.append(row)
        if not replaced:
            today.append(item)
        _write(self.path, today)

        for key in vacancy_keys(
            str(item.get("title", "")),
            str(item.get("company", "")),
            str(item.get("url", "")),
        ):
            self._records[key] = (self.path, item)
        return True


def _file_date(path: Path) -> date | None:
    """Дата из имени файла вида 2026-10-21.json; для прочих файлов — None."""
    try:
        return datetime.strptime(path.stem, DATE_FORMAT).date()
    except ValueError:
        return None


def _same_vacancy(row: dict[str, Any], item: dict[str, Any]) -> bool:
    """Одна и та же вакансия: по ссылке, иначе по названию и компании."""
    url = str(item.get("url", ""))
    if url and str(row.get("url", "")) == url:
        return True
    return str(row.get("title", "")) == str(item.get("title", "")) and str(
        row.get("company", "")
    ) == str(item.get("company", ""))


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    """Итоги файла дня по статусам — шапка отчёта."""
    counts = {status: 0 for status in STATUSES}
    for item in items:
        status = str(item.get("status", ""))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _normalize_times(item: dict[str, Any], now: str) -> dict[str, Any]:
    """Дополняет старую запись датами, не меняя уже известные значения."""
    normalized = dict(item)
    created_at = str(
        normalized.get("created_at") or normalized.get("updated_at") or now
    )
    updated_at = str(normalized.get("updated_at") or created_at)
    normalized["created_at"] = created_at
    normalized["updated_at"] = updated_at
    return normalized


def _record_time(item: dict[str, Any]) -> str:
    """Время последнего изменения записи для сортировки и повторов."""
    return str(item.get("updated_at") or item.get("created_at") or "")


def _write(path: Path, items: list[dict[str, Any]]) -> None:
    """Пишет файл дня: шапка с итогами и плоский список вакансий."""
    day = _file_date(path)
    now = _now()
    normalized = [_normalize_times(item, now) for item in items]
    payload: dict[str, Any] = {
        "date": day.strftime(DATE_FORMAT) if day else path.stem,
        "updated_at": now,
        "summary": _summary(normalized),
        "vacancies": normalized,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        raise ProcessedError(f"Не удалось сохранить {path}: {exc}") from exc


def _now() -> str:
    """Локальное время в ISO-виде для created_at и updated_at."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read(path: Path) -> list[dict[str, Any]]:
    """Читает файл дня; битые файлы и чужой формат не роняют запуск."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    items = raw.get("vacancies", [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]
