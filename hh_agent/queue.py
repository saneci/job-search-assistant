"""Очередь вакансий для ручных действий (queue.json)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

QUEUE_REASONS = ("test", "questionnaire", "external_form", "empty_cover_letter", "unknown")

REASON_TITLES: dict[str, str] = {
    "test": "тест",
    "questionnaire": "анкета",
    "external_form": "внешняя форма",
    "empty_cover_letter": "пустое сопроводительное письмо",
    "unknown": "не удалось откликнуться автоматически",
}


class QueueError(RuntimeError):
    """Не удалось прочитать или сохранить queue.json."""


class QueueStore:
    """Хранилище отложенных вакансий в JSON-файле."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise QueueError(f"Не удалось прочитать {self.path}: {exc}") from exc
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        raise QueueError(f"Файл {self.path} должен содержать JSON-массив")

    def add(
        self,
        *,
        url: str,
        title: str,
        company: str,
        reason: str,
        added_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Добавляет вакансию в очередь. Возвращает запись или None, если она уже была."""
        if reason not in QUEUE_REASONS:
            raise QueueError(f"Неизвестная причина откладывания: {reason}")
        items = self.load()
        if any(item.get("url") == url for item in items):
            return None
        record = {
            "url": url,
            "title": title,
            "company": company,
            "reason": reason,
            "added_at": added_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        items.append(record)
        self._save(items)
        return record

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self.path)
        except OSError as exc:
            raise QueueError(f"Не удалось сохранить {self.path}: {exc}") from exc
