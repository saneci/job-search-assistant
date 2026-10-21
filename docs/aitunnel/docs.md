# Введение

AITUNNEL — единый OpenAI-совместимый API для доступа к ведущим ИИ-моделям из России: OpenAI, Anthropic, Google,
DeepSeek, Qwen, Moonshot и другим. Один ключ, один формат запросов, оплата в рублях, VPN не нужен.

## Ключевые параметры

- Base URL: `https://api.aitunnel.ru/v1`
- Заголовок авторизации: `Authorization: Bearer sk-aitunnel-xxx`
- Формат ключа: `sk-aitunnel-…` 
- Формат запросов: OpenAI Chat Completions API
- Актуальный список моделей и цены: [models.md](models.md])

Если запрос отправляется с сервера за пределами России, ответ вернётся ещё быстрее — без дополнительных прокси.

## Зеркало API

У части российских провайдеров блокируется сеть, через которую работает `api.aitunnel.ru`. Симптом — запрос не доходит
вообще: таймаут соединения, `Connection reset`, `Failed to fetch`. Именно без JSON и без `error.code` в ответе, иначе
дело не в блокировке.

В этом случае поменяйте base URL на `https://ru-api.aitunnel.ru/v1`. Это тот же API: те же ключи, пути, модели, цены и
лимиты — отличается только хост, больше в коде править нечего.

Зеркало добавляет один сетевой переход, поэтому отвечает чуть медленнее. Если `api.aitunnel.ru` открывается —
оставайтесь на нём.

## Быстрый старт

1. Зарегистрируйтесь на https://aitunnel.ru
2. Укажите ключ и base URL в любой библиотеке, совместимой с OpenAI

### cURL

```bash
curl https://api.aitunnel.ru/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-aitunnel-xxx" \
  -d '{
    "model": "deepseek-v4-pro",
    "max_tokens": 50000,
    "messages": [
      { "role": "user", "content": "Скажи интересный факт" }
    ]
  }'
```

### Python

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-aitunnel-xxx",  # ключ из личного кабинета
    base_url="https://api.aitunnel.ru/v1/",
)

chat_result = client.chat.completions.create(
    messages=[{"role": "user", "content": "Скажи интересный факт"}],
    model="deepseek-v4-pro",
    max_tokens=50000,  # указывайте для точного расчёта цены
)
print(chat_result.choices[0].message)
```

### JavaScript

```javascript
import OpenAI from "openai";

const client = new OpenAI({
    apiKey: "sk-aitunnel-xxx", // ключ из личного кабинета
    baseURL: "https://api.aitunnel.ru/v1/",
});

const chatResult = await client.chat.completions.create({
    messages: [{role: "user", content: "Скажи интересный факт"}],
    model: "deepseek-v4-pro",
    max_tokens: 50000, // указывайте для точного расчёта цены
});

console.log(chatResult.choices[0]?.message);
```

### Go

```go
package main

import (
  "context"
  "fmt"
  "log"

  openai "github.com/sashabaranov/go-openai"
)

func main() {
  client := openai.NewClientWithConfig(openai.ClientConfig{
    APIKey:  "sk-aitunnel-xxx",
    BaseURL: "https://api.aitunnel.ru/v1/",
  })

  resp, err := client.CreateChatCompletion(context.Background(), openai.ChatCompletionRequest{
    Model:     "deepseek-v4-pro",
    MaxTokens: 50000,
    Messages: []openai.ChatCompletionMessage{
      {Role: "user", Content: "Скажи интересный факт"},
    },
  })
  if err != nil {
    log.Fatal(err)
  }

  fmt.Println(resp.Choices[0].Message.Content)
}
```

### PHP

```php
<?php
$api_key = 'sk-aitunnel-xxx';
$api_url = 'https://api.aitunnel.ru/v1/chat/completions';

$payload = [
    'model' => 'deepseek-v4-pro',
    'max_tokens' => 50000,
    'messages' => [
        ['role' => 'user', 'content' => 'Скажи интересный факт'],
    ],
];

$ch = curl_init($api_url);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_HTTPHEADER, [
    'Content-Type: application/json',
    'Authorization: Bearer ' . $api_key,
]);
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($payload));

