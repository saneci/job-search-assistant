"""Память об уже обработанных вакансиях: по файлу на каждую дату."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .hh_search import vacancy_keys

DATE_FORMAT = "%Y-%m-%d"


class ProcessedError(RuntimeError):
    """Не удалось прочитать или сохранить память обработанных вакансий."""


class ProcessedStore:
    """Помнит, какие вакансии уже прошли, чтобы не обрабатывать их повторно.

    Формат совпадает с queue.json (JSON-массив записей), но файл — на каждую
    дату: ``data/processed/2026-10-21.json``. При загрузке читаются все файлы
    каталога, поэтому вакансия не будет обработана повторно ни завтра, ни через
    месяц.
    """

    def __init__(self, directory: Path, today: date | None = None) -> None:
        self.directory = directory
        self.today = today or date.today()
        self._seen: dict[str, str] = {}

    @property
    def path(self) -> Path:
        """Файл сегодняшней даты."""
        return self.directory / f"{self.today.strftime(DATE_FORMAT)}.json"

    def load(self) -> int:
        """Читает историю из всех файлов каталога; возвращает число ключей."""
        self._seen.clear()
        if not self.directory.is_dir():
            return 0
        for file in sorted(self.directory.glob("*.json")):
            for record in _read(file):
                keys = vacancy_keys(
                    str(record.get("title", "")),
                    str(record.get("company", "")),
                    str(record.get("url", "")),
                )
                for key in keys:
                    self._seen.setdefault(key, file.stem)
        return len(self._seen)

    def find(self, title: str, company: str, url: str) -> str | None:
        """Дата, когда вакансия уже обрабатывалась, или None."""
        for key in vacancy_keys(title, company, url):
            day = self._seen.get(key)
            if day:
                return day
        return None

    def prune(self, days: int) -> list[str]:
        """Удаляет файлы старше days дней. Возвращает имена удалённых файлов."""
        if days <= 0 or not self.directory.is_dir():
            return []
        limit = self.today - timedelta(days=days)
        removed: list[str] = []
        for file in sorted(self.directory.glob("*.json")):
            try:
                file_date = datetime.strptime(file.stem, DATE_FORMAT).date()
            except ValueError:
                continue
            if file_date >= limit:
                continue
            try:
                file.unlink()
            except OSError:
                continue
            removed.append(file.name)
        return removed

    def add(self, *, url: str, title: str, company: str, status: str) -> bool:
        """Дописывает вакансию в файл сегодняшней даты. False — уже была."""
        if not vacancy_keys(title, company, url):
            return False
        if self.find(title, company, url):
            return False

        items = _read(self.path)
        items.append(
            {
                "url": url,
                "title": title,
                "company": company,
                "status": status,
                "processed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )
        self._save(items)

        day = self.today.strftime(DATE_FORMAT)
        for key in vacancy_keys(title, company, url):
            self._seen.setdefault(key, day)
        return True

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self.path)
        except OSError as exc:
            raise ProcessedError(f"Не удалось сохранить {self.path}: {exc}") from exc


def _read(path: Path) -> list[dict[str, Any]]:
    """Читает файл истории; битые файлы не роняют запуск."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]
