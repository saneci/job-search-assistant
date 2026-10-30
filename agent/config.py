"""Загрузка и валидация конфигурации приложения.

Схема описана pydantic-моделями: типы, диапазоны и обязательные поля проверяются
при старте, а все ошибки собираются за один проход (`ConfigError` со списком).
Раскладка `config.yaml` повторяет модели: `search`, `pipeline`, `data`, `llm`,
`browser`, `notifications`. Источники по убыванию приоритета: аргументы,
окружение, `.env`, `config.yaml` (структура, лежит в git) + `config.local.yaml`
(личные данные, файл не в git), дефолты схемы.
"""

from __future__ import annotations

import os
import random
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    AliasChoices,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .models import RETRYABLE_STATUSES, STATUS_ERROR, STATUS_EXCLUDED, STATUS_QUEUED

try:  # python-dotenv нужен только для чтения .env, для тестов логики не обязателен
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - зависимость есть в requirements.txt
    load_dotenv = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
# Личные данные (адреса, подпись, название резюме, запросы) — в отдельном файле,
# он в .gitignore: в репозитории остаётся только структура config.yaml.
LOCAL_CONFIG_NAME = "config.local.yaml"
DEFAULT_SYSTEM_PROMPT_PATH = PROJECT_ROOT / "SYSTEM_PROMPT.md"

DEFAULT_PROFILE_PATH = PROJECT_ROOT / "data" / "profile.md"
# Файлы сессии раздельные: cookie hh.ru и career.habr.com не мешают друг другу.
DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "hh-state.json"
DEFAULT_HABR_STATE_PATH = PROJECT_ROOT / "data" / "habr-state.json"
# Постоянный профиль Chrome для habr: без него hCaptcha не отрисовывается.
DEFAULT_HABR_PROFILE_PATH = PROJECT_ROOT / "data" / "habr-profile"
DEFAULT_CACHE_PATH = PROJECT_ROOT / "data" / "cache.json"
DEFAULT_LOGS_DIR = PROJECT_ROOT / "logs"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "data" / "report"

DEFAULT_API_KEY_ENV = "AITUNNEL_API_KEY"
DEFAULT_SMTP_USERNAME_ENV = "SMTP_USERNAME"
DEFAULT_SMTP_PASSWORD_ENV = "SMTP_PASSWORD"
DEFAULT_BASE_URL = "https://api.aitunnel.ru/v1"

FIT_LABEL = "подходит"
NOT_FIT_LABEL = "не подходит"

# Критерии для Decision API. Значения по умолчанию; в config.yaml
# (llm.decision.criteria) их можно переопределить — это единственный
# источник числовых порогов и формулировок для модели решений.
DEFAULT_FIT_CRITERIA = (
    "Требования, обязанности, стек и условия вакансии совпадают с профилем "
    "не менее чем на 70%, красных флагов нет"
)
DEFAULT_NOT_FIT_CRITERIA = (
    "Совпадение по ключевым требованиям менее 70%, другая специализация "
    "или есть красные флаги"
)

AREA_IDS: dict[str, int] = {
    "москва": 1,
    "санкт-петербург": 2,
    "екатеринбург": 3,
    "новосибирск": 4,
    "казань": 88,
    "россия": 113,
}

EXPERIENCE_VALUES = frozenset({"noExperience", "between1And3", "between3And6", "moreThan6"})
SCHEDULE_VALUES = frozenset({"fullDay", "shift", "flexible", "remote", "flyInFlyOut"})
# Размеры страницы выдачи, которые понимает hh.ru (параметр items_on_page).
PER_PAGE_VALUES = frozenset({20, 50, 100})

# Каналы поиска: hh — поиск по запросам, habr — лента «Подходящие».
CHANNEL_VALUES = frozenset({"hh", "habr"})

DEFAULT_RETRY_STATUSES = (STATUS_ERROR, STATUS_QUEUED, STATUS_EXCLUDED)


