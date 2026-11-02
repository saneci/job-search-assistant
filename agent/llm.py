"""Два независимых вызова LLM: Decision API (вердикт) и Chat API (письмо)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from .config import FIT_LABEL, LLMConfig, NOT_FIT_LABEL, default_decision_criteria
from .models import VacancyDetails
from .profile import Profile
from .prompts import (
    REJECTION_MAX_LENGTH,
    REJECTION_MAX_TOKENS,
    REJECTION_SYSTEM_PROMPT,
    RETRY_JSON_INSTRUCTION,
    build_rejection_prompt,
    build_user_prompt,
    detect_required_opening,
    requires_project_links,
    load_system_prompt,
)

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
    chosen: str


def meets_threshold(probability: float, threshold: float) -> bool:
    """Вероятность должна быть строго выше порога: ровно порог — пропуск."""
    return probability > threshold


@dataclass(frozen=True)
class GenerationResult:
    """Результат Generation-вызова."""

    verdict: str
    reason: str
    cover_letter: str
    greeting_added: bool = False
    # Обязательное начало письма из вакансии («3PL»), если оно есть.
    required_opening: str = ""


class LLMClient:
    """HTTP-клиент AITUNNEL для обоих вызовов."""

    def __init__(self, config: LLMConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._system_prompt = load_system_prompt(config.system_prompt_path)
        self._client = httpx.Client(transport=transport, timeout=config.timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- Decision-вызов -------------------------------------------------

    def decide(self, profile: Profile, vacancy: VacancyDetails) -> DecisionResult:
        """Оценивает вакансию и возвращает вероятность варианта «подходит»."""
        criteria = self.config.decision.criteria or default_decision_criteria()
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
                    "criteria": dict(criteria),
                }
            },
        }
        url = _endpoint(self.config.base_url, "decisions")
        data = self._post(url, payload, "Decisions API")
        probabilities = extract_probabilities(data)
        probability = probability_for(probabilities, FIT_LABEL)
        threshold = self.config.decision.threshold
        chosen = max(probabilities, key=lambda label: probabilities[label])
        return DecisionResult(
            fits=meets_threshold(probability, threshold),
            probability=probability,
            threshold=threshold,
            probabilities=probabilities,
            chosen=chosen,
        )

    # --- Generation-вызов -----------------------------------------------

    def generate(
        self,
        profile: Profile,
        vacancy: VacancyDetails,
        max_letter_length: int = 1500,
        signature: str = "",
    ) -> GenerationResult:
        """Генерирует сопроводительное письмо с валидацией JSON."""
        opening = detect_required_opening(vacancy)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": build_user_prompt(
                profile,
                vacancy,
                opening,
                self.config.cover_letter.pet_projects,
                requires_project_links(vacancy),
            )},
        ]
        attempts = 2 if self.config.generation.retry_on_invalid_json else 1
        last_error: LLMResponseError | None = None

        for attempt in range(1, attempts + 1):
            content = self._chat(messages)
            try:
                return parse_generation(
                    extract_json_object(content), max_letter_length, signature, opening=opening
                )
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

    def explain_rejection(
        self,
        profile: Profile,
        vacancy: VacancyDetails,
        probability: float,
        threshold: float,
    ) -> str:
        """Короткое объяснение отказа: один дешёвый Chat-запрос (до 120 токенов)."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": REJECTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_rejection_prompt(profile, vacancy, probability, threshold),
            },
        ]
        content = self._chat(messages, max_tokens=REJECTION_MAX_TOKENS, temperature=0.0)
        return _one_line(content, REJECTION_MAX_LENGTH)

    # --- HTTP -----------------------------------------------------------

    def _chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.config.generation.model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.generation.max_tokens,
            "temperature": (
                self.config.generation.temperature if temperature is None else temperature
            ),
        }
        if self.config.generation.json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = _endpoint(self.config.base_url, "chat/completions")
        data = self._post(url, payload, "Chat API")
        return _chat_content(data)

    def _post(self, url: str, payload: Mapping[str, Any], api_name: str) -> dict[str, Any]:
        api_key = self.config.api_key.get_secret_value()
        if not api_key:
            raise LLMError("Не задан API-ключ LLM: заполните AITUNNEL_API_KEY в .env")

        headers = {
            "Authorization": f"Bearer {api_key}",
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
        # Официальный формат AITUNNEL: answers.<name>.probabilities по ключам вариантов.
        probabilities = answer.get("probabilities")
        if isinstance(probabilities, Mapping):
            candidates.append(probabilities)

        # choice-ответ: answers.<name>.choice = выбранный ключ, confidence — его вероятность.
        choice = answer.get("choice")
        confidence = answer.get("confidence", answer.get("probability"))
        if isinstance(choice, str) and _is_number(confidence):
            candidates.append({choice: float(confidence)})  # type: ignore[arg-type]

        # noul-ответ: answers.<name>.noul — вероятность «да» (0…1).
        noul = answer.get("noul")
        if _is_number(noul):
            probability = float(noul)  # type: ignore[arg-type]
            candidates.append({FIT_LABEL: probability, NOT_FIT_LABEL: 1.0 - probability})

        # Альтернативный формат: value + probability/confidence.
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


def parse_generation(
    payload: Mapping[str, Any],
    max_length: int = 1500,
    signature: str = "",
    opening: str = "",
) -> GenerationResult:
    """Валидирует JSON Generation-вызова.

    `opening` — обязательное начало письма из вакансии: тогда оно сохраняется
    первым, а приветствие встаёт сразу после него.
    """
    verdict = normalize_verdict(payload.get("verdict"))
    if verdict is None:
        raise LLMResponseError(
            f"В ответе LLM отсутствует корректный verdict: {payload.get('verdict')!r}"
        )

    reason = str(payload.get("reason") or "").strip()
    raw_letter = payload.get("cover_letter") or ""
    cover_letter = raw_letter.strip() if isinstance(raw_letter, str) else str(raw_letter).strip()
    greeting_added = False

    if verdict == NOT_FIT_LABEL:
        cover_letter = ""
    elif cover_letter:
        cover_letter, greeting_added = ensure_greeting(cover_letter, opening)
        cover_letter = add_signature(cover_letter, signature, max_length)

    return GenerationResult(
        verdict=verdict,
        reason=reason,
        cover_letter=cover_letter,
        greeting_added=greeting_added,
        required_opening=opening.strip(),
    )


SIGNATURE_SEPARATOR = "\n\n"
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_TELEGRAM_RE = re.compile(r"@[A-Za-z][\w_]{2,}")
_GREETING_RE = re.compile(
    r"^\s*(?:здравствуйте|добрый день|добрый вечер|приветствую)\b[\s!.,]*",
    re.IGNORECASE,
)
# Обрывок в начале письма вроде «3PL.»: короткое слово/аббревиатура с точкой.
_SHORT_LEAD_RE = re.compile(r"^[A-ZА-ЯЁ0-9][\w+-]{0,5}\.\s+")


def ensure_greeting(text: str, opening: str = "") -> tuple[str, bool]:
    """Гарантирует приветствие, не ломая обязательное начало письма.

    Если вакансия требует начать с конкретного слова («Начните сопроводительное
    письмо со слова 3PL»), это слово остаётся первым, а приветствие встаёт сразу
    после него: «3PL. Здравствуйте! Меня заинтересовала вакансия». Иначе
    приветствие добавляется в начало, а случайный обрывок вроде «3PL.» срезается.

    Возвращает (текст, было ли приветствие добавлено).
    """
    text = (text or "").strip()
    if not text:
        return "", False

    opening = (opening or "").strip()
    if opening:
        return _greeting_after_opening(text, opening)

    if _GREETING_RE.match(text):
        return text, False
    body = _SHORT_LEAD_RE.sub("", text, count=1).strip() or text
    return f"Здравствуйте! {body}", True


def _greeting_after_opening(text: str, opening: str) -> tuple[str, bool]:
    """Ставит приветствие сразу после обязательного начала, сохраняя его дословно."""
    end = _opening_end(text, opening)
    if end is None:
        # Модель проигнорировала требование: ставим слово первым, приветствие за ним.
        rest = _drop_opening(_without_greeting(text), opening) or _without_greeting(text)
        return f"{opening}. Здравствуйте! {_capitalized(rest)}".strip(), True

    head = opening + text[len(opening) : end].rstrip()
    rest = text[end:].lstrip()
    if _GREETING_RE.match(rest):
        return text, False

    head = head.rstrip(" \t.!?:;,—-–")
    if not rest:
        return f"{head}. Здравствуйте!", True
    return f"{head}. Здравствуйте! {_capitalized(rest)}", True


def _opening_end(text: str, opening: str) -> int | None:
    """Индекс сразу после обязательного начала и следующей за ним пунктуации."""
    if text[: len(opening)].lower() != opening.lower():
        return None
    end = len(opening)
    if end < len(text) and (text[end].isalnum() or text[end] == "_"):
        return None
    while end < len(text) and text[end] in " \t.!?:;,—-–":
        end += 1
    return end


def _without_greeting(text: str) -> str:
    return _GREETING_RE.sub("", text, count=1).strip()


def _drop_opening(text: str, opening: str) -> str:
    end = _opening_end(text, opening)
    return "" if end is None else text[end:].strip()


def _capitalized(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def add_signature(text: str, signature: str, limit: int = 1500) -> str:
    """Добавляет подпись с контактами через пустую строку, не превышая limit.

    Подпись задаётся в config.local.yaml (llm.cover_letter.signature). Если
    модель уже добавила контакты в письмо, повторно они не дублируются.
    """
    text = (text or "").strip()
    signature = (signature or "").strip()
    if not text:
        return ""
    if not signature:
        return truncate_cover_letter(text, limit)
    if _signature_present(text, signature):
        return truncate_cover_letter(text, limit)

    body_limit = limit - len(signature) - len(SIGNATURE_SEPARATOR)
    if body_limit <= 0:
        # Подпись занимает весь лимит — письмо всё равно уходит с контактами.
        return truncate_cover_letter(signature, limit)

    body = truncate_cover_letter(text, body_limit)
    if not body:
        return truncate_cover_letter(signature, limit)
    return f"{body}{SIGNATURE_SEPARATOR}{signature}"


def _signature_present(text: str, signature: str) -> bool:
    """Проверяет, что подпись (или её контакты) уже есть в тексте письма."""
    lowered_text = " ".join(text.lower().split())
    lowered_signature = " ".join(signature.lower().split())
    if lowered_signature and lowered_signature in lowered_text:
        return True
    contacts = _EMAIL_RE.findall(signature) + _TELEGRAM_RE.findall(signature)
    return bool(contacts) and all(contact.lower() in lowered_text for contact in contacts)


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
        if any(key in answers for key in ("probabilities", "probability", "choice", "noul", "value")):
            return answers
        for value in answers.values():
            if isinstance(value, Mapping) and (
                "probabilities" in value or "probability" in value or "choice" in value
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


def _one_line(text: str, limit: int) -> str:
    """Схлопывает ответ модели в одну строку и ограничивает длину."""
    line = " ".join((text or "").split())
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip(" ,;:-") + "…"
