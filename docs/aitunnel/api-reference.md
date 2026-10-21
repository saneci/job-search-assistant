# Справочник API

Схемы запросов и ответов AITUNNEL очень похожи на OpenAI Chat API, с небольшими отличиями. **AITUNNEL нормализует схему
для всех моделей и провайдеров**, поэтому достаточно выучить одну.

Работает в `POST https://api.aitunnel.ru/v1/chat/completions`. Те же поля принимают `/responses` и `/messages`. Картинки
во входе чата и генерация — [картинки](https://aitunnel.ru/docs/images). PDF — [PDF](https://aitunnel.ru/docs/pdf).
Видео во входе чата и генерация — [видео](https://aitunnel.ru/docs/videos). Аудио во входе и выходе
чата — [аудио](https://aitunnel.ru/docs/audio). Озвучка — [озвучка текста](https://aitunnel.ru/docs/text-to-speech).
Распознавание речи — [распознавание речи](https://aitunnel.ru/docs/speech-to-text).

Полный индекс документации: [https://aitunnel.ru/llms.txt](https://aitunnel.ru/llms.txt). К любой странице добавьте
`.md` — получите исходный markdown.

## Запросы

Тело `POST` на `/chat/completions`:

```typescript
// Определения подтипов — ниже
type Request = {
    // Нужно одно из двух: messages или prompt
    messages?: Message[];
    prompt?: string;

    // Имя модели, пресета или слаг provider/model.
    // "auto" — AITUNNEL выберет модель сам.
    model?: string;

    // JSON по схеме. Подробнее: /docs/structured-outputs
    response_format?: ResponseFormat;

    stop?: string | string[];
    stream?: boolean;

    // Сжатие слишком длинного промпта. В API выключено; чат личного кабинета включает сам.
    // Подробнее: /docs/transforms
    plugins?: Plugin[];

    max_tokens?: number; // [1, context_length)
    temperature?: number; // [0, 2]

    // Function-tools и серверные инструменты (aitunnel:web_search).
    // Подробнее: /docs/tool-calling, /docs/web-search
    tools?: Tool[];
    tool_choice?: ToolChoice;
    parallel_tool_calls?: boolean;

    seed?: number;
    top_p?: number; // (0, 1]
    top_k?: number; // [1, ∞) — у OpenAI игнорируется
    frequency_penalty?: number; // [-2, 2]
    presence_penalty?: number; // [-2, 2]
    repetition_penalty?: number; // (0, 2]
    logit_bias?: { [key: number]: number };
    top_logprobs?: number;
    min_p?: number; // [0, 1]
    top_a?: number; // [0, 1]

    // Предсказанный хвост ответа — меньше задержка, если угадали.
    prediction?: { type: "content"; content: string };

    // Голосовой ответ. Подробнее: /docs/audio
    modalities?: ("text" | "audio")[];
    audio?: { voice: string; format: string };

    // Параметры AITUNNEL
    models?: string[]; // запасные модели. /docs/fallback
    provider?: { sort?: "price" | "throughput" | "latency" }; // /docs/provider
    reasoning?: Reasoning; // /docs/reasoning
    cache_control?: { type: "ephemeral"; ttl?: "5m" | "1h" }; // /docs/caching
    session_id?: string; // липкая маршрутизация кэша
    service_tier?: "flex" | "priority"; // /docs/service-tiers
};

type TextContent = {
    type: "text";
    text: string;
};

type ImageContentPart = {
    type: "image_url";
    image_url: {
        url: string; // URL или data:…;base64. Подробнее: /docs/images
        detail?: string; // по умолчанию "auto"
    };
};

type FileContentPart = {
    type: "file";
    file: {
        filename: string;
        file_data: string; // URL или data:application/pdf;base64,…  /docs/pdf
    };
};

type VideoContentPart = {
    type: "video_url";
    video_url: {
        url: string; // URL или data:video/mp4;base64. Подробнее: /docs/videos
    };
};

type AudioContentPart = {
    type: "input_audio";
    input_audio: {
        data: string; // сырой base64, не URL. Подробнее: /docs/audio
        format: string; // например "wav" или "mp3"
    };
};

type ContentPart =
    | TextContent
    | ImageContentPart
    | FileContentPart
    | VideoContentPart
    | AudioContentPart;

type Message =
    | {
    role: "user" | "assistant" | "system";
    content: string | ContentPart[];
    name?: string;
}
    | {
    role: "tool";
    content: string;
    tool_call_id: string;
    name?: string;
};

type FunctionDescription = {
    description?: string;
    name: string;
    parameters: object; // JSON Schema
};

type Tool =
    | { type: "function"; function: FunctionDescription }
    | { type: "aitunnel:web_search"; parameters?: object };

type ToolChoice =
    | "none"
    | "auto"
    | "required"
    | { type: "function"; function: { name: string } };

type ResponseFormat =
    | { type: "json_object" }
    | {
    type: "json_schema";
    json_schema: {
        name: string;
        strict?: boolean;
        schema: object;
    };
};

type Plugin =
    | {
    id: "context-compression";
    enabled?: boolean;
}
    | {
    id: "file-parser"; // PDF. /docs/pdf
    pdf?: { engine?: "mistral-ocr" | "cloudflare-ai" | "native" };
};

type Reasoning = {
    effort?:
        | "max"
        | "xhigh"
        | "high"
        | "medium"
        | "low"
        | "minimal"
        | "none";
    max_tokens?: number;
    exclude?: boolean;
    enabled?: boolean;
    context?: "auto" | "all_turns" | "current_turn"; // GPT-5.6+. /docs/reasoning
    mode?: "standard" | "pro"; // GPT-5.6+. /docs/reasoning
};
```

Подробнее по полям:

- [Параметры](https://aitunnel.ru/docs/parameters) — temperature, top_p, max_tokens, stop, logprobs
- [Аудио](https://aitunnel.ru/docs/audio) — `input_audio`, `modalities`, `audio`
- [Структурированный вывод](https://aitunnel.ru/docs/structured-outputs) — `response_format`
- [Вызов инструментов](https://aitunnel.ru/docs/tool-calling) — `tools`, `tool_choice`
- [Веб-поиск](https://aitunnel.ru/docs/web-search) — `aitunnel:web_search`
- [Оптимизация сообщений](https://aitunnel.ru/docs/transforms) — `plugins`
- [Запасные модели](https://aitunnel.ru/docs/fallback) — `models`
- [Выбор провайдера](https://aitunnel.ru/docs/provider) — `provider.sort`
- [Токены рассуждений](https://aitunnel.ru/docs/reasoning) — `reasoning`
- [Кеширование промпта](https://aitunnel.ru/docs/caching) — `cache_control`, `session_id`
- [Уровни обслуживания](https://aitunnel.ru/docs/service-tiers) — `service_tier`
- [Пресеты](https://aitunnel.ru/docs/presets) — имя пресета вместо модели
- [Список моделей](https://aitunnel.ru/docs/models) — какие модели есть
- [Последняя версия семейства](https://aitunnel.ru/docs/latest-models) — `~author/family-latest`
- [Ошибки и отладка](https://aitunnel.ru/docs/errors) — коды, 402, стрим

### Структурированный вывод

`response_format` заставляет модель ответить JSON:

- `{ "type": "json_object" }` — валидный JSON, без схемы
- `{ "type": "json_schema", "json_schema": { ... } }` — строго по вашей JSON Schema, `strict: true`

Подробности и примеры: [структурированный вывод](https://aitunnel.ru/docs/structured-outputs). Есть не у всех моделей —
смотрите страницу модели.

### Плагины

Плагины расширяют запрос на стороне AITUNNEL, не модели. Сжатие середины промпта выключено. Чтобы включить:

```json
{
  "plugins": [
    {
      "id": "context-compression"
    }
  ]
}
```

Подробнее: [оптимизация сообщений](https://aitunnel.ru/docs/transforms). PDF — плагин `file-parser`,
см. [PDF](https://aitunnel.ru/docs/pdf). Поиск в сети — не плагин,
а [серверный инструмент](https://aitunnel.ru/docs/server-tools) `aitunnel:web_search`.

### Стриминг

[Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events#event_stream_format)
работают для всех моделей: передайте `stream: true`. `usage` приходит один раз в финальном чанке с пустым `choices`,
перед `[DONE]`. Примеры и разбор ошибок: [стриминг](https://aitunnel.ru/docs/streaming).

### Нестандартные параметры

Если модель не умеет параметр (например `logit_bias` не у OpenAI или `top_k` у OpenAI), он **игнорируется**. Остальное
уходит провайдеру как есть.

### Маршрутизация модели

- `model` обязателен, если не используете массив `models`. Значение — id из [каталога](https://aitunnel.ru/docs/models),
  имя [пресета](https://aitunnel.ru/docs/presets), `auto`, слаг `~author/family-latest`
  для [последней версии линейки](https://aitunnel.ru/docs/latest-models) или слаг `provider/model`
  для [OpenRouter](https://aitunnel.ru/docs/openrouter).
- Несколько моделей подряд при сбое — массив [`models`](https://aitunnel.ru/docs/fallback).
- Сортировка провайдеров — [`provider.sort`](https://aitunnel.ru/docs/provider).

В ответе поле `model` — фактически использованная модель, не пресет и не `auto`.

## Предзаполнение ответа

Можно попросить модель продолжить начатую реплику: последнее сообщение с `role: "assistant"` — это префикс, который она
допишет.

### cURL

```bash
curl https://api.aitunnel.ru/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-aitunnel-xxx" \
  -d '{
    "model": "gpt-5.6-sol",
    "messages": [
      { "role": "user", "content": "В чём смысл жизни?" },
      { "role": "assistant", "content": "Не уверен, но моё лучшее предположение:" }
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

response = client.chat.completions.create(
    model="gpt-5.6-sol",
    messages=[
        {"role": "user", "content": "В чём смысл жизни?"},
        {"role": "assistant", "content": "Не уверен, но моё лучшее предположение:"},
    ],
)
print(response.choices[0].message.content)
```

### JavaScript

```javascript
import OpenAI from "openai";

const client = new OpenAI({
    apiKey: "sk-aitunnel-xxx",
    baseURL: "https://api.aitunnel.ru/v1/",
});

const response = await client.chat.completions.create({
    model: "gpt-5.6-sol",
    messages: [
        {role: "user", content: "В чём смысл жизни?"},
        {role: "assistant", content: "Не уверен, но моё лучшее предположение:"},
    ],
});
console.log(response.choices[0].message.content);
```

### PHP

```php
<?php
$payload = [
    'model' => 'gpt-5.6-sol',
    'messages' => [
        ['role' => 'user', 'content' => 'В чём смысл жизни?'],
        ['role' => 'assistant', 'content' => 'Не уверен, но моё лучшее предположение:'],
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

## Ответы

Схема нормализована под [OpenAI Chat Completions](https://platform.openai.com/docs/api-reference/chat). `choices` всегда
массив. При стриминге у выбора есть `delta`, иначе — `message`.

```typescript
type Response = {
    id: string;
    choices: (NonStreamingChoice | StreamingChoice | NonChatChoice)[];
    created: number; // Unix timestamp
    model: string; // фактически использованная модель
    object: "chat.completion" | "chat.completion.chunk";
    system_fingerprint?: string;
    usage?: ResponseUsage;
};

type ResponseUsage = {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    cost: number; // то же число, что cost_rub (рубли); читает Cline
    cost_rub: number; // сколько списали за этот запрос, в рублях
    balance: number; // остаток кабинета после списания
    prompt_tokens_details?: {
        cached_tokens?: number;
        cache_write_tokens?: number;
    };
    completion_tokens_details?: {
        reasoning_tokens?: number;
    };
    server_tool_use?: {
        web_search_requests?: number;
    };
};

type NonChatChoice = {
    finish_reason: string | null;
    text: string;
    error?: ErrorResponse;
};

type NonStreamingChoice = {
    finish_reason: string | null;
    native_finish_reason: string | null;
    message: {
        content: string | null;
        role: string;
        tool_calls?: ToolCall[];
        reasoning?: string;
        reasoning_details?: unknown[];
        audio?: { data: string; transcript?: string };
    };
    error?: ErrorResponse;
};

type StreamingChoice = {
    finish_reason: string | null;
    native_finish_reason: string | null;
    delta: {
        content: string | null;
        role?: string;
        tool_calls?: ToolCall[];
        reasoning?: string;
        reasoning_details?: unknown[];
        audio?: { data?: string; transcript?: string };
    };
    error?: ErrorResponse;
};

type ErrorResponse = {
    code: number;
    message: string;
    metadata?: Record<string, unknown>;
};

type ToolCall = {
    id: string;
    type: "function";
    function: { name: string; arguments: string };
};
```

Пример:

```json
{
  "id": "chatcmpl-xxxxxxxxxxxxxx",
  "choices": [
    {
      "finish_reason": "stop",
      "native_finish_reason": "stop",
      "message": {
        "role": "assistant",
        "content": "Привет!"
      }
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 4,
    "total_tokens": 16,
    "prompt_tokens_details": {
      "cached_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 0
    },
    "cost": 0.51,
    "cost_rub": 0.51,
    "balance": 1212.38933
  },
  "model": "gpt-5.6-sol"
}
```

### Причина завершения

`finish_reason` приводится к одному из: `tool_calls`, `stop`, `length`, `content_filter`, `error`.

Исходная строка провайдера — в `native_finish_reason`.

### Стоимость и usage

`usage` всегда есть в нестриминговом ответе. При стриминге — в последнем чанке.

- `prompt_tokens` / `completion_tokens` / `total_tokens` — как вернул провайдер
- `cost` — то же число, что `cost_rub`. Его читают Cline и другие OpenAI-совместимые клиенты
- `cost_rub` — сколько списали за этот запрос, в рублях
- `balance` — остаток кабинета после списания
- `prompt_tokens_details.cached_tokens` — чтение [кэша промпта](https://aitunnel.ru/docs/caching)
- `completion_tokens_details.reasoning_tokens` — [токены рассуждений](https://aitunnel.ru/docs/reasoning)
- `server_tool_use.web_search_requests` — фактические вызовы [веб-поиска](https://aitunnel.ru/docs/web-search)

История расхода по ключу — [статистика](https://aitunnel.ru/docs/api/stats), не отдельный generation-эндпоинт.