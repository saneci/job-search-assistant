"""Веб-интерфейс дневных отчётов: главная, воронка и правка статусов.

Модуль работает только с файлами `data/report` — тем же форматом, что пишет
агент (`agent/processed.py`): шапка с итогами и плоский список `vacancies`.
Браузер и hh.ru здесь не используются: это офлайн-просмотр и правка.
"""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .metrics import build_metrics, dedupe_vacancies, profile_key, profile_value

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "data" / "report"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Статусы записи: те же, что в отчёте агента (agent/models.py STATUSES).
STATUSES = (
    "sent",
    "skipped",
    "queued",
    "excluded",
    "error",
    "invite",
    "rejected",
    "offer",
)

# Исход отклика человек ставит вручную: причина всегда одна и та же, попытки
# отклика не пересчитываем (agent/models.py).
STATUS_SENT = "sent"
STATUS_INVITE = "invite"
STATUS_REJECTED = "rejected"
STATUS_OFFER = "offer"
REJECTED_REASON = "По отклику пришёл отказ"
OFFER_REASON = "По отклику пришёл оффер"

# Исход отклика — терминальная операция: после него статус не меняется.
TERMINAL_STATUSES = (STATUS_REJECTED, STATUS_OFFER)

# Периоды воронки: считаются по календарю относительно последней даты в данных.
PERIODS = ("year", "quarter", "month", "week", "day")
PERIOD_LABELS = {
    "year": "Год",
    "quarter": "Квартал",
    "month": "Месяц",
    "week": "Неделя",
    "day": "День",
}
DEFAULT_PERIOD = "year"
MONTHS_NOMINATIVE = (
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
QUARTER_NAMES = ("I", "II", "III", "IV")

app = FastAPI(title="Отчёты по вакансиям")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class StatusUpdate(BaseModel):
    """Правка статуса вакансии в конкретном файле дня."""

    report_name: str
    url: str
    # None — старый клиент: профиль не передан, ищем запись по URL.
    profile: str | None = None
    status: str


def report_files() -> list[Path]:
    """Файлы отчётов в каталоге, свежие сверху."""
    if not REPORTS_DIR.is_dir():
        return []
    return sorted(REPORTS_DIR.glob("*.json"), reverse=True)


def report_path(report_name: str) -> Path:
    """Путь к файлу дня; имя с каталогами не принимаем."""
    path = REPORTS_DIR / Path(report_name).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Отчёт не найден")
    return path

def current_report_path() -> Path:
    """Файл отчёта текущей даты."""
    return REPORTS_DIR / f"{date.today().isoformat()}.json"


def find_vacancy(url: str, profile: str | None = None) -> dict[str, Any] | None:
    """Первая запись вакансии с этим резюме во всех отчётах."""
    wanted = None if profile is None else profile_value(profile)
    for path in report_files():
        for vacancy in read_report(path)["vacancies"]:
            if (
                isinstance(vacancy, dict)
                and str(vacancy.get("url", "")) == url
                and (wanted is None or profile_key(vacancy) == wanted)
            ):
                return vacancy
    return None



def read_report(path: Path) -> dict[str, Any]:
    """Читает файл дня; чужой формат — ошибка, а не пустая страница."""
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Не читается {path.name}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("vacancies"), list):
        raise HTTPException(status_code=422, detail=f"Некорректный формат отчёта: {path.name}")
    return data


def write_report(path: Path, data: dict[str, Any]) -> None:
    """Сохраняет файл дня, обновляя шапку: время и итоги по статусам."""
    data["updated_at"] = now()
    data["summary"] = summarize(data["vacancies"])
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def summarize(vacancies: list[dict[str, Any]]) -> dict[str, int]:
    """Итоги по статусам — шапка отчёта."""
    counts = {status: 0 for status in STATUSES}
    for vacancy in vacancies:
        status = str(vacancy.get("status", ""))
        counts[status] = counts.get(status, 0) + 1
    return counts


