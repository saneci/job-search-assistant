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
