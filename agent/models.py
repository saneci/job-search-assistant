"""Модели данных, общие для всех модулей."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VacancyCard:
    """Вакансия, собранная со страницы поиска."""

    title: str
    company: str
    url: str
    salary: str = ""


@dataclass(frozen=True)
class VacancyDetails:
    """Полные данные вакансии со страницы вакансии."""

    url: str
    title: str
    company: str
    salary: str
    description: str
    already_responded: bool = False
    blocker: str | None = None

    @property
    def salary_text(self) -> str:
        return self.salary.strip() or "не указана"


@dataclass(frozen=True)
class SentVacancy:
    """Отправленный отклик."""

    title: str
    company: str
    url: str
    probability: float


@dataclass(frozen=True)
class SkippedVacancy:
    """Вакансия, пропущенная без отклика."""

    title: str
    company: str
    url: str
    reason: str
    probability: float | None = None


@dataclass(frozen=True)
class RunError:
    """Ошибка, возникшая при обработке вакансии."""

    url: str
    title: str
    message: str


# Статусы вакансии в дневном отчёте (data/report/<дата>.json).
# `excluded` — исключённая вакансия (стоп-слово в названии или компания из
# exclude_companies), `error` — сбой обработки: такие вакансии можно повторить
# по прямой ссылке (`--retry`).
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_QUEUED = "queued"
STATUS_EXCLUDED = "excluded"
STATUS_ERROR = "error"

STATUSES = (STATUS_SENT, STATUS_SKIPPED, STATUS_QUEUED, STATUS_EXCLUDED, STATUS_ERROR)

# Что имеет смысл повторять: отклик не ушёл из-за сбоя, ограничения сайта
# или списка исключённых компаний.
RETRYABLE_STATUSES = (STATUS_ERROR, STATUS_QUEUED, STATUS_EXCLUDED, STATUS_SKIPPED)