class ConfigError(RuntimeError):
    """Некорректная конфигурация."""


def default_decision_criteria() -> dict[str, str]:
    """Критерии Decision API по умолчанию (переопределяются в config.yaml)."""
    return {FIT_LABEL: DEFAULT_FIT_CRITERIA, NOT_FIT_LABEL: DEFAULT_NOT_FIT_CRITERIA}


# --- Примитивы схемы ------------------------------------------------------
# Проверки типов пишут сообщения по-русски: они попадают в консоль при старте.


def _as_int(value: Any) -> int:
    """Целое число; строки допускаются для переопределений из окружения."""
    if isinstance(value, bool):
        raise ValueError("должно быть целым числом")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError("должно быть целым числом")


def _as_float(value: Any) -> float:
    """Число; строки допускаются для переопределений из окружения."""
    if isinstance(value, bool):
        raise ValueError("должно быть числом")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            raise ValueError("должно быть числом") from None
    raise ValueError("должно быть числом")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
        return value.strip().lower() in {"true", "1"}
    raise ValueError("должно быть true или false")


PositiveInt = Annotated[int, BeforeValidator(_as_int), Field(gt=0)]
NonNegativeInt = Annotated[int, BeforeValidator(_as_int), Field(ge=0)]
PositiveFloat = Annotated[float, BeforeValidator(_as_float), Field(gt=0)]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _str(value: Any, key: str) -> str:
    text = _optional_str(value)
    if not text:
        raise ValueError(f"Параметр {key} не может быть пустым")
    return text


def _channel_name(value: Any, key: str) -> str:
    """Канал поиска: только значения из CHANNEL_VALUES, регистр не важен."""
    text = (_optional_str(value) or "").lower()
    if text not in CHANNEL_VALUES:
        allowed = ", ".join(sorted(CHANNEL_VALUES))
        raise ValueError(f"Параметр {key} должен быть одним из: {allowed}")
    return text


def _positive_int(value: Any, key: str) -> int:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())  # значение из окружения приходит строкой
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Параметр {key} должен быть целым числом")
    if value <= 0:
        raise ValueError(f"Параметр {key} должен быть больше нуля")
    return value


def _optional_positive_int(value: Any, key: str) -> int | None:
    """Необязательное положительное число: пустое значение означает None."""
    if value is None:
        return None
    return _positive_int(value, key)


def _optional_non_negative_int(value: Any, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())  # значение из окружения приходит строкой
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Параметр {key} должен быть целым числом")
    if value < 0:
        raise ValueError(f"Параметр {key} не может быть отрицательным")
    return value


def _page_size(value: Any, key: str) -> int:
    """Размер страницы выдачи: hh.ru понимает только 20, 50 и 100 карточек."""
    size = _positive_int(value, key)
    if size not in PER_PAGE_VALUES:
        allowed = ", ".join(str(item) for item in sorted(PER_PAGE_VALUES))
        raise ValueError(f"Параметр {key} должен быть одним из: {allowed}")
    return size


