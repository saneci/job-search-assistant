"""Тесты LLM-слоя: разбор ответов, порог решения, ретрай на невалидном JSON."""

from __future__ import annotations

import base64
import dataclasses
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agent.config import (
    PROJECT_ROOT,
    CoverLetterConfig,
    DecisionConfig,
    GenerationConfig,
    LLMConfig,
)
from agent.llm import (
    FIT_LABEL,
    LLMClient,
    LLMError,
    LLMResponseError,
    add_signature,
    decision_reason,
    ensure_greeting,
    extract_json_object,
    extract_probabilities,
    meets_threshold,
    normalize_verdict,
    parse_generation,
    probability_for,
    truncate_cover_letter,
)
from agent.models import VacancyDetails
from agent.profile import Profile
from agent.prompts import (
    FALLBACK_SYSTEM_PROMPT,
    REJECTION_MAX_LENGTH,
    REJECTION_MAX_TOKENS,
    load_system_prompt,
)

PROFILE = Profile(text="Java-разработчик", examples="- пример письма", style_rules="Коротко и по делу")
PNG_IMAGE = b"\x89PNG\r\n\x1a\n" + b"png-payload"
JPEG_IMAGE = b"\xff\xd8\xff\xe0" + b"jpeg-payload"
VACANCY = VacancyDetails(
    url="https://hh.ru/vacancy/1",
    title="Java-разработчик",
    company="Ozon",
    salary="200 000 ₽",
    description="Spring Boot, Kafka, PostgreSQL.",
)

# Вакансия с явными блоками: проверяет, что требования и «будет плюсом»
# попадают в запрос решения отдельными полями.
DECISION_VACANCY = VacancyDetails(
    url="https://hh.ru/vacancy/2",
    title="Java-разработчик",
    company="Ozon",
    salary="200 000 ₽",
    description=(
        "Обязанности\nРазработка сервисов\n\n"
        "Требования\nJava, Spring Boot, PostgreSQL\n\n"
        "Будет плюсом\nKafka"
    ),
)

OPTIONAL_STACK_VACANCY = VacancyDetails(
    url="https://hh.ru/vacancy/139411862",
    title="Backend-разработчик",
    company="ООО Агенторика",
    salary="от 120 000 до 150 000 ₽",
    description=(
        "Чем предстоит заниматься:\n"
        "Разработка нового API и проектирование микросервисной архитектуры.\n\n"
        "Наши ожидания:\n"
        "Опыт коммерческой разработки на Java от 1 года.\n"
        "Уверенное знание Spring (Boot, MVC, Data).\n"
        "Опыт работы с реляционными базами данных (SQL).\n"
        "Владение инструментами контроля версий (Git).\n"
        "Опыт работы с брокерами сообщений (Kafka).\n"
        "Понимание принципов работы REST API и микросервисной архитектуры.\n\n"
        "Будет плюсом:\n"
        "Опыт работы с Elasticsearch.\n"
        "Знание Docker.\n"
        "Уверенное владение Linux CLI."
    ),
)


def make_config(
    *,
    threshold: float = 0.8,
    retry: bool = True,
    system_prompt_path: Path | None = None,
    pet_projects: tuple[str, ...] = (),
) -> LLMConfig:
    return LLMConfig(
        api_key="sk-aitunnel-test",
        base_url="https://api.aitunnel.ru/v1",
        timeout_seconds=5.0,
        decision=DecisionConfig(
            model="gpt-6-luna-decisions",
            threshold=threshold,
        ),
        generation=GenerationConfig(
            model="gpt-6-luna-pro",
            max_tokens=1000,
            temperature=0.3,
            json_mode=False,
            retry_on_invalid_json=retry,
        ),
        system_prompt_path=system_prompt_path,
        cover_letter=CoverLetterConfig(pet_projects=pet_projects),
    )


def make_client(handler: Any, **config_kwargs: Any) -> LLMClient:
    return LLMClient(make_config(**config_kwargs), transport=httpx.MockTransport(handler))