echo curl_exec($ch);
```

Указывайте `max_tokens`: по этому значению резервируется стоимость запроса, а после ответа списывается фактический
расход.

## Возможности

- Чат с моделями — `POST /chat/completions`. Текстовые запросы к 100+ моделям: GPT, Claude, Gemini, DeepSeek, Qwen, Kimi
  и другим.
- [Генерация изображений](https://aitunnel.ru/docs/images) — `POST /images/generations`. GPT Image, Flux, Seedream —
  генерация и редактирование по референсным изображениям.
- [Генерация видео](https://aitunnel.ru/docs/videos) — `POST /videos`. Veo, Seedance, Wan через асинхронный эндпоинт с
  опросом статуса задачи.
- [Аудио в чате](https://aitunnel.ru/docs/audio) — `POST /chat/completions`. Аудиофайл на вход (input_audio) и голосовой
  ответ модели (modalities, audio).
- [Озвучка текста](https://aitunnel.ru/docs/text-to-speech) — `POST /audio/speech`. Text-to-Speech с выбором голоса и
  формата аудио.
- [Распознавание речи](https://aitunnel.ru/docs/speech-to-text) — `POST /audio/transcriptions`. Whisper, GPT-4o
  Transcribe, Voxtral, Qwen3-ASR, Chirp 3 — транскрипция аудио.
- [Эмбеддинги](https://aitunnel.ru/docs/embeddings) — `POST /embeddings`. Векторные представления текста для поиска и
  RAG.
- [Модерация](https://aitunnel.ru/docs/moderation) — `POST /moderations`. Проверка контента перед отправкой в модель или
  публикацией.
- [Ранжирование](https://aitunnel.ru/docs/rerank) — `POST /rerank`. Перестановка документов по релевантности запросу —
  второй шаг RAG после эмбеддингов.
- [Решения](https://aitunnel.ru/docs/decisions) — `POST /decisions`. Ответы да/нет, выбор варианта и оценка по шкале с
  вероятностями — для роутинга, классификации и проверки действий агента.
- [RAG](https://aitunnel.ru/docs/rag) — `POST /embeddings · /rerank`. Поиск по своей базе и ответ модели по найденному
  контексту — эмбеддинги, rerank, чат.
- Другие форматы API — `POST /messages · /responses`. Нативный Claude Messages API и Responses API OpenAI — те же ключ и
  base URL.
- [Пакетные запросы](https://aitunnel.ru/docs/batch) — `POST /batches`. Много запросов одним пакетом со скидкой на
  токены. Результаты забираете позже.

## Модель auto

Если не хотите выбирать модель вручную, укажите `"model": "auto"` — AITUNNEL подберёт модель под сложность запроса и
баланс цены и качества.

### cURL

```bash
curl https://api.aitunnel.ru/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-aitunnel-xxx" \
  -d '{
    "model": "auto",
    "messages": [
      { "role": "user", "content": "Скажи интересный факт" }
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
    messages=[{"role": "user", "content": "Скажи интересный факт"}],
    model="auto",
)
print(chat_result.choices[0].message)
```

### JavaScript

```javascript
import OpenAI from "openai";

const client = new OpenAI({
    apiKey: "sk-aitunnel-xxx",
    baseURL: "https://api.aitunnel.ru/v1/",
});

const chatResult = await client.chat.completions.create({
    messages: [{role: "user", content: "Скажи интересный факт"}],
    model: "auto",
});

console.log(chatResult.choices[0]?.message);
```

### Go

```go
package main

import (
  "context"
  "fmt"
  "log"

  openai "github.com/sashabaranov/go-openai"
)

func main() {
  client := openai.NewClientWithConfig(openai.ClientConfig{
    APIKey:  "sk-aitunnel-xxx",
    BaseURL: "https://api.aitunnel.ru/v1/",
  })

  resp, err := client.CreateChatCompletion(context.Background(), openai.ChatCompletionRequest{
    Model: "auto",
    Messages: []openai.ChatCompletionMessage{
      {Role: "user", Content: "Скажи интересный факт"},
    },
  })
  if err != nil {
    log.Fatal(err)
  }

  fmt.Println(resp.Choices[0].Message.Content)
}
```

### PHP

```php
<?php
$api_key = 'sk-aitunnel-xxx';
$api_url = 'https://api.aitunnel.ru/v1/chat/completions';

$payload = [
    'model' => 'auto',
    'messages' => [
        ['role' => 'user', 'content' => 'Скажи интересный факт'],
    ],
];

$ch = curl_init($api_url);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_HTTPHEADER, [
    'Content-Type: application/json',
    'Authorization: Bearer ' . $api_key,
]);
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($payload));

echo curl_exec($ch);
```

Оплата идёт по фактически выбранной модели, её точное название возвращается в поле `model` ответа. Работает в
`/chat/completions`, `/responses` и `/messages`.

## Поддержка

- Кнопка «Спросить ИИ» в правом нижнем углу сайта — ассистент отвечает по этой документации и актуальному каталогу
  моделей с ценами.
- Почта: support@aitunnel.ru
- Телеграм: https://t.me/aitunnelru
