"""Дневной отчёт о вакансиях: файл на дату (data/report) и память о пройденном."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .hh_search import profile_key, vacancy_keys
from .models import (
    ARCHIVE_REASON,
    INVITE_REASON,
    STATUSES,
    STATUS_INVITE,
    STATUS_OFFER,
    STATUS_REJECTED,
    STATUS_SENT,
)
from .report_io import ReportLockError, report_lock, write_report_file

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

    Одна запись — это вакансия и профиль: ключ `(url, profile)`. Разными профилями
    можно отвечать на одну вакансию, поэтому у каждой такой пары своя запись.
    При загрузке старые файлы не переписываются, а при обновлении запись
    переносится в отчёт текущей даты.

    У записи две даты: `created_at` — дата обработки вакансии, она не меняется
    при переносах между файлами и сменах статуса, и `updated_at` — когда запись
    последний раз менялась. Для старых записей без `created_at` используется
    дата обработки, сохранённая в самой записи.
    """

    def __init__(
        self,
        directory: Path,
        today: date | None = None,
        run_id: str = "",
        legacy_profile: str = "",
    ) -> None:
        self.directory = directory
        self.today = today or date.today()
        self.run_id = (run_id or "").strip()
        # Старые записи Habr шли без профиля: имя подставляет вызывающий код,
        # иначе вакансия считалась бы новой и обрабатывалась повторно.
        self.legacy_profile = (legacy_profile or "").strip()
        self._seen: dict[str, str] = {}
        self._records: dict[str, tuple[Path, dict[str, Any]]] = {}
        self._archived: set[str] = set()
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

        Файлы читаются от свежих к старым, а из нескольких копий одной записи
        берётся самая свежая по `updated_at`: в файле дня строки идут в порядке
        обработки, поэтому первая строка могла быть старой копией. Старые файлы
        остаются историей и не переписываются. Ключи всех архивных копий
        запоминаются: архив терминальный, и повтор их не берёт.
        """
        self._seen.clear()
        self._records.clear()
        self._archived.clear()
        self.dropped_copies = 0
        if not self.directory.is_dir():
            return 0
        for file in sorted(self.directory.glob("*.json"), reverse=True):
            day = _file_date(file)
            if day is None:
                continue
            day_text = day.strftime(DATE_FORMAT)
            for record in _read(file):
                keys = self._keys(record)
                if not keys:
                    continue
                if record.get("archived"):
                    self._archived.update(keys)
                known = [self._records[key] for key in keys if key in self._records]
                if not known:
                    for key in keys:
                        self._seen.setdefault(key, day_text)
                        self._records[key] = (file, record)
                    continue
                self.dropped_copies += 1
                newest = max(known, key=lambda entry: _record_time(entry[1]))
                if _record_time(record) <= _record_time(newest[1]):
                    continue
                for key in keys:
                    self._seen[key] = day_text
                    self._records[key] = (file, record)
        return len(self._seen)

    def find(
        self,
        title: str,
        company: str,
        url: str,
        profile: str = "",
    ) -> str | None:
        """Дата, когда вакансия с этим резюме уже обрабатывалась, или None."""
        for key in vacancy_keys(title, company, url, profile):
            day = self._seen.get(key)
            if day:
                return day
        return None

    def find_record(
        self,
        title: str,
        company: str,
        url: str,
        profile: str = "",
    ) -> dict[str, Any] | None:
        """Запись вакансии с этим резюме из отчётов, если она уже есть."""
        for key in vacancy_keys(title, company, url, profile):
            known = self._records.get(key)
            if known is not None:
                return known[1]
        return None

    def today_summary(self) -> dict[str, int]:
        """Итоги файла дня по статусам: в нём лежат записи и прошлых запусков."""
        counts: dict[str, int] = {}
        seen: set[int] = set()
        for path, record in self._records.values():
            if path != self.path or id(record) in seen:
                continue
            seen.add(id(record))
            status = str(record.get("status", ""))
            if status:
                counts[status] = counts.get(status, 0) + 1
        return counts

    def pending(self, statuses: Sequence[str]) -> list[dict[str, Any]]:
        """Записи со статусами, которые нужно повторить (свежие сверху).

        Архив терминальный: если хотя бы одна копия вакансии помечена
        `archived`, повтор её не берёт. Иначе старая копия без признака снова
        ставила закрытую вакансию в очередь.
        """
        wanted = set(statuses)
        found: list[dict[str, Any]] = []
        seen: set[int] = set()
        for _path, record in self._records.values():
            if id(record) in seen:
                continue
            seen.add(id(record))
            if record.get("archived"):
                continue
            if any(key in self._archived for key in self._keys(record)):
                continue
            if str(record.get("status", "")) in wanted:
                found.append(record)
        found.sort(key=_record_time, reverse=True)
        return found

    def invite_other_resume(
        self,
        *,
        url: str,
        title: str,
        company: str,
        profile: str,
    ) -> list[str]:
        """Ставит `invite` записям той же вакансии, сделанным другими резюме.

        Приглашение на собеседование приходит по отклику, поэтому меняются
        только записи со статусом `sent` (переход `sent → invite`). Запись
        текущего резюме не трогаем: её статус пишет вызывающий код.
        Факт приглашения фиксируется в `invited_at` и не сбрасывается, когда
        запись позже переходит в `offer` или `rejected`.
        Возвращает названия резюме, которым проставлен статус `invite`.
        """
        del title, company  # вакансию опознаём по ссылке: она есть в записи
        current = profile_key(profile)
        updated: list[str] = []
        seen: set[int] = set()
        for _path, record in list(self._records.values()):
            if id(record) in seen:
                continue
            seen.add(id(record))
            if str(record.get("status", "")) != STATUS_SENT:
                continue
            if not _same_url(record, url):
                continue
            other = str(record.get("profile", ""))
            if not other or profile_key(other) == current:
                continue
            # Дата приглашения ставится один раз: по ней считается «дошло до
            # приглашения» даже после смены статуса.
            if not str(record.get("invited_at", "")).strip():
                record["invited_at"] = _now()
            self._update(
                record,
                status=STATUS_INVITE,
                reason=INVITE_REASON,
                profile=other,
                channel=str(record.get("channel", "")),
                count_attempt=False,
            )
            updated.append(other)
        return updated

    def mark_archived(
        self,
        *,
        url: str,
        title: str,
        company: str,
        profile: str = "",
    ) -> bool:
        """Помечает известную запись архивной, не меняя статус.

        Экран «Вакансия в архиве» у уже обработанной вакансии не повод менять
        статус: иначе разойдутся метрики и сводка. Причина становится
        архивной: у записи с `archived: true` она одна и та же. Возвращает
        False, если записи нет — тогда вызывающий код пишет вакансию как новую.
        """
        keys = vacancy_keys(title, company, url, profile)
        known = next((self._records[key] for key in keys if key in self._records), None)
        if known is None:
            return False
        item = known[1]

        return self._update(
            item,
            status=str(item.get("status", "")),
            reason=ARCHIVE_REASON,
            profile=str(item.get("profile", "")) or None,
            channel=str(item.get("channel", "")),
            count_attempt=False,
            archived=True,
        )

    def record(
        self,
        *,
        url: str,
        title: str,
        company: str,
        status: str,
        reason: str = "",
        message: str = "",
        profile: str | None = None,
        channel: str = "",
        archived: bool = False,
        questionary: Sequence[Mapping[str, str]] | None = None,
        cover_letter: str | None = None,
        probability: float | None = None,
        decision_reason: str | None = None,
    ) -> bool:
        """Записывает вакансию в отчёт дня; известную запись обновляет (повтор).

        `profile` — профиль записи: резюме hh или имя из заголовка `# PROFILE:`.
        `message` — полный текст причины для окна деталей.
        `questionary` — ответы на анкету или тест: блок `questionary` в записи.
        `cover_letter` — сопроводительное письмо для ручного отклика: пишет его
        только отложенная или упавшая вакансия. None оставляет прежний текст:
        правка статуса письмо не удаляет.
        `probability` и `decision_reason` — вердикт Decision для аудита: без них
        по файлу дня не видно, почему вакансия признана подходящей.
        Возвращает False, если вакансию нечем опознать при следующем запуске:
        нет ни ссылки, ни названия с компанией.
        """
        keys = vacancy_keys(title, company, url, profile)
        if not keys:
            return False
        known = next((self._records[key] for key in keys if key in self._records), None)
        if known is not None:
            return self._update(
                known[1],
                status=status,
                reason=reason,
                message=message,
                profile=profile,
                channel=channel,
                archived=archived,
                questionary=questionary,
                cover_letter=cover_letter,
                probability=probability,
                decision_reason=decision_reason,
            )

        now = _now()
        item: dict[str, Any] = {
            "url": url,
            "title": title,
            "company": company,
            "status": status,
            "attempts": 1,
            "created_at": now,
            "updated_at": now,
        }
        # У Habr и внешней формы нет резюме: поле в отчёт не пишем.
        if channel != "external":
            item["profile"] = profile or ""
        if self.run_id:
            item["run_id"] = self.run_id
        if reason:
            item["reason"] = reason
        if message:
            item["message"] = message
        if channel:
            item["channel"] = channel
        if archived:
            # Вакансия в архиве или доступ к ней закрыт: признак виден в UI.
            item["archived"] = True
        if cover_letter:
            item["cover_letter"] = cover_letter
        if probability is not None:
            item["probability"] = round(float(probability), 4)
        if decision_reason:
            item["decision_reason"] = decision_reason
        item["questionary"] = [dict(entry) for entry in (questionary or [])]

        try:
            with report_lock(self.directory):
                items = _read(self.path)
                existing = next(
                    (row for row in items if self._same_record(row, item)), None
                )
                if existing is not None:
                    return self._update_locked(
                        existing,
                        status=status,
                        reason=reason,
                        message=message,
                        profile=profile,
                        channel=channel,
                        archived=archived,
                        questionary=questionary,
                        cover_letter=cover_letter,
                        probability=probability,
                        decision_reason=decision_reason,
                    )
                items.append(item)
                _write(self.path, items)
        except ReportLockError as exc:
            raise ProcessedError(str(exc)) from exc

        self._remember(item)
        return True

    def _update(
        self,
        item: dict[str, Any],
        *,
        status: str,
        reason: str,
        message: str = "",
        profile: str | None = None,
        channel: str = "",
        count_attempt: bool = True,
        archived: bool = False,
        questionary: Sequence[Mapping[str, str]] | None = None,
        cover_letter: str | None = None,
        probability: float | None = None,
        decision_reason: str | None = None,
    ) -> bool:
        """Обновляет запись, удерживая общую с веб-интерфейсом блокировку каталога."""
        try:
            with report_lock(self.directory):
                return self._update_locked(
                    item,
                    status=status,
                    reason=reason,
                    message=message,
                    profile=profile,
                    channel=channel,
                    count_attempt=count_attempt,
                    archived=archived,
                    questionary=questionary,
                    cover_letter=cover_letter,
                    probability=probability,
                    decision_reason=decision_reason,
                )
        except ReportLockError as exc:
            raise ProcessedError(str(exc)) from exc

    def _update_locked(
        self,
        item: dict[str, Any],
        *,
        status: str,
        reason: str,
        message: str = "",
        profile: str | None = None,
        channel: str = "",
        count_attempt: bool = True,
        archived: bool = False,
        questionary: Sequence[Mapping[str, str]] | None = None,
        cover_letter: str | None = None,
        probability: float | None = None,
        decision_reason: str | None = None,
    ) -> bool:
        """Обновляет запись о вакансии: повтор увеличивает счётчик попыток.

        По ключу `(url, profile)` должна остаться одна запись. Поэтому вакансия
        удаляется из всех дневных файлов, а обновлённая запись попадает в отчёт
        сегодняшней даты. `count_attempt=False` — правка статуса без обработки
        вакансии: счётчик попыток не растёт. Вызывается только под блокировкой.
        `message` — полный текст причины; пустая строка удаляет его.
        `questionary=None` оставляет прежние ответы на анкету: правка статуса их
        не затирает. Блок есть в записи всегда: без ответов это пустой список.
        `cover_letter` заменяет письмо, когда текст передан: None оставляет прежний.
        """
        now = _now()
        if channel == "external":
            item.pop("profile", None)
        elif profile is not None:
            item["profile"] = profile
        # Дата создания фиксируется один раз. Для старых записей без неё берём
        # дату обработки из самой записи, а не дату файла.
        item["created_at"] = str(
            item.get("created_at") or item.get("updated_at") or now
        )
        item["status"] = status
        if count_attempt:
            item["attempts"] = int(item.get("attempts", 1)) + 1
            if self.run_id:
                item["run_id"] = self.run_id
        item["updated_at"] = now
        if channel:
            item["channel"] = channel
        if reason:
            item["reason"] = reason
        else:
            item.pop("reason", None)
        if message:
            item["message"] = message
        else:
            item.pop("message", None)
        if archived:
            # Вакансия в архиве или доступ к ней закрыт: признак виден в UI.
            item["archived"] = True
        else:
            # Вакансия снова в работе (например, повтор): архив снимается.
            item.pop("archived", None)
        if cover_letter:
            item["cover_letter"] = cover_letter
        if probability is not None:
            item["probability"] = round(float(probability), 4)
        if decision_reason:
            item["decision_reason"] = decision_reason
        if questionary is not None:
            item["questionary"] = [dict(entry) for entry in questionary]
        elif "questionary" not in item:
            item["questionary"] = []
        for file in self.directory.glob("*.json"):
            if file == self.path:
                continue
            rows = _read(file)
            kept = [row for row in rows if not self._same_record(row, item)]
            if len(kept) != len(rows):
                _write(file, kept)

        today: list[dict[str, Any]] = []
        replaced = False
        for row in _read(self.path):
            if self._same_record(row, item):
                if not replaced:
                    today.append(item)
                    replaced = True
                continue
            today.append(row)
        if not replaced:
            today.append(item)
        _write(self.path, today)

        self._remember(item)
        return True

    def _keys(self, record: Mapping[str, Any]) -> tuple[str, ...]:
        """Ключи записи с подстановкой имени профиля для старых записей Habr."""
        return vacancy_keys(
            str(record.get("title", "")),
            str(record.get("company", "")),
            str(record.get("url", "")),
            _effective_profile(record, self.legacy_profile),
        )

    def _same_record(self, row: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
        """Одна и та же запись: та же вакансия и тот же профиль.

        Пустой профиль старой записи Habr приравнивается к имени из `# PROFILE:`,
        иначе при обновлении рядом с ней появлялась копия с профилем, и повтор
        брал в работу уже обработанную вакансию.
        """
        return _same_vacancy(row, item, self.legacy_profile)

    def _remember(self, item: dict[str, Any]) -> None:
        """Индексирует запись по ключам с профилем и без него.

        Ключи с профилем — рабочая дедупликация: вакансия и резюме. Ключи без
        профиля остаются для обратной совместимости вызовов без `profile`.
        """
        self._remember_at(self.path, item)

    def _remember_at(self, path: Path, item: dict[str, Any]) -> None:
        """Индексирует запись и запоминает файл, в котором она лежит.

        Архивный признак запоминается по всем ключам вакансии: повтор не должен
        вернуть в работу вакансию, у которой есть архивная копия.
        """
        day = _file_date(path)
        day_text = day.strftime(DATE_FORMAT) if day else self.today.strftime(DATE_FORMAT)
        keys = self._keys(item)
        if item.get("archived"):
            self._archived.update(keys)
        for key in keys:
            self._seen.setdefault(key, day_text)
            self._records[key] = (path, item)

    def update_status(
        self,
        *,
        url: str,
        title: str,
        company: str,
        status: str,
        reason: str,
        profile: str = "",
    ) -> bool:
        """Меняет статус известной записи и переносит её в отчёт текущей даты.

        Нужен для автоматической сверки со страницей откликов Habr. Запись
        трогается только когда статус действительно меняется: повторный
        «Отказ» или «Прочитано» не переписывает файл. `created_at` и
        `attempts` сохраняются, `updated_at` становится временем сверки, при
        `invite` заполняется `invited_at`. Терминальные `rejected` и `offer`
        не переоткрываются, архив не меняется.
        """
        if status not in (STATUS_SENT, STATUS_INVITE, STATUS_REJECTED, STATUS_OFFER):
            return False
        keys = vacancy_keys(title, company, url, profile)
        known = next((self._records[key] for key in keys if key in self._records), None)
        if known is None:
            return False
        item = known[1]
        current = str(item.get("status", ""))
        if item.get("archived") or any(
            key in self._archived for key in self._keys(item)
        ):
            return False
        # Терминальные отказ и оффер не переоткрываем. Остальные статусы
        # (sent, invite, skipped, queued, error, excluded) страница откликов
        # может уточнить: она знает, что отклик действительно существует.
        if current in (STATUS_REJECTED, STATUS_OFFER):
            return False
        if current == status:
            return False
        if current == STATUS_INVITE and status == STATUS_SENT:
            return False
        if status == STATUS_INVITE:
            item["invited_at"] = str(item.get("invited_at") or _now())
        return self._update(
            item,
            status=status,
            reason=reason,
            profile=profile or None,
            channel=str(item.get("channel", "")),
            count_attempt=False,
        )

    def update_status_in_place(
        self,
        *,
        url: str,
        title: str,
        company: str,
        status: str,
        reason: str,
        profile: str = "",
    ) -> bool:
        """Совместимое имя `update_status` для старого вызова сверки."""
        return self.update_status(
            url=url,
            title=title,
            company=company,
            status=status,
            reason=reason,
            profile=profile,
        )


def _file_date(path: Path) -> date | None:
    """Дата из имени файла вида 2026-10-21.json; для прочих файлов — None."""
    try:
        return datetime.strptime(path.stem, DATE_FORMAT).date()
    except ValueError:
        return None


def _same_url(row: dict[str, Any], url: str) -> bool:
    """Запись относится к этой вакансии: ссылки совпадают."""
    return bool(url) and str(row.get("url", "")) == url


def _effective_profile(record: Mapping[str, Any], legacy_profile: str) -> str:
    """Профиль записи: у старой записи Habr его подставляет имя из `# PROFILE:`.

    Без подстановки такая запись и её новая копия с профилем считались разными
    вакансиями, и повтор снова брал в работу уже обработанную вакансию.
    """
    profile = str(record.get("profile", ""))
    if profile or not legacy_profile or record.get("channel") != "habr":
        return profile
    return legacy_profile


def _same_vacancy(
    row: Mapping[str, Any], item: Mapping[str, Any], legacy_profile: str = ""
) -> bool:
    """Одна и та же запись: та же вакансия и тот же профиль.

    Пустой профиль старой записи Habr приравнивается к имени из `# PROFILE:`:
    это её же запись, просто сделанная до появления профиля в отчёте.
    """
    if profile_key(_effective_profile(row, legacy_profile)) != profile_key(
        _effective_profile(item, legacy_profile)
    ):
        return False
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
    """Дополняет запись датами, не меняя уже известные значения."""
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
    try:
        write_report_file(path, payload)
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