def chat_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_transcribe_images_sends_images_as_data_urls() -> None:
    """Картинки вакансии уходят в Chat API как data URL, ответ становится текстом описания."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return chat_response("ЧТО НУЖНО БУДЕТ ДЕЛАТЬ\nРазработка сервисов на Java")

    with make_client(handler) as client:
        text = client.transcribe_images([PNG_IMAGE, JPEG_IMAGE])

    payload = captured["payload"]
    content = payload["messages"][0]["content"]
    assert payload["model"] == "gpt-6-luna-pro"
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(PNG_IMAGE).decode("ascii")
    )
    assert content[2]["image_url"]["url"] == (
        "data:image/jpeg;base64," + base64.b64encode(JPEG_IMAGE).decode("ascii")
    )
    assert text == "ЧТО НУЖНО БУДЕТ ДЕЛАТЬ\nРазработка сервисов на Java"


def test_meets_threshold_is_strict() -> None:
    """Ровно порог — не выше порога, поэтому вакансия пропускается."""
    assert meets_threshold(0.8, 0.8) is False
    assert meets_threshold(0.81, 0.8) is True


def decision_response(**checks: float) -> httpx.Response:
    """Ответ Decisions API: по одному noul на каждый вопрос."""
    values = {
        "mandatory_match": 0.98,
        "experience_match": 0.97,
        "stack_match": 0.95,
        "red_flags": 0.99,
    }
    values.update(checks)
    return httpx.Response(
        200,
        json={
            "answers": {
                name: {"type": "noul", "noul": value}
                for name, value in values.items()
            }
        },
    )


def test_decide_sends_questions_and_reads_noul_answers() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        captured["auth"] = request.headers["authorization"]
        return decision_response(
            stack_match=0.9,
            mandatory_match=0.9,
            experience_match=0.9,
            red_flags=0.9,
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, DECISION_VACANCY)

    payload = captured["payload"]
    assert captured["url"] == "https://api.aitunnel.ru/v1/decisions"
    assert captured["auth"] == "Bearer sk-aitunnel-test"
    assert payload["model"] == "gpt-6-luna-decisions"
    assert sorted(payload["questions"]) == [
        "experience_match",
        "mandatory_match",
        "red_flags",
        "stack_match",
    ]
    assert all(item["type"] == "noul" for item in payload["questions"].values())
    assert payload["state"]["profile"]["text"] == "Java-разработчик"
    assert "не доказывает отсутствие опыта" in payload["state"]["profile"]["facts"]
    assert "более высокий подтверждённый уровень" in payload["state"]["profile"]["facts"]
    vacancy = payload["state"]["vacancy"]
    assert vacancy["title"] == "Java-разработчик"
    assert vacancy["requirements"] == "Java, Spring Boot, PostgreSQL"
    assert vacancy["optional"] == "Kafka"
    assert result.checks["mandatory_match"] == pytest.approx(0.9)
    assert result.probability == pytest.approx(0.9)
    assert result.fits is True
    assert result.reason == "Вакансия подходит на 90%, это выше требования 80%"


def test_stack_question_ignores_optional_technologies() -> None:
    """Плюсы вакансии не входят в stack_match: вопрос только об обязательных технологиях."""

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return decision_response()

    with make_client(handler) as client:
        client.decide(PROFILE, OPTIONAL_STACK_VACANCY)

    vacancy = captured["payload"]["state"]["vacancy"]
    assert "Elasticsearch" in vacancy["optional"]
    assert "Elasticsearch" not in vacancy["requirements"]

    stack = captured["payload"]["questions"]["stack_match"]
    text = f"{stack['instructions']} {stack['criteria']['true']}"
    assert "обязательн" in text.casefold()
    assert "не влияют" in text.casefold()
    assert "более высокого" in text.casefold()


def test_decide_is_strict_at_threshold() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return decision_response(
            stack_match=0.5,
            mandatory_match=1.0,
            experience_match=1.0,
            red_flags=1.0,
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.probability == pytest.approx(0.8)
    assert result.fits is False


@pytest.mark.parametrize(
    ("probability", "expected"),
    [
        (0.21, "Вакансия подходит на 21%, но это ниже требования 80%"),
        (0.8, "Вакансия подходит на 80%, но это не выше требования 80%"),
        (0.81, "Вакансия подходит на 81%, это выше требования 80%"),
    ],
)
def test_decision_reason_speaks_percent_and_requirement(
    probability: float, expected: str
) -> None:
    """В отчёт идёт фраза про проценты, а не служебные названия проверок."""
    assert decision_reason(probability, 0.8) == expected


def test_decide_missing_mandatory_caps_score_at_seventy_percent() -> None:
    """Неподтверждённые обязательные требования не дают оценке подняться выше 70%."""

    def handler(request: httpx.Request) -> httpx.Response:
        return decision_response(
            stack_match=1.0,
            mandatory_match=0.0,
            experience_match=1.0,
            red_flags=1.0,
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.checks["mandatory_match"] == pytest.approx(0.0)
    assert result.probability == pytest.approx(0.7)
    assert result.fits is False


def test_decide_missing_stack_caps_score_at_sixty_percent() -> None:
    """Неподтверждённый стек не даёт оценке подняться выше 60%."""

    def handler(request: httpx.Request) -> httpx.Response:
        return decision_response(
            stack_match=0.0,
            mandatory_match=1.0,
            experience_match=1.0,
            red_flags=1.0,
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.checks["stack_match"] == pytest.approx(0.0)
    assert result.probability == pytest.approx(0.6)
    assert result.fits is False


def test_decide_small_experience_gap_is_not_blocker() -> None:
    """«От 3 лет» при 2.5 года: разница меньше двух лет не блокирует отклик."""

    def handler(request: httpx.Request) -> httpx.Response:
        return decision_response(
            stack_match=1.0,
            mandatory_match=1.0,
            experience_match=0.3,
            red_flags=1.0,
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.probability == pytest.approx(0.895)
    assert result.fits is True


def test_decide_low_stack_is_rejected() -> None:
    """Требование «от 1 года» не спасает вакансию с совпадением стека ниже порога."""

    def handler(request: httpx.Request) -> httpx.Response:
        return decision_response(
            stack_match=0.4,
            mandatory_match=1.0,
            experience_match=1.0,
            red_flags=1.0,
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.probability == pytest.approx(0.76)
    assert result.fits is False


def test_decide_parses_flat_answers() -> None:
    """Плоский ответ провайдера: имя вопроса → вероятность «да»."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "mandatory_match": 0.9,
                "experience_match": 0.9,
                "stack_match": 0.9,
                "red_flags": 0.9,
            },
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.probability == pytest.approx(0.9)
    assert result.fits is True


