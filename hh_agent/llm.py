"""Два независимых вызова LLM: Decision API (вердикт) и Chat API (письмо)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from .config import LLMConfig
from .models import VacancyDetails
from .profile import Profile
from .prompts import RETRY_JSON_INSTRUCTION, SYSTEM_PROMPT, build_user_prompt

FIT_LABEL = "подходит"
NOT_FIT_LABEL = "не подходит"

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """Ошибка обращения к LLM."""


class LLMResponseError(LLMError):
    """Ответ LLM не соответствует ожидаемому формату."""


@dataclass(frozen=True)
class DecisionResult:
    """Результат Decision-вызова."""

    fits: bool
    probability: float
    threshold: float
    probabilities: dict[str, float]


@dataclass(frozen=True)
class GenerationResult:
    """Результат Generation-вызова."""

    verdict: str
    reason: str
    cover_letter: str


class LLMClient:
    """HTTP-клиент AITUNNEL для обоих вызовов."""

    def __init__(self, config: LLMConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._client = httpx.Client(transport=transport, timeout=config.timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- Decision-вызов -------------------------------------------------

    def decide(self, profile: Profile, vacancy: VacancyDetails) -> DecisionResult:
        """Оценивает вакансию и возвращает вероятность варианта «подходит»."""
        payload = {
            "model": self.config.decision.model,
            "state": {
                "profile": profile.text,
                "vacancy": {
                    "title": vacancy.title,
                    "company": vacancy.company,
                    "salary": vacancy.salary_text,
                    "description": vacancy.description,
                },
            },
            "questions": {
                "fit": {
                    "type": "choice",
                    "instructions": "Подходит ли вакансия профилю пользователя?",
                    "criteria": {
                        FIT_LABEL: (
                            "Требования, обязанности, стек и условия вакансии совпадают "
                            "с профилем не менее чем на 70%, красных флагов нет"
                        ),
                        NOT_FIT_LABEL: (
                            "Совпадение по ключевым требованиям менее 70%, "
                            "другая специализация или есть красные флаги"
                        ),
                    },
                }
            },
        }
        url = _endpoint(self.config.decision.base_url, "decisions")
        data = self._post(url, payload, "Decisions API")
        probabilities = extract_probabilities(data)
        probability = probability_for(probabilities, FIT_LABEL)
        threshold = self.config.decision.threshold
        return DecisionResult(
            fits=probability > threshold,
            probability=probability,
            threshold=threshold,
            probabilities=probabilities,
        )

    # --- Generation-вызов -----------------------------------------------

    def generate(
        self,
        profile: Profile,
        vacancy: VacancyDetails,
        max_letter_length: int = 1500,
    ) -> GenerationResult:
        """Генерирует сопроводительное письмо с валидацией JSON."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(profile, vacancy)},
        ]
        attempts = 2 if self.config.generation.retry_on_invalid_json else 1
        last_error: LLMResponseError | None = None

        for attempt in range(1, attempts + 1):
            content = self._chat(messages)
            try:
                return parse_generation(extract_json_object(content), max_letter_length)
            except LLMResponseError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": RETRY_JSON_INSTRUCTION},
                ]

        assert last_error is not None
        raise last_error

    # --- HTTP -----------------------------------------------------------

    def _chat(self, messages: list[dict[str, str]]) -> str:
        payload: dict[str, Any] = {
            "model": self.config.generation.model,
            "messages": messages,
            "max_tokens": self.config.generation.max_tokens,
            "temperature": self.config.generation.temperature,
        }
        if self.config.generation.json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = _endpoint(self.config.generation.base_url, "chat/completions")
        data = self._post(url, payload, "Chat API")
        return _chat_content(data)

    def _post(self, url: str, payload: Mapping[str, Any], api_name: str) -> dict[str, Any]:
        if not self.config.api_key:
            raise LLMError("Не задан API-ключ LLM: заполните AITUNNEL_API_KEY в .env")

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        attempts = 3
        last_error = f"{api_name}: запрос не выполнен"

        for attempt in range(attempts):
            is_last = attempt == attempts - 1
            try:
                response = self._client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{api_name}: сетевая ошибка — {exc}"
                if is_last:
                    raise LLMError(last_error) from exc
                time.sleep(2 ** attempt)
                continue

            if response.status_code < 400:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise LLMError(
                        f"{api_name}: ответ не является JSON — {_shorten(response.text)}"
                    ) from exc
                if not isinstance(data, dict):
                    raise LLMError(f"{api_name}: неожиданный формат ответа — {_shorten(response.text)}")
                return data

            last_error = f"{api_name}: HTTP {response.status_code} — {_shorten(response.text)}"
            if response.status_code not in _RETRYABLE_STATUSES or is_last:
                raise LLMError(last_error)
            _sleep_before_retry(attempt, response.headers.get("Retry-After"))

        raise LLMError(last_error)


# --- Разбор ответов LLM -------------------------------------------------


def normalize_label(value: Any) -> str:
    """Нормализует подпись для сравнения («ё» → «е», нижний регистр)."""
    return str(value).strip().lower().replace("ё", "е")


