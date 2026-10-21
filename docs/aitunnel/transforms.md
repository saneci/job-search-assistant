# Оптимизация сообщений

Плагин `context-compression` укорачивает середину диалога, если промпт не влезает в контекст модели — запрос всё равно уходит. В API по умолчанию выключен. В [чате](https://aitunnel.ru/panel/chat) включается сам.

## Как работает

Когда сумма токенов больше окна модели, из середины промпта убираются или обрезаются сообщения, пока всё не влезет. Начало и конец диалога остаются: [модели хуже помнят середину](https://arxiv.org/abs/2307.03172).

Иногда упираются не в токены, а в число сообщений. У Claude лимит — 1000. Тогда остаётся половина с начала и половина с конца.

Без плагина промпт длиннее контекста вернёт ошибку.

Работает в `/chat/completions`, `/responses` и `/messages` для моделей из каталога. На прямых маршрутах отдельных провайдеров плагин не применяется.

## Как включить

В теле запроса:

```json
{
  "plugins": [{ "id": "context-compression" }]
}
```

### cURL

```bash
curl https://api.aitunnel.ru/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-aitunnel-xxx" \
  -d '{
    "model": "claude-sonnet-4.6",
    "plugins": [{ "id": "context-compression" }],
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
    model="claude-sonnet-4.6",
    extra_body={
        "plugins": [{"id": "context-compression"}],
    },
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
  messages: [{ role: "user", content: "Скажи интересный факт" }],
  model: "claude-sonnet-4.6",
  // @ts-expect-error — plugins нет в типах OpenAI SDK
  plugins: [{ id: "context-compression" }],
});

console.log(chatResult.choices[0]?.message);
```

### PHP

```php
<?php
$payload = [
    'model' => 'claude-sonnet-4.6',
    'plugins' => [
        ['id' => 'context-compression'],
    ],
    'messages' => [
        ['role' => 'user', 'content' => 'Скажи интересный факт'],
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

Поле `plugins` не входит в стандарт OpenAI: в Python передавайте через `extra_body`, в TypeScript — с `@ts-expect-error`. На cURL и любых «сырых» HTTP-клиентах ограничений нет.

Включайте для длинных диалогов, где можно укоротить середину истории. Чат в личном кабинете передаёт заголовок `Chat-Aitunnel: 1`, и для такого запроса плагин добавляется сам. Чтобы явно выключить плагин в запросе, где он уже указан, передайте `"enabled": false`.

Другой плагин в том же массиве `plugins` — `file-parser` для [PDF](https://aitunnel.ru/docs/pdf).