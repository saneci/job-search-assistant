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
# по прямой ссылке (`--retry`), `rejected` — работодатель отказал по уже
# отправленному отклику (на странице вакансии «Вам отказали»).
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_QUEUED = "queued"
STATUS_EXCLUDED = "excluded"
STATUS_ERROR = "error"
STATUS_REJECTED = "rejected"

STATUSES = (
    STATUS_SENT,
    STATUS_SKIPPED,
    STATUS_QUEUED,
    STATUS_EXCLUDED,
    STATUS_ERROR,
    STATUS_REJECTED,
)

# Причина отказа: её пишет и агент при обработке, и веб-интерфейс при правке.
REJECTED_REASON = "По отклику пришёл отказ"

# hh.ru показывает, что отклик по вакансии уже отправлен (руками или в прошлом
# запуске): в файл дня такая вакансия идёт со статусом `sent` и этой причиной,
# но лимит откликов запуска не расходует — отклик отправили не мы.
ALREADY_RESPONDED_REASON = "отклик уже отправлен"

# Что имеет смысл повторять: отклик не ушёл из-за сбоя, ограничения сайта
# или списка исключённых компаний.
RETRYABLE_STATUSES = (STATUS_ERROR, STATUS_QUEUED, STATUS_EXCLUDED, STATUS_SKIPPED)
