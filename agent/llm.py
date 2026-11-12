"""Два независимых вызова LLM: Decision API (вердикт) и Chat API (письмо)."""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from .config import (
    FIT_LABEL,
    LLMConfig,
    NOT_FIT_LABEL,
    default_decision_questions,
)
from .models import VacancyDetails
from .profile import Profile
from .prompts import (
    DESCRIPTION_IMAGES_MAX_TOKENS,
    DESCRIPTION_IMAGES_PROMPT,
    REJECTION_MAX_LENGTH,
    REJECTION_MAX_TOKENS,
    REJECTION_SYSTEM_PROMPT,
    RETRY_JSON_INSTRUCTION,
    build_rejection_prompt,
    build_user_prompt,
    detect_required_opening,
    profile_facts,
    requires_project_links,
    split_requirements,
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
    # Уверенность по каждому вопросу: «да» значит «проблем нет».
    checks: dict[str, float] = field(default_factory=dict)
    # Вердикт человеческим языком: модель решений объяснений не даёт, текст собирает код.
    reason: str = ""


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
    # Ответы на вопросы анкеты или теста: номер вопроса с единицы и текст ответа.
    answers: dict[int, str] = field(default_factory=dict)


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
        """Оценивает вакансию четырьмя вопросами и складывает ответы в одну оценку.

        Вопросы сформулированы так, что «да» значит «проблем нет», поэтому
        уверенность каждого ответа входит в оценку с весом из конфигурации.
        """
        questions = self.config.decision.questions or default_decision_questions()
        requirements = split_requirements(vacancy.description)
        vacancy_state: dict[str, Any] = {
            "title": vacancy.title,
            "company": vacancy.company,
            "salary": vacancy.salary_text,
            "description": vacancy.description,
        }
        if requirements.mandatory:
            vacancy_state["requirements"] = requirements.mandatory
        if requirements.optional:
            vacancy_state["optional"] = requirements.optional
        payload = {
            "model": self.config.decision.model,
            "state": {
                "profile": {"facts": profile_facts(profile), "text": profile.text},
                "vacancy": vacancy_state,
            },
            "questions": {
                name: {
                    "type": "noul",
                    "instructions": question.instructions,
                    "criteria": dict(question.criteria),
                }
                for name, question in questions.items()
            },
        }
        url = _endpoint(self.config.base_url, "decisions")
        data = self._post(url, payload, "Decisions API")
        checks = {name: extract_truth_probability(data, name) for name in questions}
        weights = {name: question.weight for name, question in questions.items()}
        score = weighted_score(checks, weights)
        threshold = self.config.decision.threshold
        probabilities = {
            FIT_LABEL: score,
            NOT_FIT_LABEL: round(1.0 - score, 4),
        }
        return DecisionResult(
            fits=meets_threshold(score, threshold),
            probability=score,
            threshold=threshold,
            probabilities=probabilities,
            chosen=max(probabilities, key=probabilities.__getitem__),
            checks=checks,
            reason=decision_reason(score, threshold),
        )

    # --- Generation-вызов -----------------------------------------------

    def generate(
        self,
        profile: Profile,
        vacancy: VacancyDetails,
        max_letter_length: int = 1500,
        signature: str = "",
        questionnaire: str = "",
    ) -> GenerationResult:
        """Генерирует сопроводительное письмо и ответы на вопросы анкеты.

        `questionnaire` — уже пронумерованные вопросы формы отклика или теста.
        Ответы приходят тем же вызовом: один запрос вместо двух.
        """
        opening = detect_required_opening(vacancy)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": build_user_prompt(
                profile,
                vacancy,
                opening,
                self.config.cover_letter.pet_projects,
                requires_project_links(vacancy),
                questionnaire,
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

    def transcribe_images(self, images: Sequence[bytes]) -> str:
        """Переписывает текст вакансии с картинок: у работодателя описание может быть изображениями.

        Провайдер принимает изображения внутри JSON, поэтому каждая картинка уходит
        data URL с base64, а не ссылкой на CDN.
        """
        parts: list[dict[str, Any]] = [
            {"type": "text", "text": DESCRIPTION_IMAGES_PROMPT}
        ]
        for data in images:
            parts.append({"type": "image_url", "image_url": {"url": _image_data_url(data)}})
        content = self._chat(
            [{"role": "user", "content": parts}],
            max_tokens=DESCRIPTION_IMAGES_MAX_TOKENS,
            temperature=0.0,
            model=self.config.ocr.model or self.config.generation.model,
        )
        return content.strip()

    # --- HTTP -----------------------------------------------------------

    def _chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.config.generation.model,
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


def extract_truth_probability(payload: Mapping[str, Any], question: str) -> float:
    """Вероятность «да» для вопроса noul (устойчиво к формату ответа)."""
    answer = _find_answer(payload, question)
    if _is_number(answer):
        return _clamp_probability(float(answer))
    if isinstance(answer, Mapping):
        value = answer.get("noul", answer.get("value"))
        if _is_number(value):
            return _clamp_probability(float(value))
        probabilities = answer.get("probabilities")
        if isinstance(probabilities, Mapping):
            parsed = _to_probability_map(probabilities)
            for key, number in parsed.items():
                if normalize_label(key) in ("true", "да", "yes"):
                    return _clamp_probability(number)
            if len(parsed) == 1:
                return _clamp_probability(next(iter(parsed.values())))
        flat = _to_probability_map(answer)
        if "true" in flat:
            return _clamp_probability(flat["true"])
    raise LLMResponseError(
        f"В ответе Decisions API нет вероятности для вопроса «{question}»: "
        f"{_shorten(json.dumps(payload, ensure_ascii=False))}"
    )


def weighted_score(
    checks: Mapping[str, float], weights: Mapping[str, float]
) -> float:
    """Оценка соответствия: сумма уверенностей по вопросам с весами.

    Вопросы сформулированы так, что «да» значит «проблем нет», поэтому
    уверенность входит в оценку напрямую, без инверсии.
    """
    total = sum(
        _clamp_probability(checks.get(name, 0.0)) * weight
        for name, weight in weights.items()
    )
    return round(total, 4)


def decision_reason(probability: float, threshold: float) -> str:
    """Вердикт Decision человеческим языком: оценка соответствия и требование.

    Названия проверок и их вероятности в отчёт не попадают: в окне деталей
    нужен итог, а сами ответы остаются в журнале событием `decision`.
    """
    percent = f"{probability:.0%}"
    requirement = f"{threshold:.0%}"
    if probability > threshold:
        return f"Вакансия подходит на {percent}, это выше требования {requirement}"
    if probability == threshold:
        return f"Вакансия подходит на {percent}, но это не выше требования {requirement}"
    return f"Вакансия подходит на {percent}, но это ниже требования {requirement}"


def _clamp_probability(value: float) -> float:
    """Вероятность в диапазоне 0…1: провайдер иногда отдаёт значения за границей."""
    return min(max(value, 0.0), 1.0)


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
        answers=parse_answers(payload.get("answers")),
    )