def test_decide_all_good_answers_give_one_hundred_percent() -> None:
    """Если все четыре вопроса хорошие, оценка равна 100%."""

    def handler(request: httpx.Request) -> httpx.Response:
        return decision_response(
            stack_match=1.0,
            mandatory_match=1.0,
            experience_match=1.0,
            red_flags=1.0,
        )

    with make_client(handler) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.probability == pytest.approx(1.0)
    assert result.fits is True
    assert result.reason == "Вакансия подходит на 100%, это выше требования 80%"


def test_decide_requires_every_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"answers": {"stack_match": {"type": "noul", "noul": 0.9}}},
        )

    with make_client(handler) as client:
        with pytest.raises(LLMResponseError, match="mandatory_match"):
            client.decide(PROFILE, VACANCY)


def test_decide_without_api_key_raises() -> None:
    config = make_config().model_copy(update={"api_key": SecretStr("")})
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


SIGNATURE = "С уважением,\nАлександр Гладской\nТелеграм: @saneci\nПочта: a.gladskoy@gmail.com"


def test_add_signature_appends_contacts_after_blank_line() -> None:
    letter = add_signature("Здравствуйте! Готов обсудить вакансию.", SIGNATURE, 1500)

    assert letter == "Здравствуйте! Готов обсудить вакансию.\n\n" + SIGNATURE


def test_add_signature_does_not_duplicate_signature() -> None:
    text = "Текст письма.\n\n" + SIGNATURE

    assert add_signature(text, SIGNATURE, 1500) == text


def test_add_signature_skips_when_contacts_already_present() -> None:
    text = "Текст письма. Пишите: @saneci или a.gladskoy@gmail.com"

    assert add_signature(text, SIGNATURE, 1500) == text


def test_add_signature_without_signature_keeps_letter() -> None:
    assert add_signature("Текст.", "", 1500) == "Текст."


def test_add_signature_respects_total_length_limit() -> None:
    letter = add_signature("Предложение номер один. " * 200, SIGNATURE, 400)

    assert len(letter) <= 400
    assert letter.endswith(SIGNATURE)


def test_add_signature_for_empty_letter_returns_empty() -> None:
    assert add_signature("   ", SIGNATURE, 1500) == ""