def all_vacancies(paths: list[Path]) -> list[dict[str, Any]]:
    """Вакансии из выбранных файлов: в записи видно, из какого она файла."""
    items: list[dict[str, Any]] = []
    for path in paths:
        for vacancy in read_report(path)["vacancies"]:
            if isinstance(vacancy, dict):
                items.append({**vacancy, "report_name": path.name})
    items.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return items


def selected_reports(files: str = "") -> list[Path]:
    """Файлы дня по списку имён; без списка — все."""
    selected = report_files()
    if not files:
        return selected
    wanted = {item.strip() for item in files.split(",") if item.strip()}
    return [path for path in selected if path.name in wanted or path.stem in wanted]


def period_bounds(period: str, anchor: date) -> tuple[date, date]:
    """Границы календарного периода, в который попадает anchor."""
    if period not in PERIODS:
        raise ValueError(f"Неизвестный период: {period}")
    if period == "year":
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    if period == "quarter":
        first_month = (anchor.month - 1) // 3 * 3 + 1
        last_month = first_month + 2
        return (
            date(anchor.year, first_month, 1),
            date(anchor.year, last_month, monthrange(anchor.year, last_month)[1]),
        )
    if period == "month":
        return (
            date(anchor.year, anchor.month, 1),
            date(anchor.year, anchor.month, monthrange(anchor.year, anchor.month)[1]),
        )
    if period == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    return anchor, anchor


def record_date(record: dict[str, Any]) -> date | None:
    """Дата создания записи для периода воронки.

    Источник один: `created_at`. Формат отчёта требует это поле, легаси-форматы
    не поддерживаются: данные старых отчётов приведены к текущему виду.
    """
    value = str(record.get("created_at", "")).strip()
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def latest_record_date(records: list[dict[str, Any]]) -> date | None:
    """Самая свежая дата создания — опора для календарных периодов."""
    dates = [value for record in records if (value := record_date(record))]
    return max(dates) if dates else None


def filter_by_period(
    records: list[dict[str, Any]], start: date, end: date
) -> list[dict[str, Any]]:
    """Оставляет записи, созданные в указанном календарном диапазоне."""
    result: list[dict[str, Any]] = []
    for record in records:
        value = record_date(record)
        if value is not None and start <= value <= end:
            result.append(record)
    return result


def period_label(period: str, start: date, end: date) -> str:
    """Подпись выбранного периода для шапки фильтра."""
    if period == "year":
        return str(start.year)
    if period == "quarter":
        return f"{QUARTER_NAMES[(start.month - 1) // 3]} квартал {start.year}"
    if period == "month":
        return f"{MONTHS_NOMINATIVE[start.month - 1]} {start.year}"
    if period == "day":
        return f"{start.day} {MONTHS_GENITIVE[start.month - 1]} {start.year}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {MONTHS_GENITIVE[start.month - 1]} {start.year}"
    if start.year == end.year:
        return (
            f"{start.day} {MONTHS_GENITIVE[start.month - 1]} — "
            f"{end.day} {MONTHS_GENITIVE[end.month - 1]} {start.year}"
        )
    return (
        f"{start.day} {MONTHS_GENITIVE[start.month - 1]} {start.year} — "
        f"{end.day} {MONTHS_GENITIVE[end.month - 1]} {end.year}"
    )


