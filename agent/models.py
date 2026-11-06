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
    # Адреса картинок блока описания: работодатель может отдать текст вакансии
    # изображениями. Агент скачивает их и переписывает текст отдельным вызовом.
    description_images: tuple[str, ...] = ()

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
    # Короткая метка для таблицы отчёта и полный текст для окна деталей:
    # в списке видна причина, подробности открываются в модалке.
    report_reason: str = ""
    report_message: str = ""


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
    # Короткая метка и полный текст для таблицы и окна деталей.
    report_reason: str = ""
    report_message: str = ""



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

# Причина, когда отклик не ушёл, а сайт не дал понятного объяснения.
SEND_FAILED_REASON = (
    "Не удалось отправить отклик, посмотрите логи и повторите отправку позже"
)
# Короткие метки для таблицы отчёта: полный текст причины (ответ LLM или
# пошаговое описание сбоя) показывается в окне деталей, чтобы колонка
# «Причина» оставалась плотной и по ней можно было сравнивать вакансии.
SENT_REPORT_REASON = "Отклик отправлен"
NOT_FIT_REPORT_REASON = "Не подходит по оценке LLM"
ERROR_REPORT_REASON = "Сбой обработки вакансии"
# Сайт открывает экран «Вакансия в архиве»: работодатель закрыл вакансию и
# отклики больше не принимает. Вакансия пропускается, в отчёте у неё
# появляется поле `archived: true`.
ARCHIVE_REASON = (
    "Вакансия в архиве, работодатель, больше не принимает отклики на эту вакансию"
)



# Что имеет смысл повторять: отклик не ушёл из-за сбоя, ограничения сайта
# или списка исключённых компаний.
RETRYABLE_STATUSES = (STATUS_ERROR, STATUS_QUEUED, STATUS_EXCLUDED, STATUS_SKIPPED)
