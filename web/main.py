"""Веб-интерфейс дневных отчётов: просмотр вакансий и правка статусов.

Модуль работает только с файлами `data/report` — тем же форматом, что пишет
агент (`agent/processed.py`): шапка с итогами и плоский список `vacancies`.
Браузер и hh.ru здесь не используются: это офлайн-просмотр и правка.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "data" / "report"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Статусы записи: те же, что в отчёте агента (agent/models.py STATUSES).
STATUSES = ("sent", "skipped", "queued", "excluded", "error", "rejected")

# Статус «отказ» ставит человек, когда работодатель ответил отказом: причина
# всегда одна и та же, попытки отклика не пересчитываем (agent/models.py).
STATUS_REJECTED = "rejected"
REJECTED_REASON = "По отклику пришёл отказ"

app = FastAPI(title="Отчёты по вакансиям")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class StatusUpdate(BaseModel):
    """Правка статуса вакансии в конкретном файле дня."""

    report_name: str
    url: str
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
    items.sort(key=lambda item: str(item.get("processed_at", "")), reverse=True)
    return items


def now() -> str:
    """Локальное время в ISO-виде — как поле processed_at в отчёте."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Страница отчёта: таблица вакансий, фильтры и правка статусов."""
    return templates.TemplateResponse(request=request, name="report.html")


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
    selected = report_files()
    if files:
        wanted = {item.strip() for item in files.split(",") if item.strip()}
        selected = [path for path in selected if path.name in wanted or path.stem in wanted]
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


@app.post("/api/v1/update-status")
async def update_status(update: StatusUpdate) -> dict[str, Any]:
    """Меняет статус вакансии, не переписывая остальную запись.

    Набор полей зависит от статуса:

    * `rejected` — отказ работодателя: пишем только `status`, `reason`
      («По отклику пришёл отказ») и `processed_at`; попытки не считаем;
    * остальные статусы — `status`, `attempts` (+1) и `processed_at`,
      причина остаётся прежней.

    Итоги в шапке пересчитываем по самим записям, чтобы цифры наверху
    сходились с таблицей.
    """
    if update.status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"Неизвестный статус: {update.status}")
    path = report_path(update.report_name)
    data = read_report(path)
    for vacancy in data["vacancies"]:
        if not isinstance(vacancy, dict) or str(vacancy.get("url", "")) != update.url:
            continue
        vacancy["status"] = update.status
        vacancy["processed_at"] = now()
        if update.status == STATUS_REJECTED:
            vacancy["reason"] = REJECTED_REASON
        else:
            vacancy["attempts"] = int(vacancy.get("attempts", 0)) + 1
        write_report(path, data)
        return {
            "ok": True,
            "report_name": path.name,
            "summary": data["summary"],
            "attempts": vacancy["attempts"],
            "reason": str(vacancy.get("reason", "")),
            "processed_at": vacancy["processed_at"],
        }
    raise HTTPException(status_code=404, detail="Вакансия не найдена в отчёте")