def current_metrics(
    paths: list[Path] | None = None,
    period: str | None = None,
    anchor: date | None = None,
) -> dict[str, Any]:
    """Метрики по выбранным файлам (по умолчанию — по всем отчётам).

    Без `period` считаются все отчёты. С периодом записи фильтруются по
    `created_at`: вакансия остаётся в когорте даты создания, даже если статус
    изменился позже. Точкой отсчёта служит последняя дата создания в данных,
    чтобы воронка не оказалась пустой из-за давно не обновлявшегося отчёта.
    """
    selected = report_files() if paths is None else paths
    items = dedupe_vacancies(all_vacancies(selected))
    period_info: dict[str, Any] | None = None
    if period is not None:
        if period not in PERIODS:
            raise HTTPException(status_code=422, detail=f"Неизвестный период: {period}")
        anchor_date = anchor or latest_record_date(items) or date.today()
        start, end = period_bounds(period, anchor_date)
        items = filter_by_period(items, start, end)
        period_info = {
            "key": period,
            "label": period_label(period, start, end),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "options": [
                {
                    "key": key,
                    "label": label,
                    "active": key == period,
                }
                for key, label in PERIOD_LABELS.items()
            ],
        }
    updated_at = ""
    for path in selected:
        try:
            data = read_report(path)
        except HTTPException:
            continue
        updated_at = max(updated_at, str(data.get("updated_at", "")))
    result = build_metrics(
        items,
        reports_count=len(selected),
        updated_at=updated_at,
    )
    if period_info is not None:
        result["period"] = period_info
    return result


def now() -> str:
    """Локальное время в ISO-виде для updated_at в отчёте."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Главная страница: переход к воронке и отчёту, живые итоги."""
    return templates.TemplateResponse(
        request=request, name="index.html", context={"metrics": current_metrics()}
    )


@app.get("/report", response_class=HTMLResponse)
@app.get("/report.html", response_class=HTMLResponse, include_in_schema=False)
async def report(request: Request) -> HTMLResponse:
    """Страница отчёта: таблица вакансий, фильтры и правка статусов."""
    return templates.TemplateResponse(request=request, name="report.html")


@app.get("/metrics", response_class=HTMLResponse)
@app.get("/metrics.html", response_class=HTMLResponse, include_in_schema=False)
async def metrics(request: Request, period: str = DEFAULT_PERIOD) -> HTMLResponse:
    """Воронка поиска работы: метрики по дневным отчётам за выбранный период."""
    if period not in PERIODS:
        raise HTTPException(status_code=422, detail=f"Неизвестный период: {period}")
    return templates.TemplateResponse(
        request=request,
        name="metrics.html",
        context={"metrics": current_metrics(period=period)},
    )


@app.get("/index.html", include_in_schema=False)
async def index_alias() -> RedirectResponse:
    """Старая ссылка на главную."""
    return RedirectResponse(url="/", status_code=307)


@app.get("/api/v1/reports")
async def list_reports() -> dict[str, Any]:
    """Список файлов дня: имя, дата, время обновления и итоги."""
    items: list[dict[str, Any]] = []
    for path in report_files():
        try:
            data = read_report(path)
        except HTTPException:
            continue
        items.append(
            {
                "name": path.name,
                "date": str(data.get("date", path.stem)),
                "updated_at": str(data.get("updated_at", "")),
                "summary": summarize(data["vacancies"]),
                "total": len(data["vacancies"]),
            }
        )
    return {"reports": items}


@app.get("/api/v1/vacancies")
async def list_vacancies(files: str = "") -> dict[str, Any]:
    """Вакансии из выбранных файлов дня; без выбора — по всем файлам.

    Дата и файл дня — одно и то же (`2026-10-23.json`), поэтому фильтр идёт
    по имени файла; для старых клиентов принимаем и дату без расширения.
    """
    selected = selected_reports(files)
    vacancies = all_vacancies(selected)
    return {
        "summary": summarize(vacancies),
        "vacancies": vacancies,
        "files": [path.name for path in selected],
    }


@app.get("/api/v1/reports/{report_name}")
async def get_report(report_name: str) -> dict[str, Any]:
    """Файл дня целиком."""
    return read_report(report_path(report_name))


@app.get("/api/v1/metrics")
async def get_metrics(files: str = "", period: str = "") -> dict[str, Any]:
    """Метрики по выбранным файлам дня; без периода — по всем отчётам."""
    if period and period not in PERIODS:
        raise HTTPException(status_code=422, detail=f"Неизвестный период: {period}")
    return current_metrics(selected_reports(files), period=period or None)