def normalize_verdict(value: Any) -> str | None:
    """Приводит вердикт LLM к «подходит» / «не подходит»."""
    if not isinstance(value, str):
        return None
    text = normalize_label(value)
    if "не подходит" in text:
        return NOT_FIT_LABEL
    if "подходит" in text:
        return FIT_LABEL
    if text in {"fit", "fits", "yes", "true", "да"}:
        return FIT_LABEL
    if text in {"not fit", "no", "false", "нет"}:
        return NOT_FIT_LABEL
    return None


def extract_probabilities(payload: Mapping[str, Any], question: str = "fit") -> dict[str, float]:
    """Достаёт вероятности вариантов из ответа Decisions API (устойчиво к формату)."""
    candidates: list[Mapping[str, Any]] = []
    answer = _find_answer(payload, question)

    if isinstance(answer, Mapping):
        probabilities = answer.get("probabilities")
        if isinstance(probabilities, Mapping):
            candidates.append(probabilities)

        value = answer.get("value")
        single = answer.get("probability", answer.get("confidence"))

        if isinstance(value, str) and _is_number(single):
            candidates.append({value: float(single)})  # type: ignore[arg-type]
        if isinstance(value, bool) and _is_number(single):
            probability = float(single)  # type: ignore[arg-type]
            candidates.append(
                {FIT_LABEL: probability, NOT_FIT_LABEL: 1.0 - probability}
                if value
                else {FIT_LABEL: 1.0 - probability, NOT_FIT_LABEL: probability}
            )
        if answer and all(_is_number(item) for item in answer.values()):
            candidates.append(answer)

    if _has_criteria(payload):
        candidates.append(payload)

    for candidate in candidates:
        parsed = _to_probability_map(candidate)
        if parsed:
            return parsed

    raise LLMResponseError(f"В ответе Decisions API нет вероятностей: {_shorten(json.dumps(payload, ensure_ascii=False))}")


def probability_for(probabilities: Mapping[str, float], label: str = FIT_LABEL) -> float:
    """Возвращает вероятность указанного варианта."""
    target = normalize_label(label)
    for key, value in probabilities.items():
        if normalize_label(key) == target:
            return float(value)
    raise LLMResponseError(
        f"В ответе нет вероятности для варианта «{label}»: {sorted(probabilities)}"
    )


def extract_json_object(text: str) -> dict[str, Any]:
    """Извлекает JSON-объект из ответа LLM (в т.ч. из markdown-блока)."""
    if not text or not text.strip():
        raise LLMResponseError("LLM вернул пустой ответ")

    candidates = [match.group("body") for match in _FENCE_RE.finditer(text)]
    candidates.append(text)
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(text[brace_start:brace_end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed

    raise LLMResponseError(f"LLM вернул не JSON: {_shorten(text)}")


def parse_generation(payload: Mapping[str, Any], max_length: int = 1500) -> GenerationResult:
    """Валидирует JSON Generation-вызова."""
    verdict = normalize_verdict(payload.get("verdict"))
    if verdict is None:
        raise LLMResponseError(
            f"В ответе LLM отсутствует корректный verdict: {payload.get('verdict')!r}"
        )

    reason = str(payload.get("reason") or "").strip()
    raw_letter = payload.get("cover_letter") or ""
    cover_letter = raw_letter.strip() if isinstance(raw_letter, str) else str(raw_letter).strip()

    if verdict == NOT_FIT_LABEL:
        cover_letter = ""
    elif cover_letter:
        cover_letter = truncate_cover_letter(cover_letter, max_length)

    return GenerationResult(verdict=verdict, reason=reason, cover_letter=cover_letter)


def truncate_cover_letter(text: str, limit: int = 1500) -> str:
    """Обрезает письмо до limit символов по границе предложения."""
    text = text.strip()
    if len(text) <= limit:
        return text

    window = text[:limit]
    sentence_end = max(
        window.rfind("."),
        window.rfind("!"),
        window.rfind("?"),
        window.rfind("…"),
    )
    if sentence_end >= limit // 3:
        return window[: sentence_end + 1].strip()

    space_index = window.rfind(" ")
    if space_index > 0:
        return window[:space_index].rstrip(" ,;:—-") + "…"
    return window


def _find_answer(payload: Mapping[str, Any], question: str) -> Any:
    answers = payload.get("answers")
    if isinstance(answers, Mapping):
        if question in answers:
            return answers[question]
        if any(key in answers for key in ("probabilities", "probability", "value")):
            return answers
        for value in answers.values():
            if isinstance(value, Mapping) and (
                "probabilities" in value or "probability" in value
            ):
                return value
    for key in ("result", "output"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return payload.get(question)


def _has_criteria(payload: Mapping[str, Any]) -> bool:
    normalized = {normalize_label(key) for key in payload}
    return normalize_label(FIT_LABEL) in normalized


def _to_probability_map(raw: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        result[str(key)] = float(value)
    return result


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _chat_content(data: Mapping[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError(f"Chat API вернул ответ без choices: {_shorten(json.dumps(data, ensure_ascii=False))}")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping) and part.get("text")
        ]
        return "".join(parts)
    raise LLMResponseError("Chat API вернул ответ без текста")


def _endpoint(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _sleep_before_retry(attempt: int, retry_after: str | None) -> None:
    delay = float(2 ** attempt)
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            pass
    time.sleep(min(delay, 30.0))


def _shorten(text: str, limit: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"
