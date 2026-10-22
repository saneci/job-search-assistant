"""Загрузка и валидация конфигурации приложения."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

try:  # python-dotenv нужен только для чтения .env, для тестов логики не обязателен
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - зависимость есть в requirements.txt
    load_dotenv = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_SYSTEM_PROMPT_PATH = PROJECT_ROOT / "SYSTEM_PROMPT.md"

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


class ConfigError(RuntimeError):
    """Некорректная конфигурация."""


def default_decision_criteria() -> dict[str, str]:
    """Критерии Decision API по умолчанию (переопределяются в config.yaml)."""
    return {FIT_LABEL: DEFAULT_FIT_CRITERIA, NOT_FIT_LABEL: DEFAULT_NOT_FIT_CRITERIA}


@dataclass(frozen=True)
class DelayRange:
    """Случайная пауза между действиями."""

    minimum: float
    maximum: float

    def next(self) -> float:
        return random.uniform(self.minimum, self.maximum)


@dataclass(frozen=True)
class SearchConfig:
    """Параметры поиска вакансий."""

    queries: tuple[str, ...]
    area: str
    area_id: int
    salary_from: int | None
    experience: tuple[str, ...]
    schedule: str | None
    exclude_keywords: tuple[str, ...]
    exclude_companies: tuple[str, ...]
    limit_per_run: int
    delay: DelayRange
    pages_limit: int
    per_page: int
    max_vacancies: int


@dataclass(frozen=True)
class DecisionConfig:
    """Параметры Decision-вызова (модель решений)."""

    base_url: str
    model: str
    threshold: float
    criteria: dict[str, str] = field(default_factory=dict)
    explain_rejections: bool = False


@dataclass(frozen=True)
class GenerationConfig:
    """Параметры Generation-вызова (чат-модель)."""

    base_url: str
    model: str
    max_tokens: int
    temperature: float
    json_mode: bool
    retry_on_invalid_json: bool


@dataclass(frozen=True)
class LLMConfig:
    """Настройки LLM-слоя."""

    api_key: str
    timeout: float
    decision: DecisionConfig
    generation: GenerationConfig
    system_prompt_path: Path | None = None


@dataclass(frozen=True)
class BrowserConfig:
    """Настройки браузера."""

    headless: bool
    slow_mo_ms: int
    channel: str | None = None
    # Сколько секунд ждать, пока человек решит капчу в открытом окне (0 — не ждать).
    captcha_wait_seconds: int = 120


@dataclass(frozen=True)
class EmailConfig:
    """Настройки отправки отчёта на email."""

    enabled: bool
    smtp_host: str
    smtp_port: int
    use_ssl: bool
    sender: str
    recipient: str
    username: str
    password: str


@dataclass(frozen=True)
class ResumeConfig:
    """Подъём резюме в поиске: название резюме и минимальный интервал между подъёмами."""

    name: str = ""
    boost_interval_hours: int = 4


@dataclass(frozen=True)
class AppConfig:
    """Полная конфигурация запуска."""

    source_path: Path
    search: SearchConfig
    llm: LLMConfig
    browser: BrowserConfig
    email: EmailConfig
    profile_path: Path
    state_path: Path
    cache_path: Path
    logs_dir: Path
    report_dir: Path
    cover_letter_max_length: int = 1500
    cover_letter_signature: str = ""
    processed_retention_days: int = 7
    resume: ResumeConfig = field(default_factory=ResumeConfig)


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


def load_config(config_path: Path | str | None = None) -> AppConfig:
    """Читает YAML-конфигурацию и переменные окружения."""
    load_env()

    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise ConfigError(f"Файл конфигурации не найден: {path}")

    try:
        raw = _load_yaml(path.read_text(encoding="utf-8")) or {}
    except YAMLError as exc:
        raise ConfigError(f"Не удалось разобрать {path.name}: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise ConfigError("Корневой элемент config.yaml должен быть словарём")

    paths = _section(raw, "paths")
    return AppConfig(
        source_path=path,
        search=_load_search(raw),
        llm=_load_llm(raw),
        browser=_load_browser(raw),
        email=_load_email(raw),
        profile_path=_resolve_project_path(paths.get("profile", "PROFILE.md"), "paths.profile"),
        state_path=_resolve_project_path(paths.get("session_state", "state.json"), "paths.session_state"),
        cache_path=_resolve_project_path(paths.get("cache", "data/cache.json"), "paths.cache"),
        logs_dir=_resolve_project_path(paths.get("logs_dir", ".logs"), "paths.logs_dir"),
        report_dir=_resolve_project_path(
            paths.get("report_dir") or paths.get("processed_dir") or "data/report",
            "paths.report_dir",
        ),
        processed_retention_days=_positive_int(
            raw.get("processed_retention_days", 7), "processed_retention_days"
        ),
        cover_letter_max_length=_positive_int(
            raw.get("cover_letter_max_length", 1500), "cover_letter_max_length"
        ),
        cover_letter_signature=_optional_str(raw.get("cover_letter_signature")) or "",
        resume=_load_resume(raw),
    )


def _load_resume(raw: Mapping[str, Any]) -> ResumeConfig:
    """Параметры подъёма резюме в поиске; пустое имя — подъём отключён."""
    section = _section(raw, "resume")
    return ResumeConfig(
        name=_optional_str(section.get("name")) or "",
        boost_interval_hours=_positive_int(
            section.get("boost_interval_hours", 4), "resume.boost_interval_hours"
        ),
    )


def _load_yaml(text: str) -> Any:
    """Разбирает YAML чистым Python-реализацией (без C-расширений и компиляции)."""
    return YAML(typ="safe", pure=True).load(text)


def load_env() -> None:
    """Подхватывает .env из корня проекта (если он есть)."""
    if load_dotenv is None:
        return
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _load_search(raw: Mapping[str, Any]) -> SearchConfig:
    queries = _parse_queries(raw.get("search_query"))
    area = _require(raw, "area", "")
    area_id = resolve_area_id(area)

    experience = _filter_values(raw.get("experience"), "experience", EXPERIENCE_VALUES)
    schedule = _optional_str(raw.get("schedule"))
    if schedule is not None and schedule not in SCHEDULE_VALUES:
        raise ConfigError(
            "schedule должен быть одним из: " + ", ".join(sorted(SCHEDULE_VALUES))
        )

    return SearchConfig(
        queries=queries,
        area=str(area),
        area_id=area_id,
        salary_from=_optional_non_negative_int(raw.get("salary_from"), "salary_from"),
        experience=experience,
        schedule=schedule,
        exclude_keywords=_str_list(raw.get("exclude_keywords"), "exclude_keywords"),
        exclude_companies=_str_list(raw.get("exclude_companies"), "exclude_companies"),
        limit_per_run=_positive_int(_limit_per_run_value(raw), "limit_per_run"),
        delay=_parse_delay(_require(raw, "delay_between_actions", "")),
        pages_limit=_positive_int(raw.get("pages_limit", 5), "pages_limit"),
        per_page=min(_positive_int(raw.get("per_page", 50), "per_page"), 100),
        max_vacancies=_positive_int(raw.get("max_vacancies", 200), "max_vacancies"),
    )


def _limit_per_run_value(raw: Mapping[str, Any]) -> Any:
    """Значение лимита откликов за запуск.

    Основной ключ — limit_per_run; daily_limit (ключ из ТЗ) поддерживается
    как устаревший синоним, чтобы старые конфиги продолжали работать.
    """
    if raw.get("limit_per_run") is not None:
        return raw["limit_per_run"]
    if raw.get("daily_limit") is not None:
        return raw["daily_limit"]
    raise ConfigError("Обязательный параметр отсутствует: limit_per_run")


def _load_llm(raw: Mapping[str, Any]) -> LLMConfig:
    section = _section(raw, "llm")
    api_key_env = _optional_str(section.get("api_key_env")) or "AITUNNEL_API_KEY"
    api_key = os.environ.get(api_key_env, "").strip()

    decision_raw = _section(section, "decision")
    generation_raw = _section(section, "generation")

    threshold = _float(decision_raw.get("threshold", 0.8), "llm.decision.threshold")
    if not 0 < threshold <= 1:
        raise ConfigError("llm.decision.threshold должен быть в диапазоне (0, 1]")

    criteria = _load_decision_criteria(decision_raw.get("criteria"))

    system_prompt_path = (
        _optional_project_path(section.get("system_prompt_path"), "llm.system_prompt_path")
        or DEFAULT_SYSTEM_PROMPT_PATH
    )
    if not system_prompt_path.is_file():
        raise ConfigError(
            "llm.system_prompt_path: файл с системным промптом не найден — "
            f"{system_prompt_path}. Создайте его или укажите другой путь в config.yaml"
        )

    return LLMConfig(
        api_key=api_key,
        timeout=float(_positive_int(section.get("timeout_seconds", 120), "llm.timeout_seconds")),
        system_prompt_path=system_prompt_path,
        decision=DecisionConfig(
            base_url=_str(decision_raw.get("base_url", "https://api.aitunnel.ru/v1"), "llm.decision.base_url"),
            model=_str(decision_raw.get("model", "gpt-6-luna-decisions"), "llm.decision.model"),
            threshold=threshold,
            criteria=criteria,
            explain_rejections=_bool(
                decision_raw.get("explain_rejections", False), "llm.decision.explain_rejections"
            ),
        ),
        generation=GenerationConfig(
            base_url=_str(
                generation_raw.get("base_url", "https://api.aitunnel.ru/v1"),
                "llm.generation.base_url",
            ),
            model=_str(generation_raw.get("model", "gpt-6-luna-pro"), "llm.generation.model"),
            max_tokens=_positive_int(generation_raw.get("max_tokens", 2000), "llm.generation.max_tokens"),
            temperature=_float(generation_raw.get("temperature", 0.7), "llm.generation.temperature"),
            json_mode=bool(generation_raw.get("json_mode", False)),
            retry_on_invalid_json=bool(generation_raw.get("retry_on_invalid_json", True)),
        ),
    )


def _load_browser(raw: Mapping[str, Any]) -> BrowserConfig:
    section = _section(raw, "browser")
    slow_mo = _optional_non_negative_int(section.get("slow_mo_ms"), "browser.slow_mo_ms")
    captcha_wait = _optional_non_negative_int(
        section.get("captcha_wait_seconds"), "browser.captcha_wait_seconds"
    )
    return BrowserConfig(
        headless=bool(section.get("headless", False)),
        slow_mo_ms=slow_mo or 0,
        channel=_optional_str(section.get("channel")),
        captcha_wait_seconds=captcha_wait if captcha_wait is not None else 120,
    )


def _load_email(raw: Mapping[str, Any]) -> EmailConfig:
    notifications = _section(raw, "notifications")
    section = _section(notifications, "email")
    sender = _optional_str(section.get("sender")) or ""
    username = os.environ.get("SMTP_USERNAME", "").strip() or sender
    return EmailConfig(
        enabled=bool(section.get("enabled", False)),
        smtp_host=_optional_str(section.get("smtp_host")) or "",
        smtp_port=_positive_int(section.get("smtp_port", 465), "notifications.email.smtp_port"),
        use_ssl=bool(section.get("use_ssl", True)),
        sender=sender,
        recipient=_optional_str(section.get("recipient")) or "",
        username=username,
        password=os.environ.get("SMTP_PASSWORD", ""),
    )


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"Раздел {key} должен быть словарём")
    return value


def _require(raw: Mapping[str, Any], key: str, section: str) -> Any:
    if key not in raw or raw[key] is None:
        name = f"{section}.{key}" if section else key
        raise ConfigError(f"Обязательный параметр отсутствует: {name}")
    return raw[key]


def _parse_queries(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        queries = (value.strip(),)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        queries = tuple(str(item).strip() for item in value)
    else:
        raise ConfigError("Обязательный параметр отсутствует или некорректен: search_query")
    queries = tuple(query for query in queries if query)
    if not queries:
        raise ConfigError("search_query должен содержать хотя бы один непустой запрос")
    return queries


def _str_list(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ConfigError(f"Параметр {key} должен быть списком строк")


def _parse_delay(value: Any) -> DelayRange:
    if isinstance(value, Mapping):
        raw_minimum = value.get("from", value.get("min"))
        raw_maximum = value.get("to", value.get("max"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
        raw_minimum, raw_maximum = value[0], value[1]
    else:
        raise ConfigError(
            "delay_between_actions должен быть словарём {from, to} или списком из двух чисел"
        )
    minimum = _float(raw_minimum, "delay_between_actions.from")
    maximum = _float(raw_maximum, "delay_between_actions.to")
    if minimum < 0 or maximum < 0:
        raise ConfigError("delay_between_actions не может содержать отрицательные значения")
    if minimum > maximum:
        raise ConfigError("delay_between_actions: значение from больше значения to")
    return DelayRange(minimum=minimum, maximum=maximum)


def _load_decision_criteria(value: Any) -> dict[str, str]:
    """Критерии Decision API: значения по умолчанию, переопределённые из конфига."""
    criteria = default_decision_criteria()
    if value is None:
        return criteria
    if not isinstance(value, Mapping):
        raise ConfigError(
            "llm.decision.criteria должен быть словарём вида "
            f"{{{FIT_LABEL}: '...', {NOT_FIT_LABEL}: '...'}}"
        )
    for label, text in value.items():
        name = str(label).strip()
        if name not in criteria:
            raise ConfigError(
                f"llm.decision.criteria: неизвестный вариант «{name}» "
                f"(допустимо: {FIT_LABEL}, {NOT_FIT_LABEL})"
            )
        criteria[name] = _str(text, f"llm.decision.criteria.{name}")
    return criteria


def _positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Параметр {key} должен быть целым числом")
    if value <= 0:
        raise ConfigError(f"Параметр {key} должен быть больше нуля")
    return value


def _optional_non_negative_int(value: Any, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Параметр {key} должен быть целым числом")
    if value < 0:
        raise ConfigError(f"Параметр {key} не может быть отрицательным")
    return value


def _float(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"Параметр {key} должен быть числом")
    return float(value)


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"Параметр {key} должен быть true или false")


def _str(value: Any, key: str) -> str:
    text = _optional_str(value)
    if not text:
        raise ConfigError(f"Параметр {key} не может быть пустым")
    return text


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
        raise ConfigError(f"{key} должен быть строкой или списком строк")

    values: list[str] = []
    for item in items:
        text = _optional_str(item)
        if not text:
            continue
        if text not in allowed:
            raise ConfigError(
                f"{key}: неизвестное значение «{text}» (допустимо: "
                + ", ".join(sorted(allowed))
                + ")"
            )
        if text not in values:
            values.append(text)
    return tuple(values)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_project_path(value: Any, key: str) -> Path:
    text = _str(value, key)
    path = Path(text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ConfigError(f"{key} должен указывать внутрь каталога проекта: {resolved}")
    return resolved


def _optional_project_path(value: Any, key: str) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return _resolve_project_path(value, key)
