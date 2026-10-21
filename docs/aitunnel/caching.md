# Кеширование промпта

Повторяющийся префикс промпта можно не тарифицировать по полной цене. У части моделей это происходит само. У Claude и Qwen кэш нужно включить через `cache_control`.

Работает в `/chat/completions`, `/responses` и `/messages`. На прямых маршрутах отдельных провайдеров `cache_control` игнорируется.

Точная скидка на **чтение** кэша — поле `cache_discount` в [каталоге](https://aitunnel.ru/docs/models) и на странице модели: `0.9` значит 90% скидки, чтение стоит 10% от входной цены. Запись в кэш у Claude и Qwen обычно дороже обычного входа. Итог смотрите в `usage.cost_rub`.

Промпт должен быть достаточно длинным. Короче порога модели — кэша не будет.

## Когда само, когда руками

| Как | Кто | Что делать |
|---|---|---|
| Само | OpenAI, DeepSeek, Gemini, Grok, Kimi, GLM | Держите начало сообщений стабильным, меняйте хвост |
| Руками | Claude, Qwen | Без `cache_control` кэша не будет |

Явные точки тоже можно ставить на Gemini (последняя точка в запросе) и на GPT-5.6+ через `prompt_cache_breakpoint`.

## Привязка к провайдеру

Кэш живёт у конкретного провайдера. После запроса с кэшем следующие запросы к той же модели стараются уйти туда же.

- Включается, только если чтение кэша дешевле обычного входа.
- Если этот провайдер недоступен — следующий подходящий.
- Явный `provider.sort` важнее привязки. См. [выбор провайдера](https://aitunnel.ru/docs/provider).
- Сессия остывает через **10 минут** без запросов. Успешный запрос сдвигает таймер. Ошибка провайдера привязку не обновляет.

По умолчанию диалог определяется по хешу первого `system`/`developer` и первого не-системного сообщения. Разные диалоги могут уйти к разным провайдерам; один и тот же префикс остаётся тёплым.

Без `session_id` привязка включается после первого cache hit. С `session_id` — с первого успешного запроса.

## session_id

Явный ключ сессии вместо хеша сообщений. Нужен агентам, у которых начало промпта меняется, а провайдер должен остаться тем же.

- Тело: поле `session_id` верхнего уровня. Если есть и заголовок — берётся тело.
- Заголовок: `x-session-id`.
- Не длиннее 256 символов.
- Если ничего не задали — как ключ подойдёт OpenAI-поле `prompt_cache_key`.

```json
{
  "model": "claude-sonnet-4.6",
  "session_id": "my-agent-session-abc123",
  "messages": [
    { "role": "user", "content": "Продолжим разговор…" }
  ]
}
```

## Как проверить

В каждом ответе:

```json
{
  "usage": {
    "prompt_tokens": 10339,
    "completion_tokens": 60,
    "total_tokens": 10399,
    "prompt_tokens_details": {
      "cached_tokens": 10318,
      "cache_write_tokens": 0
    },
    "cost_rub": 1.24
  }
}
```

- `cached_tokens` — прочитано из кэша. Больше нуля — кэш сработал.
- `cache_write_tokens` — записано в кэш (обычно первый запрос с новым префиксом).
- `cost_rub` — сколько списали. У Claude запись может увеличить сумму, чтение — уменьшить.

В Responses API те же числа лежат в `usage.input_tokens_details`.

## OpenAI

Само, без настроек. Минимум промпта — 1024 токена.

- **Запись:** у моделей до GPT-5.6 обычно бесплатна. GPT-5.6 и новее берут запись по ~1.25× входной цены, даже при автоматическом кэше.
- **Чтение:** скидка из `cache_discount` в каталоге (часто 50% или 90%).

### Явные точки (GPT-5.6+)

`prompt_cache_breakpoint` на текстовом блоке (`text` в Chat Completions, `input_text` в Responses) отмечает конец кэшируемого префикса. `prompt_cache_options.mode: "explicit"` отключает автоматические точки: кэшируется только то, что пометили. `ttl` — например `"30m"`. Минимум TTL кэшированного префикса — 30 минут.

Chat Completions:

```json
{
  "model": "gpt-5.6-sol",
  "prompt_cache_key": "my-session-key",
  "prompt_cache_options": {
    "mode": "explicit",
    "ttl": "30m"
  },
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "<REUSABLE_PREFIX>",
          "prompt_cache_breakpoint": { "mode": "explicit" }
        },
        { "type": "text", "text": "<TASK_SPECIFIC_SUFFIX>" }
      ]
    }
  ]
}
```

Responses:

```json
{
  "model": "gpt-5.6-sol",
  "prompt_cache_key": "my-session-key",
  "prompt_cache_options": {
    "mode": "explicit",
    "ttl": "30m"
  },
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "<REUSABLE_PREFIX>",
          "prompt_cache_breakpoint": { "mode": "explicit" }
        },
        { "type": "input_text", "text": "<TASK_SPECIFIC_SUFFIX>" }
      ]
    }
  ]
}
```

Маркеры взаимозаменяемы: блок с Anthropic-style `cache_control` уходит на поддерживающую GPT как `prompt_cache_breakpoint`, и наоборот — на Claude/Gemini как `cache_control` на 5 минут. TTL не переносится: `ttl` внутри `cache_control` до GPT не доезжает, `prompt_cache_options` остаётся только у OpenAI.

## Grok

Само, без настроек. Запись обычно бесплатна, чтение — по `cache_discount`.

## Kimi (Moonshot)

Само, без настроек. Запись обычно бесплатна, чтение — по `cache_discount`.

## Qwen

Нужны явные точки: `cache_control: { "type": "ephemeral" }` на блоке, тот же синтаксис, что у Claude. TTL записи — 5 минут. Snapshot-эндпоинты часто не умеют. Есть ли кэш у конкретной модели — смотрите `cache_discount` в каталоге.

```json
{
  "model": "qwen3.7-max",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "Используй справочник ниже." },
        {
          "type": "text",
          "text": "БОЛЬШОЙ ТЕКСТ",
          "cache_control": { "type": "ephemeral" }
        },
        { "type": "text", "text": "Кратко опиши реализацию." }
      ]
    }
  ]
}
```

## Claude

Два режима:

- **Автоматический** — `cache_control` на верхнем уровне. Точка ставится на последний кэшируемый блок и сдвигается вперёд по мере роста диалога. Так удобно в длинном чате. То же поле можно сохранить в [пресете](https://aitunnel.ru/docs/presets).
- **Явные точки** — `cache_control` на конкретном текстовом блоке. Максимум 4. Ставьте на большие куски: системный промпт, RAG, главу книги.

| TTL | Как задать | Запись (ориентир) |
|---|---|---|
| 5 минут | `{ "type": "ephemeral" }` | ~1.25× входа |
| 1 час | `{ "type": "ephemeral", "ttl": "1h" }` | ~2× входа |

Чтение — по `cache_discount` (часто 90% скидки, то есть ~0.1× входа). Часовой TTL дороже в записи, но на длинной сессии не приходится переписывать кэш каждые пять минут.

Пороги длины. Короче — кэша не будет:

| Минимум | Модели |
|---|---|
| 4096 токенов | Claude Opus 4.5+, Claude Haiku 4.5 |
| 2048 токенов | Claude Haiku 3.5 |
| 1024 токена | Claude Sonnet 4 / 4.5 / 4.6, Claude Opus 4 / 4.1 |

В Responses API работает только верхний `cache_control`. Точки на блоках `input` через Responses не ставятся — для точечного кэша используйте Chat Completions, Messages или OpenAI-маркер `prompt_cache_breakpoint` (он уйдёт на Claude как `cache_control` на 5 минут, без `ttl`).

В [Batch запросах](https://aitunnel.ru/docs/batch) `cache_control` на строках пакета работает так же, но строки одного пакета могут идти параллельно и в любом порядке: запись одной строки не обязана быть видна другим. Чтобы чтение попало в кэш, ставьте `"ttl": "1h"` на общий префикс и переиспользуйте его в следующих пакетах (или прогрейте синхронным запросом).

### cURL

```bash
curl https://api.aitunnel.ru/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-aitunnel-xxx" \
  -d '{
    "model": "claude-sonnet-4.6",
    "cache_control": { "type": "ephemeral" },
    "messages": [
      {
        "role": "system",
        "content": "Ты историк. Ниже большая книга: …"
      },
      { "role": "user", "content": "Что вызвало крах?" }
    ]
  }'
```

### Python

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-aitunnel-xxx",
    base_url="https://api.aitunnel.ru/v1/",
)

chat_result = client.chat.completions.create(
    messages=[
        {"role": "system", "content": "Ты историк. Ниже большая книга: …"},
        {"role": "user", "content": "Что вызвало крах?"},
    ],
    model="claude-sonnet-4.6",
    extra_body={"cache_control": {"type": "ephemeral"}},
)
print(chat_result.usage)
```

### JavaScript

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "sk-aitunnel-xxx",
  baseURL: "https://api.aitunnel.ru/v1/",
});

const chatResult = await client.chat.completions.create({
  messages: [
    { role: "system", content: "Ты историк. Ниже большая книга: …" },
    { role: "user", content: "Что вызвало крах?" },
  ],
  model: "claude-sonnet-4.6",
  // @ts-expect-error — cache_control нет в типах OpenAI SDK
  cache_control: { type: "ephemeral" },
});

console.log(chatResult.usage);
```

### PHP

```php
<?php
$payload = [
    'model' => 'claude-sonnet-4.6',
    'cache_control' => ['type' => 'ephemeral'],
    'messages' => [
        ['role' => 'system', 'content' => 'Ты историк. Ниже большая книга: …'],
        ['role' => 'user', 'content' => 'Что вызвало крах?'],
    ],
];

$ch = curl_init('https://api.aitunnel.ru/v1/chat/completions');
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_HTTPHEADER, [
    'Content-Type: application/json',
    'Authorization: Bearer sk-aitunnel-xxx',
]);
curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($payload));
echo curl_exec($ch);
```

Поле `cache_control` не входит в стандарт OpenAI: в Python передавайте через `extra_body`, в TypeScript — с `@ts-expect-error`. На cURL ограничений нет.

Автоматическое кэширование с TTL 1 час:

```json
{
  "model": "claude-sonnet-4.6",
  "cache_control": { "type": "ephemeral", "ttl": "1h" },
  "messages": [
    { "role": "system", "content": "Ты полезный ассистент." },
    { "role": "user", "content": "В чём смысл жизни?" }
  ]
}
```

Явная точка на system (5 минут):

```json
{
  "model": "claude-sonnet-4.6",
  "messages": [
    {
      "role": "system",
      "content": [
        {
          "type": "text",
          "text": "Ты историк, изучающий падение Римской империи. Ниже справочник:"
        },
        {
          "type": "text",
          "text": "БОЛЬШОЙ ТЕКСТ",
          "cache_control": { "type": "ephemeral" }
        }
      ]
    },
    {
      "role": "user",
      "content": [{ "type": "text", "text": "Что вызвало крах?" }]
    }
  ]
}
```

Явная точка на user (1 час):

```json
{
  "model": "claude-sonnet-4.6",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "Учитывая книгу ниже:" },
        {
          "type": "text",
          "text": "БОЛЬШОЙ ТЕКСТ",
          "cache_control": { "type": "ephemeral", "ttl": "1h" }
        },
        { "type": "text", "text": "Перечисли персонажей." }
      ]
    }
  ]
}
```

## DeepSeek

Само, без настроек. Запись обычно по цене обычного входа, чтение — по `cache_discount`.

## GLM (Z.AI)

Само, без настроек. Чтение — по `cache_discount`. `session_id` помогает держаться одного кэша в длинном диалоге.

## Gemini

Неявный кэш (как у OpenAI) — без `cache_control`. TTL в среднем 3–5 минут. Минимум длины обычно 1024–4096 токенов, зависит от модели. Начало массива сообщений держите стабильным, вариации — в хвосте.

Явный кэш — `cache_control` на блоке, как у Claude. Создавать и удалять кэш руками, давать ему имя и TTL не нужно: учитывается **последняя** точка в запросе. Несколько точек безопасны (совместимость с Claude), для Gemini лишние игнорируются.

У Gemini одно поле `systemInstruction`. `cache_control` в первом `system`/`developer` кэширует нормализованный системный промпт целиком и не оставляет динамический хвост внутри того же сообщения. Динамику кладите в следующее `user`-сообщение.

System:

```json
{
  "model": "gemini-3.7-flash",
  "messages": [
    {
      "role": "system",
      "content": [
        {
          "type": "text",
          "text": "Ты историк. Ниже справочная книга:"
        },
        {
          "type": "text",
          "text": "БОЛЬШОЙ ТЕКСТ",
          "cache_control": { "type": "ephemeral" }
        }
      ]
    },
    {
      "role": "user",
      "content": [{ "type": "text", "text": "Что вызвало крах?" }]
    }
  ]
}
```

User:

```json
{
  "model": "gemini-3.7-flash",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "По тексту книги ниже:" },
        {
          "type": "text",
          "text": "БОЛЬШОЙ ТЕКСТ",
          "cache_control": { "type": "ephemeral" }
        },
        { "type": "text", "text": "Перечисли главных персонажей." }
      ]
    }
  ]
}
```