def test_parse_generation_adds_signature() -> None:
    result = parse_generation(
        {"verdict": "подходит", "reason": "ok", "cover_letter": "Здравствуйте! Текст."},
        1500,
        SIGNATURE,
    )

    assert result.cover_letter == "Здравствуйте! Текст.\n\n" + SIGNATURE


def test_generate_adds_signature_from_argument() -> None:
    content = json.dumps(
        {"verdict": "подходит", "reason": "ok", "cover_letter": "Здравствуйте! Текст."},
        ensure_ascii=False,
    )

    with make_client(lambda request: chat_response(content)) as client:
        result = client.generate(PROFILE, VACANCY, signature=SIGNATURE)

    assert result.cover_letter == "Здравствуйте! Текст.\n\n" + SIGNATURE


def test_generate_sends_required_opening_to_prompt() -> None:
    """Требование вакансии уходит в промпт, а письмо начинается с нужного слова."""
    captured: dict[str, Any] = {}
    vacancy = dataclasses.replace(
        VACANCY,
        description=(
            "Как откликнуться. Начните сопроводительное письмо со слова 3PL. "
            "Дальше расскажите, почему вам интересна роль."
        ),
    )
    content = json.dumps(
        {"verdict": "подходит", "reason": "ok", "cover_letter": "3PL. Мне интересна задача."},
        ensure_ascii=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["messages"] = json.loads(request.content)["messages"]
        return chat_response(content)

    with make_client(handler) as client:
        result = client.generate(PROFILE, vacancy)

    assert "«3PL»" in captured["messages"][1]["content"]
    assert result.required_opening == "3PL"
    assert result.cover_letter.startswith("3PL. Здравствуйте! Мне интересна задача.")


def test_truncate_cover_letter_by_sentence() -> None:
    text = "Первое предложение. Второе предложение. " + "х" * 2000

    result = truncate_cover_letter(text, 60)

    assert result == "Первое предложение. Второе предложение."


def test_truncate_cover_letter_short_text_unchanged() -> None:
    assert truncate_cover_letter("Короткое письмо.", 100) == "Короткое письмо."


def test_parse_generation_requires_verdict() -> None:
    with pytest.raises(LLMResponseError):
        parse_generation({"reason": "нет вердикта"})


def test_load_system_prompt_falls_back_to_minimal_prompt(tmp_path: Path) -> None:
    assert load_system_prompt(None) == FALLBACK_SYSTEM_PROMPT
    assert load_system_prompt(tmp_path / "missing.md") == FALLBACK_SYSTEM_PROMPT

    empty = tmp_path / "empty.md"
    empty.write_text("   ", encoding="utf-8")
    assert load_system_prompt(empty) == FALLBACK_SYSTEM_PROMPT


def test_load_system_prompt_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "SYSTEM_PROMPT.md"
    path.write_text("Только факты из профиля.", encoding="utf-8")

    assert load_system_prompt(path) == "Только факты из профиля."


def test_generate_uses_system_prompt_file(tmp_path: Path) -> None:
    prompt_path = tmp_path / "SYSTEM_PROMPT.md"
    prompt_path.write_text("МОЙ СИСТЕМНЫЙ ПРОМПТ", encoding="utf-8")
    captured: dict[str, Any] = {}
    content = json.dumps(
        {"verdict": "подходит", "reason": "ok", "cover_letter": "Текст письма."},
        ensure_ascii=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["system"] = payload["messages"][0]["content"]
        return chat_response(content)

    with make_client(handler, system_prompt_path=prompt_path) as client:
        client.generate(PROFILE, VACANCY)

    assert captured["system"] == "МОЙ СИСТЕМНЫЙ ПРОМПТ"


def test_system_prompt_keeps_simple_typography() -> None:
    """В письме не должно быть точек с запятой и длинных тире."""
    prompt = load_system_prompt(PROJECT_ROOT / "SYSTEM_PROMPT.md")

    assert "—" not in prompt
    assert ";" not in prompt


def test_ensure_greeting_keeps_existing() -> None:
    for text in ("Здравствуйте! Текст.", "Здравствуйте. Текст.", "Добрый день! Текст."):
        assert ensure_greeting(text) == (text, False)


def test_ensure_greeting_adds_missing() -> None:
    letter, added = ensure_greeting("Мне интересна задача.")

    assert letter == "Здравствуйте! Мне интересна задача."
    assert added is True


def test_ensure_greeting_removes_dangling_lead() -> None:
    """Инцидент 2026-10-21: письмо начиналось с обрывка «3PL. Мне интересна…»."""
    letter, added = ensure_greeting("3PL. Мне интересна задача строить бэкенд.")

    assert letter == "Здравствуйте! Мне интересна задача строить бэкенд."


def test_ensure_greeting_keeps_first_sentence_intact() -> None:
    """Промпт просит «Здравствуйте! Заинтересовала Ваша вакансия.» — код фразу не трогает."""
    letter, added = ensure_greeting("Заинтересовала Ваша вакансия. Мне интересны задачи.")

    assert letter == "Здравствуйте! Заинтересовала Ваша вакансия. Мне интересны задачи."
    assert added is True


def test_ensure_greeting_keeps_required_opening_first() -> None:
    """Вакансия просит начать с «3PL»: слово остаётся первым, приветствие после него."""
    letter, added = ensure_greeting("3PL. Мне интересна задача строить бэкенд.", "3PL")

    assert letter == "3PL. Здравствуйте! Мне интересна задача строить бэкенд."
    assert added is True


def test_ensure_greeting_keeps_opening_with_greeting() -> None:
    text = "3PL. Здравствуйте! Мне интересна задача."

    assert ensure_greeting(text, "3PL") == (text, False)


def test_ensure_greeting_moves_greeting_after_opening() -> None:
    """Модель начала с приветствия, хотя вакансия требует слово первым."""
    letter, added = ensure_greeting("Здравствуйте! 3PL. Мне интересна задача.", "3PL")

    assert letter == "3PL. Здравствуйте! Мне интересна задача."
    assert added is True


def test_ensure_greeting_adds_missing_opening() -> None:
    letter, added = ensure_greeting("Мне интересна задача.", "3PL")

    assert letter == "3PL. Здравствуйте! Мне интересна задача."
    assert added is True


def test_ensure_greeting_uses_opening_from_vacancy() -> None:
    """Регистр и написание обязательного начала берём из вакансии."""
    letter, added = ensure_greeting("3pl. мне интересна задача.", "3PL")

    assert letter == "3PL. Здравствуйте! Мне интересна задача."
    assert added is True


def test_ensure_greeting_empty_letter() -> None:
    assert ensure_greeting("   ") == ("", False)


def test_parse_generation_adds_greeting_before_signature() -> None:
    result = parse_generation(
        {"verdict": "подходит", "reason": "ok", "cover_letter": "3PL. Мне интересна задача."},
        1500,
        SIGNATURE,
    )

    assert result.greeting_added is True
    assert result.cover_letter.startswith("Здравствуйте! Мне интересна задача.")
    assert result.cover_letter.endswith(SIGNATURE)


def test_parse_generation_respects_required_opening() -> None:
    result = parse_generation(
        {"verdict": "подходит", "reason": "ok", "cover_letter": "3PL. Мне интересна задача."},
        1500,
        SIGNATURE,
        opening="3PL",
    )

    assert result.required_opening == "3PL"
    assert result.greeting_added is True
    assert result.cover_letter.startswith("3PL. Здравствуйте! Мне интересна задача.")
    assert result.cover_letter.endswith(SIGNATURE)


def test_parse_generation_flags_greeting_present() -> None:
    result = parse_generation(
        {"verdict": "подходит", "reason": "ok", "cover_letter": "Здравствуйте! Текст."},
        1500,
        SIGNATURE,
    )

    assert result.greeting_added is False
    assert result.cover_letter.startswith("Здравствуйте! Текст.")


def test_decide_sends_questions_from_config() -> None:
    """Формулировки вопросов задаются только конфигом."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return decision_response()

    config = make_config()
    questions = dict(config.decision.questions)
    questions["mandatory_match"] = questions["mandatory_match"].model_copy(
        update={
            "instructions": "МОЙ ВОПРОС",
            "criteria": {"true": "МОЙ КРИТЕРИЙ", "false": "МОЙ АНТИКРИТЕРИЙ"},
        }
    )
    config = config.model_copy(
        update={"decision": config.decision.model_copy(update={"questions": questions})}
    )

    with LLMClient(config, transport=httpx.MockTransport(handler)) as client:
        client.decide(PROFILE, VACANCY)

    sent = captured["payload"]["questions"]["mandatory_match"]
    assert sent["instructions"] == "МОЙ ВОПРОС"
    assert sent["criteria"] == {"true": "МОЙ КРИТЕРИЙ", "false": "МОЙ АНТИКРИТЕРИЙ"}


def test_decide_uses_weights_from_questions() -> None:
    """Итоговая оценка берёт вес из каждого вопроса, а не из отдельного словаря."""

    def handler(request: httpx.Request) -> httpx.Response:
        return decision_response(
            stack_match=0.0,
            mandatory_match=1.0,
            experience_match=1.0,
            red_flags=1.0,
        )

    config = make_config()
    weights = {
        "stack_match": 0.1,
        "mandatory_match": 0.6,
        "experience_match": 0.15,
        "red_flags": 0.15,
    }
    questions = {
        name: question.model_copy(update={"weight": weights[name]})
        for name, question in config.decision.questions.items()
    }
    config = config.model_copy(
        update={"decision": config.decision.model_copy(update={"questions": questions})}
    )

    with LLMClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.decide(PROFILE, VACANCY)

    assert result.probability == pytest.approx(0.9)
    assert result.fits is True


def test_explain_rejection_is_cheap_and_single_line() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["max_tokens"] = payload["max_tokens"]
        captured["temperature"] = payload["temperature"]
        captured["prompt"] = payload["messages"][-1]["content"]
        return chat_response("Не хватает\nопыта Kafka и Ignite.")

    with make_client(handler) as client:
        reason = client.explain_rejection(PROFILE, VACANCY, 0.04, 0.8)

    assert reason == "Не хватает опыта Kafka и Ignite."
    assert captured["max_tokens"] == REJECTION_MAX_TOKENS
    assert captured["temperature"] == 0.0
    assert "Spring Boot, Kafka, PostgreSQL." in captured["prompt"]


def test_explain_rejection_truncates_long_answer() -> None:
    with make_client(lambda request: chat_response("слово " * 200)) as client:
        reason = client.explain_rejection(PROFILE, VACANCY, 0.1, 0.8)

    assert len(reason) <= REJECTION_MAX_LENGTH
    assert reason.endswith("…")


def test_system_prompt_fits_budget() -> None:
    """Промпт держим компактным: запросы к LLM должны быть дешёвыми."""
    prompt = load_system_prompt(PROJECT_ROOT / "SYSTEM_PROMPT.md")

    assert prompt
    assert len(prompt) <= 3500


def test_system_prompt_requires_opening_sentence() -> None:
    """Первое предложение письма: «Здравствуйте! Заинтересовала Ваша вакансия.»."""
    prompt = load_system_prompt(PROJECT_ROOT / "SYSTEM_PROMPT.md")

    assert "Здравствуйте! Заинтересовала Ваша вакансия." in prompt


def test_system_prompt_has_no_threshold_numbers() -> None:
    """Порог соответствия и лимит длины живут только в конфиге."""
    prompt = load_system_prompt(PROJECT_ROOT / "SYSTEM_PROMPT.md")

    assert "70%" not in prompt


def test_system_prompt_answers_as_candidate() -> None:
    """Ответы на анкету пишутся от первого лица и не ссылаются на профиль."""
    prompt = load_system_prompt(PROJECT_ROOT / "SYSTEM_PROMPT.md")

    assert "от первого лица" in prompt
    assert "Не ссылайся на профиль" in prompt
    assert "1500" not in prompt


def test_generate_passes_pet_projects_when_vacancy_requests_links() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return chat_response(
            json.dumps(
                {"verdict": "подходит", "reason": "ok", "cover_letter": "Здравствуйте! Текст."},
                ensure_ascii=False,
            )
        )

    vacancy = VacancyDetails(
        url="https://hh.ru/vacancy/138836233",
        title="Java-разработчик",
        company="ППК",
        salary="",
        description="Приложите ссылку на ваши проекты в письме.",
    )

    with make_client(
        handler, pet_projects=("https://github.com/user/project",)
    ) as client:
        client.generate(PROFILE, vacancy, signature="")

    prompt = captured["payload"]["messages"][1]["content"]
    assert "ТРЕБОВАНИЕ ВАКАНСИИ К ПРОЕКТАМ" in prompt
    assert "https://github.com/user/project" in prompt