def _parse_queries(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        queries = (value.strip(),)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        queries = tuple(str(item).strip() for item in value)
    else:
        raise ValueError("search.queries должен быть строкой или списком строк")
    queries = tuple(query for query in queries if query)
    if not queries:
        raise ValueError("search.queries должен содержать хотя бы один непустой запрос")
    return queries


def _str_list(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError(f"Параметр {key} должен быть списком строк")


def _filter_values(value: Any, key: str, allowed: frozenset[str]) -> tuple[str, ...]:
    """Значения фильтра hh.ru: одна строка или список строк, без повторов.

    Несколько значений означают несколько отмеченных чекбоксов в фильтре
    на hh.ru (например, опыт «от 1 года до 3 лет» и «от 3 до 6 лет»).
    """
    if value is None:
        return ()
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = list(value)
    else:
        raise ValueError(f"{key} должен быть строкой или списком строк")

    values: list[str] = []
    for item in items:
        text = _optional_str(item)
        if not text:
            continue
        if text not in allowed:
            raise ValueError(
                f"{key}: неизвестное значение «{text}» (допустимо: "
                + ", ".join(sorted(allowed))
                + ")"
            )
        if text not in values:
            values.append(text)
    return tuple(values)


def _schedule(value: Any) -> str | None:
    schedule = _optional_str(value)
    if schedule is not None and schedule not in SCHEDULE_VALUES:
        raise ValueError("schedule должен быть одним из: " + ", ".join(sorted(SCHEDULE_VALUES)))
    return schedule


def _resolve_project_path(value: Any, key: str) -> Path:
    text = _str(value, key)
    path = Path(text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ValueError(f"{key} должен указывать внутрь каталога проекта: {resolved}")
    return resolved


def _area_text(value: Any) -> str:
    if value is None:
        raise ValueError("Обязательный параметр отсутствует: search.area")
    return _str(value, "search.area")


def _area_id(value: Any) -> int:
    """id региона hh.ru; ошибка конфигурации переводится в ошибку схемы."""
    try:
        return resolve_area_id(value)
    except ConfigError as exc:
        raise ValueError(str(exc)) from None


def _decision_criteria(value: Any) -> dict[str, str]:
    """Критерии Decision API: значения по умолчанию, переопределённые из конфига."""
    criteria = default_decision_criteria()
    if value is None:
        return criteria
    if not isinstance(value, Mapping):
        raise ValueError(
            "llm.decision.criteria должен быть словарём вида "
            f"{{{FIT_LABEL}: '...', {NOT_FIT_LABEL}: '...'}}"
        )
    for label, text in value.items():
        name = str(label).strip()
        if name not in criteria:
            raise ValueError(
                f"llm.decision.criteria: неизвестный вариант «{name}» "
                f"(допустимо: {FIT_LABEL}, {NOT_FIT_LABEL})"
            )
        criteria[name] = _str(text, f"llm.decision.criteria.{name}")
    return criteria


def _drop_path(data: dict[str, Any], path: str) -> None:
    """Удаляет вложенный ключ вида `a.b.c` из словаря источника значений."""
    *parents, leaf = path.split(".")
    node: Any = data
    for part in parents:
        child = node.get(part) if isinstance(node, Mapping) else None
        if not isinstance(child, Mapping):
            return
        node[part] = dict(child)
        node = node[part]
    if isinstance(node, dict):
        node.pop(leaf, None)


# --- Модели конфигурации --------------------------------------------------


class DelayRange(BaseModel):
    """Случайная пауза между действиями."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum: float
    maximum: float

    @model_validator(mode="before")
    @classmethod
    def _from_config(cls, data: Any) -> Any:
        """Принимает {from, to}, {min, max}, список из двух чисел и саму модель."""
        if isinstance(data, DelayRange):
            return {"minimum": data.minimum, "maximum": data.maximum}
        if isinstance(data, Mapping):
            raw = dict(data)
            if "minimum" not in raw and "maximum" not in raw:
                raw = {
                    "minimum": raw.get("from", raw.get("min")),
                    "maximum": raw.get("to", raw.get("max")),
                }
        elif isinstance(data, Sequence) and not isinstance(data, (str, bytes)) and len(data) == 2:
            raw = {"minimum": data[0], "maximum": data[1]}
        else:
            raise ValueError("должен быть словарём {from, to} или списком из двух чисел")

        values: dict[str, float] = {}
        for field, source in (("minimum", "from"), ("maximum", "to")):
            value = raw.get(field)
            if value is None:
                raise ValueError(f"не задано значение {source}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("должен содержать числа")
            if value < 0:
                raise ValueError("не может содержать отрицательные значения")
            values[field] = float(value)
        if values["minimum"] > values["maximum"]:
            raise ValueError("значение from больше значения to")
        return values

    def next(self) -> float:
        return random.uniform(self.minimum, self.maximum)


class ResumeConfig(BaseModel):
    """Подъём резюме в поиске: название резюме и минимальный интервал между подъёмами."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = ""
    boost_interval_hours: PositiveInt = 4

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str:
        return _optional_str(value) or ""


class RetryConfig(BaseModel):
    """Какие вакансии из дневных отчётов повторять при `--retry`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    statuses: tuple[str, ...] = DEFAULT_RETRY_STATUSES

    @field_validator("statuses", mode="before")
    @classmethod
    def _statuses(cls, value: Any) -> tuple[str, ...]:
        statuses = _filter_values(value, "statuses", frozenset(RETRYABLE_STATUSES))
        return statuses or DEFAULT_RETRY_STATUSES


class SearchConfig(BaseModel):
    """Параметры поиска: канал (hh/habr) и фильтры hh.ru."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Канал поиска: hh — запросы и фильтры, habr — лента «Подходящие».
    channel: str = "hh"
    # Для habr запросы и регион не нужны: лента строится по профилю пользователя.
    queries: tuple[str, ...] = ()
    area: str = ""
    salary_from: int | None = None
    experience: tuple[str, ...] = ()
    schedule: str | None = None
    exclude_keywords: tuple[str, ...] = ()
    exclude_companies: tuple[str, ...] = ()
    per_page: int = 100
    pages_limit: int | None = None

    @property
    def area_id(self) -> int:
        """id региона hh.ru: единственный источник — area, второго поля нет."""
        return resolve_area_id(self.area)

    @field_validator("channel", mode="before")
    @classmethod
    def _channel_value(cls, value: Any) -> str:
        return _channel_name(value, "search.channel")

    @field_validator("queries", mode="before")
    @classmethod
    def _queries(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return _parse_queries(value)

    @field_validator("area", mode="before")
    @classmethod
    def _area(cls, value: Any) -> str:
        if value is None or not str(value).strip():
            return ""
        text = _area_text(value)
        _area_id(text)  # регион проверяем при загрузке, а не при построении URL
        return text

    @field_validator("salary_from", mode="before")
    @classmethod
    def _salary(cls, value: Any) -> int | None:
        return _optional_non_negative_int(value, "search.salary_from")

    @field_validator("experience", mode="before")
    @classmethod
    def _experience(cls, value: Any) -> tuple[str, ...]:
        return _filter_values(value, "search.experience", EXPERIENCE_VALUES)

    @field_validator("schedule", mode="before")
    @classmethod
    def _schedule_value(cls, value: Any) -> str | None:
        return _schedule(value)

    @field_validator("exclude_keywords", mode="before")
    @classmethod
    def _exclude_keywords(cls, value: Any) -> tuple[str, ...]:
        return _str_list(value, "search.exclude_keywords")

    @field_validator("exclude_companies", mode="before")
    @classmethod
    def _exclude_companies(cls, value: Any) -> tuple[str, ...]:
        return _str_list(value, "search.exclude_companies")

    @field_validator("pages_limit", mode="before")
    @classmethod
    def _pages_limit(cls, value: Any) -> int | None:
        return _optional_positive_int(value, "search.pages_limit")

    @field_validator("per_page", mode="before")
    @classmethod
    def _per_page(cls, value: Any) -> int:
        return _page_size(value, "search.per_page")

    @model_validator(mode="after")
    def _required_fields(self) -> "SearchConfig":
        """Запросы и регион обязательны только для поиска hh.ru."""
        if self.channel == "hh":
            if not self.queries:
                raise ValueError("Обязательный параметр отсутствует: search.queries")
            if not self.area:
                raise ValueError("Обязательный параметр отсутствует: search.area")
        return self


class PipelineConfig(BaseModel):
    """Параметры запуска: лимиты, паузы, подъём резюме и повторы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resume: ResumeConfig = Field(default_factory=ResumeConfig)
    limit_per_run: int
    delay_between_actions: DelayRange
    max_vacancies: int = 200
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @field_validator("limit_per_run", mode="before")
    @classmethod
    def _limit_per_run(cls, value: Any) -> int:
        return _positive_int(value, "pipeline.limit_per_run")

    @field_validator("max_vacancies", mode="before")
    @classmethod
    def _max_vacancies(cls, value: Any) -> int:
        return _positive_int(value, "pipeline.max_vacancies")


class DataPathsConfig(BaseModel):
    """Пути к локальным данным проекта."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: Path = DEFAULT_PROFILE_PATH
    # Сессия hh.ru: имя ключа прежнее, изменился только файл по умолчанию.
    session_state: Path = DEFAULT_STATE_PATH
    # Сессия career.habr.com и постоянный профиль Chrome для hCaptcha.
    habr_session_state: Path = DEFAULT_HABR_STATE_PATH
    habr_profile: Path = DEFAULT_HABR_PROFILE_PATH
    cache: Path = DEFAULT_CACHE_PATH
    report_dir: Path = DEFAULT_REPORT_DIR
    logs_dir: Path = DEFAULT_LOGS_DIR

    @field_validator(
        "profile",
        "session_state",
        "habr_session_state",
        "habr_profile",
        "cache",
        "report_dir",
        "logs_dir",
        mode="before",
    )
    @classmethod
    def _project_path(cls, value: Any, info: ValidationInfo) -> Path:
        return _resolve_project_path(value, str(info.field_name))


class DataConfig(BaseModel):
    """Локальные данные: срок хранения логов и пути."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Журналы и скриншоты старше срока удаляются при старте.
    # Дневные отчёты (data/report) не удаляются никогда: это история для аналитики.
    logs_retention_days: PositiveInt = 30
    paths: DataPathsConfig = Field(default_factory=DataPathsConfig)


class CoverLetterConfig(BaseModel):
    """Сопроводительное письмо: лимит длины и подпись с контактами."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_length: PositiveInt = 1500
    # Личное поле: живёт в config.local.yaml, из окружения не читается.
    signature: str = ""
    # Ссылки на проекты: нужны, если вакансия просит приложить их в письме.
    pet_projects: Annotated[tuple[str, ...], NoDecode] = Field(
        default_factory=tuple,
        validation_alias=AliasChoices("pet-projects", "pet_projects"),
    )


    @field_validator("signature", mode="before")
    @classmethod
    def _signature(cls, value: Any) -> str:
        return str(value).strip() if value is not None else ""

    @field_validator("pet_projects", mode="before")
    @classmethod
    def _pet_projects(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            items: Sequence[Any] = (value,)
        elif isinstance(value, Sequence):
            items = value
        else:
            raise ValueError("llm.cover_letter.pet-projects должен быть списком ссылок")
        result: list[str] = []
        for item in items:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return tuple(result)



class DecisionConfig(BaseModel):
    """Параметры Decision-вызова (модель решений)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = "gpt-6-luna-decisions"
    threshold: float = 0.8
    criteria: dict[str, str] = Field(default_factory=default_decision_criteria)
    explain_rejections: bool = False

    @field_validator("model", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        return _str(value, "llm.decision.model")

    @field_validator("threshold", mode="before")
    @classmethod
    def _threshold(cls, value: Any) -> float:
        number = _as_float(value)
        if not 0 < number <= 1:
            raise ValueError("llm.decision.threshold должен быть в диапазоне (0, 1]")
        return number

    @field_validator("criteria", mode="before")
    @classmethod
    def _criteria(cls, value: Any) -> dict[str, str]:
        return _decision_criteria(value)

    @field_validator("explain_rejections", mode="before")
    @classmethod
    def _explain(cls, value: Any) -> bool:
        return _as_bool(value)


class GenerationConfig(BaseModel):
    """Параметры Generation-вызова (чат-модель)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = "gpt-6-luna-pro"
    max_tokens: PositiveInt = 2000
    temperature: PositiveFloat = 0.7
    json_mode: bool = False
    retry_on_invalid_json: bool = True

    @field_validator("model", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        return _str(value, "llm.generation.model")

    @field_validator("json_mode", "retry_on_invalid_json", mode="before")
    @classmethod
    def _flags(cls, value: Any) -> bool:
        return _as_bool(value)


class LLMConfig(BaseModel):
    """Настройки LLM-слоя: общий транспорт и два вызова."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Секрет: в конфиге хранится только имя переменной окружения с ключом.
    api_key: SecretStr = SecretStr("")
    api_key_env: str = DEFAULT_API_KEY_ENV
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: PositiveFloat = 120.0
    system_prompt_path: Path | None = DEFAULT_SYSTEM_PROMPT_PATH
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    cover_letter: CoverLetterConfig = Field(default_factory=CoverLetterConfig)

    @field_validator("api_key_env", mode="before")
    @classmethod
    def _api_key_env(cls, value: Any) -> str:
        return _optional_str(value) or DEFAULT_API_KEY_ENV

    @field_validator("base_url", mode="before")
    @classmethod
    def _base_url(cls, value: Any) -> str:
        return _str(value, "llm.base_url")

    @field_validator("system_prompt_path", mode="before")
    @classmethod
    def _system_prompt(cls, value: Any) -> Path:
        """Путь к промпту: относительный считается от корня проекта, файл обязателен."""
        if value is None or not str(value).strip():
            return DEFAULT_SYSTEM_PROMPT_PATH
        path = Path(str(value).strip())
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path = path.resolve()
        if not path.is_file():
            raise ValueError(
                "llm.system_prompt_path: файл с системным промптом не найден — "
                f"{path}. Создайте его или укажите другой путь в config.yaml"
            )
        return path


class BrowserConfig(BaseModel):
    """Настройки браузера."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    headless: bool = False
    slow_mo_ms: NonNegativeInt = 0
    channel: str | None = None
    # Playwright по умолчанию запускает Chromium с --no-sandbox: браузер пишет
    # предупреждение о стабильности и безопасности. true — sandbox включён.
    # false нужно только там, где sandbox не стартует (root, Linux без userns).
    chromium_sandbox: bool = True
    # Сколько секунд ждать, пока человек решит капчу в открытом окне (0 — не ждать).
    captcha_wait_seconds: NonNegativeInt = 120

    @field_validator("headless", mode="before")
    @classmethod
    def _headless(cls, value: Any) -> bool:
        return _as_bool(value)

    @field_validator("channel", mode="before")
    @classmethod
    def _channel(cls, value: Any) -> str | None:
        return _optional_str(value)


class EmailConfig(BaseModel):
    """SMTP-канал: общий транспорт для отчёта и уведомлений."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: PositiveInt = 465
    use_ssl: bool = True
    sender: str = ""
    recipient: str = ""
    username: str = ""
    # Секрет: приходит из окружения (SMTP_PASSWORD), в конфиге не хранится.
    password: SecretStr = SecretStr("")

    @field_validator("enabled", "use_ssl", mode="before")
    @classmethod
    def _flags(cls, value: Any) -> bool:
        return _as_bool(value)

    @field_validator("smtp_host", "sender", "recipient", "username", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _optional_str(value) or ""


class NotificationConfig(BaseModel):
    """Один вид писем: нужен ли он и на какой адрес.

    Пустой `recipient` означает «общий адрес из notifications.email.recipient».
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    recipient: str = ""

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled(cls, value: Any) -> bool:
        return _as_bool(value)

    @field_validator("recipient", mode="before")
    @classmethod
    def _recipient(cls, value: Any) -> str:
        return _optional_str(value) or ""

    def target(self, channel: EmailConfig) -> str:
        """Адрес получателя: свой у этого вида писем или общий из канала."""
        return self.recipient or channel.recipient


class NotificationsConfig(BaseModel):
    """Письма на email: общий транспорт и отдельные виды писем."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    email: EmailConfig = Field(default_factory=EmailConfig)
    report: NotificationConfig = Field(default_factory=NotificationConfig)
    captcha: NotificationConfig = Field(default_factory=NotificationConfig)


# --- Источники значений ---------------------------------------------------


def _load_yaml(text: str) -> Any:
    """Разбирает YAML чистым Python-реализацией (без C-расширений и компиляции)."""
    return YAML(typ="safe", pure=True).load(text)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Читает YAML-файл в словарь; пустой файл — пустой словарь."""
    try:
        data = _load_yaml(path.read_text(encoding="utf-8")) or {}
    except YAMLError as exc:
        raise ConfigError(f"Не удалось разобрать {path.name}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError(f"{path.name}: корневой элемент должен быть словарём")
    return dict(data)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Накладывает override на base: словари сливаются, значения — перекрываются."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# Какие файлы читать в текущем вызове load_config: путь к config.yaml и к
# локальному файлу рядом с ним. ContextVar — потому что pydantic-settings
# создаёт источники без параметров.
_SOURCES: ContextVar[tuple[Path, Path | None]] = ContextVar(
    "hh_config_sources", default=(DEFAULT_CONFIG_PATH, None)
)


class YamlSource(PydanticBaseSettingsSource):
    """Слой config.yaml + config.local.yaml (личные данные поверх структуры)."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # noqa: ARG002
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        base_path, local_path = _SOURCES.get()
        data = _read_yaml(base_path)
        if local_path is not None and local_path.exists():
            data = _deep_merge(data, _read_yaml(local_path))
        data["source_path"] = str(base_path)
        return data

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return "YamlSource()"


# Личные данные из окружения не читаются: их место — config.local.yaml.
# Иначе старый .env молча перекрывал бы файл (а переводы строк в .env
# превращаются в литеральные «\n» и ломают подпись).
PERSONAL_FIELDS = frozenset({
    "llm.cover_letter.signature",
    "llm.cover_letter.pet_projects",
})


class EnvSourceWithoutPersonal(PydanticBaseSettingsSource):
    """Обёртка над источником: отбрасывает личные поля, остальное пропускает."""

    def __init__(self, source: PydanticBaseSettingsSource, blocked: frozenset[str]) -> None:
        super().__init__(source.settings_cls)
        self._source = source
        self._blocked = blocked

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        # Оставляем только поля схемы: в окружении и .env лежат ещё секреты
        # (AITUNNEL_API_KEY, SMTP_*) — их читает _with_secrets, а не валидатор.
        allowed = set(self.settings_cls.model_fields)
        data = {key: value for key, value in self._source().items() if key in allowed}
        for path in self._blocked:
            _drop_path(data, path)
        return data

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"EnvSourceWithoutPersonal({self._source!r})"


class AppConfig(BaseSettings):
    """Полная конфигурация запуска: разделы config.yaml и секреты из окружения."""

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    source_path: Path
    search: SearchConfig
    pipeline: PipelineConfig
    data: DataConfig = Field(default_factory=DataConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Приоритет: аргументы → окружение → .env → YAML → файлы секретов.

        Личные поля (`PERSONAL_FIELDS`) из окружения и `.env` не читаются:
        они живут только в config.local.yaml/config.yaml.
        """
        return (
            init_settings,
            EnvSourceWithoutPersonal(env_settings, PERSONAL_FIELDS),
            EnvSourceWithoutPersonal(dotenv_settings, PERSONAL_FIELDS),
            YamlSource(settings_cls),
            file_secret_settings,
        )


# --- Загрузка -------------------------------------------------------------


def load_env() -> None:
    """Подхватывает .env из корня проекта (если он есть)."""
    if load_dotenv is None:
        return
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


_TYPE_MESSAGES = {
    "missing": "обязательный параметр отсутствует",
    "greater_than": "должно быть больше нуля",
    "greater_than_equal": "не может быть отрицательным",
    "less_than_equal": "слишком большое значение",
    "int_type": "должно быть целым числом",
    "int_parsing": "должно быть целым числом",
    "float_type": "должно быть числом",
    "float_parsing": "должно быть числом",
    "bool_type": "должно быть true или false",
    "string_type": "должно быть строкой",
    "list_type": "должен быть списком",
    "tuple_type": "должен быть списком",
    "path_type": "должно быть путём",
    "extra_forbidden": "неизвестный параметр — проверьте раскладку config.yaml",
}

_LOCATION_NAMES = {"source_path": "config"}


def _error_location(error: Mapping[str, Any]) -> list[str]:
    """Путь ошибки в терминах config.yaml."""
    location = [str(part) for part in error["loc"]]
    if location and location[0] in _LOCATION_NAMES:
        location[0] = _LOCATION_NAMES[location[0]]
    return location


def _format_errors(exc: ValidationError) -> str:
    """Все ошибки схемы одной строкой: «ключ: причина», по одной на строку."""
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(_error_location(error)) or "config"
        message = str(error["msg"])
        if message.startswith("Value error, "):
            message = message[len("Value error, ") :]
        else:
            message = _TYPE_MESSAGES.get(str(error["type"]), message)
        lines.append(f"{location}: {message}")
    if len(lines) == 1:
        return lines[0]
    return f"найдено ошибок: {len(lines)}\n" + "\n".join(f"  - {line}" for line in lines)


def _with_secrets(config: AppConfig) -> AppConfig:
    """Добавляет секреты из окружения: ключ LLM и SMTP-пароль.

    В config.yaml и config.local.yaml их нет — только имена переменных.
    """
    api_key = os.environ.get(config.llm.api_key_env or DEFAULT_API_KEY_ENV, "").strip()
    email = config.notifications.email
    smtp_username = os.environ.get(DEFAULT_SMTP_USERNAME_ENV, "").strip() or email.sender
    smtp_password = os.environ.get(DEFAULT_SMTP_PASSWORD_ENV, "")
    llm = config.llm.model_copy(update={"api_key": SecretStr(api_key)})
    email = email.model_copy(
        update={"username": smtp_username, "password": SecretStr(smtp_password)}
    )
    notifications = config.notifications.model_copy(update={"email": email})
    return config.model_copy(update={"llm": llm, "notifications": notifications})


def load_config(config_path: Path | str | None = None) -> AppConfig:
    """Читает config.yaml (+ config.local.yaml) и переменные окружения."""
    load_env()

    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise ConfigError(f"Файл конфигурации не найден: {path}")

    local_path = path.with_name(LOCAL_CONFIG_NAME)
    token = _SOURCES.set((path, local_path))
    try:
        config = AppConfig()  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from None
    finally:
        _SOURCES.reset(token)

    return _with_secrets(config)


def resolve_area_id(value: Any) -> int:
    """Преобразует регион из конфигурации в id региона hh.ru."""
    if isinstance(value, bool):
        raise ConfigError("Параметр area должен быть названием региона или числовым id")
    if isinstance(value, int):
        if value <= 0:
            raise ConfigError("Параметр area: числовой id региона должен быть положительным")
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ConfigError("Параметр area не может быть пустым")
        if text.isdigit():
            return int(text)
        key = text.lower().replace("ё", "е")
        if key in AREA_IDS:
            return AREA_IDS[key]
        raise ConfigError(
            f"Неизвестный регион area: {value!r}. Укажите числовой id hh.ru "
            f"или одно из названий: {', '.join(sorted(AREA_IDS))}"
        )
    raise ConfigError("Параметр area должен быть названием региона или числовым id")