@app.post("/api/v1/update-status")
async def update_status(update: StatusUpdate) -> dict[str, Any]:
    """Меняет статус вакансии и переносит её в отчёт текущей даты.

    В отчётах одна запись на пару «URL и резюме»: вакансия с этим профилем
    удаляется из всех файлов, а обновлённая запись попадает в файл сегодняшней
    даты. `created_at` при этом не меняется, `updated_at` становится временем
    правки. `rejected` и `offer` терминальны: `invite` ставится только из `sent`,
    `offer` — из `sent` или `invite`. Момент приглашения пишется в `invited_at`
    и не сбрасывается при переходе в оффер или отказ.
    """
    if update.status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"Неизвестный статус: {update.status}")
    report_path(update.report_name)
    vacancy = find_vacancy(update.url, update.profile)
    if vacancy is None:
        raise HTTPException(status_code=404, detail="Вакансия не найдена в отчётах")
    vacancy = dict(vacancy)

    current_status = str(vacancy.get("status", ""))
    if current_status in TERMINAL_STATUSES:
        label = "Отказ" if current_status == STATUS_REJECTED else "Оффер"
        raise HTTPException(
            status_code=409,
            detail=f"Статус «{label}» терминальный: изменить его нельзя",
        )
    if update.status == STATUS_INVITE and current_status != STATUS_SENT:
        raise HTTPException(
            status_code=409,
            detail=(
                "Приглашение можно поставить только после статуса «Отправлено» "
                f"(сейчас: {current_status or 'нет статуса'})"
            ),
        )
    if update.status == STATUS_OFFER and current_status not in (STATUS_SENT, STATUS_INVITE):
        raise HTTPException(
            status_code=409,
            detail=(
                "Оффер можно поставить только после статуса «Отправлено» или «Приглашение» "
                f"(сейчас: {current_status or 'нет статуса'})"
            ),
        )

    changed_at = now()
    # Дата создания не меняется при смене статуса и переносе между отчётами.
    vacancy["created_at"] = str(
        vacancy.get("created_at") or vacancy.get("updated_at") or changed_at
    )
    # У Habr и внешней формы резюме нет: не возвращаем пустое поле в отчёт.
    if str(vacancy.get("channel", "")) in ("habr", "external"):
        vacancy.pop("profile", None)
    else:
        vacancy["profile"] = str(vacancy.get("profile") or "")
    vacancy["status"] = update.status
    vacancy["updated_at"] = changed_at
    if update.status == STATUS_INVITE:
        # Момент приглашения фиксируется один раз: по нему метрики считают
        # «дошло до приглашения» и после перехода в оффер или отказ.
        vacancy["invited_at"] = str(vacancy.get("invited_at") or changed_at)
    if update.status in TERMINAL_STATUSES:
        vacancy["reason"] = (
            REJECTED_REASON if update.status == STATUS_REJECTED else OFFER_REASON
        )
    else:
        vacancy["attempts"] = int(vacancy.get("attempts", 0)) + 1

    for path in report_files():
        data = read_report(path)
        wanted = None if update.profile is None else profile_value(update.profile)
        rows = [
            row
            for row in data["vacancies"]
            if not isinstance(row, dict)
            or str(row.get("url", "")) != update.url
            or (wanted is not None and profile_key(row) != wanted)
        ]
        if len(rows) != len(data["vacancies"]):
            data["vacancies"] = rows
            write_report(path, data)

    today = current_report_path()
    if today.exists():
        data = read_report(today)
    else:
        data = {"date": today.stem, "updated_at": now(), "summary": summarize([]), "vacancies": []}
    data["vacancies"].append(vacancy)
    write_report(today, data)

    summary = summarize(dedupe_vacancies(all_vacancies(report_files())))
    return {
        "ok": True,
        "report_name": today.name,
        "summary": summary,
        "attempts": vacancy.get("attempts", 0),
        "reason": str(vacancy.get("reason", "")),
        "profile": str(vacancy.get("profile", "")),
        "created_at": vacancy["created_at"],
        "updated_at": vacancy["updated_at"],
    }