def parse_answers(raw: Any) -> dict[int, str]:
    """Ответы на вопросы анкеты: список `{"id": 1, "answer": "Да"}` или объект.

    Номер вопроса с единицы. Ответ-пустышка и множественный выбор отбрасываются:
    проставить в форму можно только один вариант, а угадывать нельзя.
    """
    collected: dict[int, str] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            number = _question_number(key)
            text = _answer_text(value)
            if number and text:
                collected[number] = text
        return collected
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return collected
    for position, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            continue
        number = _question_number(
            item.get("id", item.get("question_id", item.get("number", position)))
        )
        text = _answer_text(item.get("answer", item.get("value", item.get("text"))))
        if number and text:
            collected[number] = text
    return collected


def _question_number(value: Any) -> int | None:
    """Номер вопроса из ответа модели: целое положительное число."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _answer_text(value: Any) -> str:
    """Текст ответа модели: число, строка или один элемент списка."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Sequence) and len(value) == 1:
        return _answer_text(value[0])
    return ""


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


def _image_data_url(data: bytes) -> str:
    """Data URL для картинки: изображения провайдер принимает только внутри JSON."""
    return f"data:{_image_mime(data)};base64," + base64.b64encode(data).decode("ascii")


def _image_mime(data: bytes) -> str:
    """Тип картинки по сигнатуре: провайдеру нужен точный MIME, иначе запрос отклоняется."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


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
