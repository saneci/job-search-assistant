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
    # Отклик отправляется на сайте компании: автоматической отправки нет,
    # вакансия ждёт ручной обработки в очереди.
    external_response: bool = False
    # На карточке видно «Вы откликнулись»: отклик уже есть, карточку не открываем.
    already_responded: bool = False


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
    # Вакансия в архиве или доступ к ней закрыт: отклик отправить нельзя.
    # Признак попадает в дневной отчёт полем `archived`.
    archived: bool = False

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
    # Последнее предложение письма LLM: по нему в отчёте видно, что отправлено.
    reason: str = ""


@dataclass(frozen=True)
class SkippedVacancy:
    """Вакансия, пропущенная без отклика."""

    title: str
    company: str
    url: str
    reason: str
    probability: float | None = None
    # Вакансия в архиве или доступ закрыт: в отчёте это поле `archived`.
    archived: bool = False



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
# отправленному отклику (на странице вакансии «Вам отказали»), `offer` —
# ручной статус веб-интерфейса: по отклику пришёл оффер.
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_QUEUED = "queued"
STATUS_EXCLUDED = "excluded"
STATUS_ERROR = "error"
STATUS_REJECTED = "rejected"
STATUS_OFFER = "offer"
# Работодатель назначил собеседование: статус ставится записи того резюме, по
# отклику которого пришло приглашение. Решение терминальное, повторов нет.
STATUS_INVITE = "invite"

STATUSES = (
    STATUS_SENT,
    STATUS_SKIPPED,
    STATUS_QUEUED,
    STATUS_EXCLUDED,
    STATUS_ERROR,
    STATUS_REJECTED,
    STATUS_OFFER,
    STATUS_INVITE,
)

# Причина отказа: её пишет и агент при обработке, и веб-интерфейс при правке.
REJECTED_REASON = "По отклику пришёл отказ"

# hh.ru показывает, что отклик по вакансии уже отправлен (руками или в прошлом
# запуске): в файл дня такая вакансия идёт со статусом `sent` и этой причиной,
# но лимит откликов запуска не расходует — отклик отправили не мы.
ALREADY_RESPONDED_REASON = "отклик уже отправлен"

# hh.ru показывает кнопку «Отклик другим резюме»: отклик по вакансии уже есть у
# другого резюме. Повторно другим резюме агент не откликается — в файл дня
# вакансия идёт со статусом `skipped` и этой причиной.
OTHER_RESUME_REASON = "Уже отправлен отклик по другому резюме"

# hh.ru показывает «Вас пригласили»: приглашение на интервью уже получено по
# другому резюме, поэтому текущим резюме не откликаемся.
INVITED_OTHER_RESUME_REASON = "Уже получено приглашение на интервью по другому резюме"

# Причина статуса `invite`: приглашение пришло по отклику этого резюме.
INVITE_REASON = "По отклику пришло приглашение на интервью"

# Причина статуса `offer`: работодатель прислал оффер.
OFFER_REASON = "По отклику пришёл оффер"

# hh.ru открывает экран «Вам недоступна эта вакансия»: резюме заблокировано или
# вакансия закрыта для просмотра. Отклик невозможен, вакансия пропускается.
UNAVAILABLE_REASON = "Вакансия недоступна или заблокирована"
# Сайт открывает экран «Вакансия в архиве»: работодатель закрыл вакансию и
# отклики больше не принимает. Вакансия пропускается, в отчёте у неё
# появляется поле `archived: true`.
ARCHIVE_REASON = (
    "Вакансия в архиве, работодатель, больше не принимает отклики на эту вакансию"
)



# Что имеет смысл повторять: отклик не ушёл из-за сбоя, ограничения сайта
# или списка исключённых компаний.
RETRYABLE_STATUSES = (STATUS_ERROR, STATUS_QUEUED, STATUS_EXCLUDED, STATUS_SKIPPED)
