# Последняя версия семейства

Один слаг `~author/family-latest` всегда указывает на новейшую модель линейки. Код не меняете: выходит новая версия — следующие запросы сами идут в неё.

Это удобно для ассистентов и внутренних инструментов, где важнее «свежий Claude Opus», а не конкретный номер релиза. Для тестов и воспроизводимости берите id из [каталога](https://aitunnel.ru/docs/models).

## Как вызвать

В поле `model` передайте алиас:

### cURL

```bash
curl https://api.aitunnel.ru/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-aitunnel-xxx" \
  -d '{
    "model": "~anthropic/claude-opus-latest",
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
    model="~anthropic/claude-opus-latest",
)
print(chat_result.model)  # конкретная версия, которая ответила
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
  model: "~anthropic/claude-opus-latest",
});

console.log(chatResult.model); // конкретная версия, которая ответила
console.log(chatResult.choices[0]?.message);
```

### PHP

```php
<?php
$api_key = 'sk-aitunnel-xxx';
$api_url = 'https://api.aitunnel.ru/v1/chat/completions';

$payload = [
    'model' => '~anthropic/claude-opus-latest',
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

Работает в `/chat/completions`, `/responses` и `/messages`.

## Формат

`~author/family-latest`

| Часть | Смысл | Пример |
|---|---|---|
| `~` | это алиас, не конкретная версия | `~` |
| `author` | кто выпускает линейку | `anthropic`, `openai`, `google` |
| `family` | семейство моделей | `claude-opus`, `claude-sonnet` |
| `latest` | всегда самая новая подходящая модель | `latest` |

Если у семейства нет доступной модели, запрос вернёт ошибку — в другую линейку алиас не уйдёт.

## Какие есть

| Автор | Алиас | Линейка |
|---|---|---|
| Anthropic | `~anthropic/claude-opus-latest` | Claude Opus |
| Anthropic | `~anthropic/claude-sonnet-latest` | Claude Sonnet |
| Anthropic | `~anthropic/claude-haiku-latest` | Claude Haiku |
| Anthropic | `~anthropic/claude-fable-latest` | Claude Fable |
| OpenAI | `~openai/gpt-mini-latest` | GPT Mini |
| OpenAI | `~openai/gpt-astra-latest` | GPT Astra |
| OpenAI | `~openai/gpt-luna-latest` | GPT Luna |
| OpenAI | `~openai/gpt-sol-latest` | GPT Sol |
| OpenAI | `~openai/gpt-terra-latest` | GPT Terra |
| Google | `~google/gemini-flash-latest` | Gemini Flash |
| Google | `~google/gemini-pro-latest` | Gemini Pro |
| DeepSeek | `~deepseek/deepseek-v4-flash-latest` | DeepSeek V4 Flash |
| DeepSeek | `~deepseek/deepseek-flash-latest` | DeepSeek Flash |
| DeepSeek | `~deepseek/deepseek-pro-latest` | DeepSeek Pro |
| Moonshot | `~moonshotai/kimi-latest` | Kimi |
| xAI | `~x-ai/grok-latest` | Grok |
| Z.ai | `~z-ai/glm-latest` | GLM |
| Z.ai | `~z-ai/glm-flash-latest` | GLM Flash |

Новые линейки появляются по мере релизов. Несуществующий слаг вернёт ошибку.

В публичном [каталоге](https://aitunnel.ru/docs/models) и в `GET /v1/models` этих слагов нет: их передают в `model` напрямую. В [чате](https://aitunnel.ru/panel/chat) и [песочнице](https://aitunnel.ru/panel/playground) введите слаг в поиск пикера.

## Что в ответе

Поле `model` — конкретная версия, которая ответила, а не алиас. По ней же считается стоимость и строится статистика.

```json
{
  "id": "chatcmpl-...",
  "model": "anthropic/claude-opus-5.5",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "..."
      }
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 85,
    "total_tokens": 97
  }
}
```

Записывайте это значение, если нужно понять, на какую версию уехал запрос после смены таргета.

## Цена

Списание — по ставке той модели, в которую алиас резолвится сейчас. Когда линейка обновляется, цена и возможности меняются вместе с таргетом. Итог смотрите в `usage.cost_rub`.

## Пресеты и запасные модели

Алиас можно поставить в список моделей [пресета](https://aitunnel.ru/docs/presets) и в массив [`models`](https://aitunnel.ru/docs/fallback). Имя самого пресета остаётся без слеша — слаг живёт только в списке моделей.

Если у ключа включён белый список, в нём должен быть точный слаг (`~anthropic/claude-opus-latest`), а не каталожное имя. См. [API-ключи](https://aitunnel.ru/docs/keys).

## Когда лучше зафиксировать версию

- Регрессионные тесты и бенчмарки, где ответ должен быть сравнимым.
- Продовые сценарии, где нельзя внезапно сменить поведение или цену.
- Нужна конкретная, а не «вторая свежесть»: у алиаса нет отката и нет «предыдущей» версии.

В этих случаях вызывайте id из каталога — `claude-opus-5.5`, `claude-sonnet-5.5`. Тот же слаг, что пришёл в `model` ответа, тоже можно отправить повторно: он уже конкретный.