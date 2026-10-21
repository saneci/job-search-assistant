"""Тесты LLM-слоя: разбор ответов, порог решения, ретрай на невалидном JSON."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest

from hh_agent.config import DecisionConfig, GenerationConfig, LLMConfig
from hh_agent.llm import (
    FIT_LABEL,
    LLMClient,
    LLMError,
    LLMResponseError,
    extract_json_object,
    extract_probabilities,
    normalize_verdict,
    parse_generation,
    probability_for,
    truncate_cover_letter,
)
from hh_agent.models import VacancyDetails
from hh_agent.profile import Profile

PROFILE = Profile(text="Java-разработчик", examples="- пример письма", style_rules="Коротко и по делу")
VACANCY = VacancyDetails(
    url="https://hh.ru/vacancy/1",
    title="Java-разработчик",
    company="Ozon",
    salary="200 000 ₽",
    description="Spring Boot, Kafka, PostgreSQL.",
)


def make_config(*, threshold: float = 0.8, retry: bool = True) -> LLMConfig:
    return LLMConfig(
        api_key="sk-aitunnel-test",
        timeout=5.0,
        decision=DecisionConfig(
            base_url="https://api.aitunnel.ru/v1",
            model="gpt-6-luna-decisions",
            threshold=threshold,
        ),
        generation=GenerationConfig(
            base_url="https://api.aitunnel.ru/v1",
            model="gpt-6-luna-pro",
            max_tokens=1000,
            temperature=0.3,
            json_mode=False,
            retry_on_invalid_json=retry,
        ),
    )


def make_client(handler: Any, **config_kwargs: Any) -> LLMClient:
    return LLMClient(make_config(**config_kwargs), transport=httpx.MockTransport(handler))


def chat_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_decide_sends_expected_request_and_reads_probabilities() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    "fit": {
                        "type": "choice",
                        "value": "подходит",
                        "probabilities": {"подходит": 0.87, "не подходит": 0.13},
                        "confidence": 0.87,
                    }
                }
            },
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert captured["url"] == "https://api.aitunnel.ru/v1/decisions"
    assert captured["auth"] == "Bearer sk-aitunnel-test"
    assert captured["payload"]["model"] == "gpt-6-luna-decisions"
    assert captured["payload"]["questions"]["fit"]["criteria"].keys() == {"подходит", "не подходит"}
    assert captured["payload"]["state"]["vacancy"]["title"] == "Java-разработчик"
    assert result.fits is True
    assert result.probability == pytest.approx(0.87)
    assert result.threshold == pytest.approx(0.8)


def test_decide_is_strict_at_threshold() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"подходит": 0.8, "не подходит": 0.2})

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.fits is False


def test_decide_accepts_noul_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"answers": {"fit": {"type": "noul", "value": True, "probability": 0.95}}},
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.probability == pytest.approx(0.95)
    assert result.fits is True


def test_decide_without_api_key_raises() -> None:
    config = dataclasses.replace(make_config(), api_key="")
    client = LLMClient(config, transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    with pytest.raises(LLMError, match="AITUNNEL_API_KEY"):
        client.decide(PROFILE, VACANCY)
    client.close()


def test_extract_probabilities_requires_known_format() -> None:
    with pytest.raises(LLMResponseError):
        extract_probabilities({"answers": {"fit": {"type": "choice", "value": "подходит"}}})


def test_probability_for_unknown_label() -> None:
    with pytest.raises(LLMResponseError):
        probability_for({"подходит": 0.9}, "может быть")


def test_generate_parses_json_and_truncates_letter() -> None:
    long_letter = "Первое предложение. " * 200
    content = (
        "```json\n"
        + json.dumps(
            {
                "verdict": "подходит",
                "reason": "Стек совпадает",
                "cover_letter": long_letter,
            },
            ensure_ascii=False,
        )
        + "\n```"
    )

    with make_client(lambda request: chat_response(content)) as client:
        result = client.generate(PROFILE, VACANCY, max_letter_length=1500)

    assert result.verdict == FIT_LABEL
    assert result.cover_letter.endswith(".")
    assert len(result.cover_letter) <= 1500


def test_generate_retries_once_on_invalid_json() -> None:
    calls: list[str] = []
    good_content = json.dumps(
        {"verdict": "подходит", "reason": "ok", "cover_letter": "Здравствуйте!"},
        ensure_ascii=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["messages"][-1]["content"])
        if len(calls) == 1:
            return chat_response("Извините, не могу ответить в JSON")
        return chat_response(good_content)

    with make_client(handler) as client:
        result = client.generate(PROFILE, VACANCY)

    assert len(calls) == 2
    assert "не является корректным JSON" in calls[1]
    assert result.cover_letter == "Здравствуйте!"


def test_generate_fails_after_second_invalid_json() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return chat_response("не json")

    with make_client(handler) as client:
        with pytest.raises(LLMResponseError):
            client.generate(PROFILE, VACANCY)

    assert len(calls) == 2


def test_generate_without_retry_makes_one_call() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return chat_response("не json")

    with make_client(handler, retry=False) as client:
        with pytest.raises(LLMResponseError):
            client.generate(PROFILE, VACANCY)

    assert len(calls) == 1


def test_generate_empty_letter_for_not_fit() -> None:
    result = parse_generation(
        {"verdict": "не подходит", "reason": "другой стек", "cover_letter": "не нужно"}
    )

    assert result.cover_letter == ""


def test_extract_json_object_variants() -> None:
    assert extract_json_object('{"verdict": "подходит"}') == {"verdict": "подходит"}
    assert extract_json_object('Ответ: {"verdict": "подходит"} — всё') == {"verdict": "подходит"}
    assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}

    with pytest.raises(LLMResponseError):
        extract_json_object("")


def test_normalize_verdict() -> None:
    assert normalize_verdict("Подходит") == FIT_LABEL
    assert normalize_verdict("НЕ ПОДХОДИТ") == "не подходит"
    assert normalize_verdict("fits") == FIT_LABEL
    assert normalize_verdict("") is None
    assert normalize_verdict(None) is None


def test_truncate_cover_letter_by_sentence() -> None:
    text = "Первое предложение. Второе предложение. " + "х" * 2000

    result = truncate_cover_letter(text, 60)

    assert result == "Первое предложение. Второе предложение."


def test_truncate_cover_letter_short_text_unchanged() -> None:
    assert truncate_cover_letter("Короткое письмо.", 100) == "Короткое письмо."


def test_parse_generation_requires_verdict() -> None:
    with pytest.raises(LLMResponseError):
        parse_generation({"reason": "нет вердикта"})
