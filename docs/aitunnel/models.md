# Список моделей

Чтобы собрать селектор, проверить цену или узнать, что модель ещё в каталоге, не парсите HTML сайта. Есть публичный
эндпоинт без ключа.

`GET https://api.aitunnel.ru/public/aitunnel/models`

Ключ и заголовок Authorization не нужны. Запрос можно слать из браузера.

## Как вызвать

### cURL

```bash
# все группы
curl https://api.aitunnel.ru/public/aitunnel/models

# только чат
curl https://api.aitunnel.ru/public/aitunnel/models/chat
```

### Python

```python
import requests

catalog = requests.get("https://api.aitunnel.ru/public/aitunnel/models").json()
chat = catalog["chat"]

print(list(chat)[:5])
print(chat["claude-sonnet-4.6"]["prompt_cost"])
```

### JavaScript

```javascript
const catalog = await fetch("https://api.aitunnel.ru/public/aitunnel/models").then((r) => r.json());
const chat = catalog.chat;

console.log(Object.keys(chat).slice(0, 5));
console.log(chat["claude-sonnet-4.6"].prompt_cost);
```

### PHP

```php
<?php
$ch = curl_init('https://api.aitunnel.ru/public/aitunnel/models');
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
$catalog = json_decode(curl_exec($ch), true);
$chat = $catalog['chat'];

print_r(array_slice(array_keys($chat), 0, 5));
echo $chat['claude-sonnet-4.6']['prompt_cost'];
```

Полный ответ — объект по группам: `chat`, `images`, `videos` и остальные. Одна группа:
`GET https://api.aitunnel.ru/public/aitunnel/models/chat` — сразу карта «имя → параметры», без обёртки.

## Группы

| Группа           | Что внутри                                                                                               | Куда слать запросы                                                                      |
|------------------|----------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------|
| `chat`           | Текст, reasoning, мультимодальный чат. Аудио во входе и выходе — [аудио](https://aitunnel.ru/docs/audio) | `/chat/completions`, `/responses`, `/messages`                                          |
| `images`         | Генерация и редактирование картинок                                                                      | `/images/generations` — [картинки](https://aitunnel.ru/docs/images)                     |
| `videos`         | Генерация видео                                                                                          | `/videos` — [видео](https://aitunnel.ru/docs/videos)                                    |
| `embeddings`     | Векторы                                                                                                  | `/embeddings` — [эмбеддинги](https://aitunnel.ru/docs/embeddings)                       |
| `speech`         | Озвучка                                                                                                  | `/audio/speech` — [озвучка текста](https://aitunnel.ru/docs/text-to-speech)             |
| `transcriptions` | Распознавание речи                                                                                       | `/audio/transcriptions` — [распознавание речи](https://aitunnel.ru/docs/speech-to-text) |
| `rerank`         | Переранжирование                                                                                         | `/rerank` — [ранжирование](https://aitunnel.ru/docs/rerank)                             |
| `decisions`      | Ответы да/нет, выбор и шкала с вероятностями                                                             | `/decisions` — [решения](https://aitunnel.ru/docs/decisions)                            |
| `moderations`    | Модерация                                                                                                | `/moderations` — [модерация](https://aitunnel.ru/docs/moderation)                       |

Ключ объекта — имя модели для поля `model` в запросе: `claude-sonnet-4.6`, `gpt-5.6-sol`, `auto`. Чтобы всегда брать
новейший релиз линейки, без привязки к номеру версии, используйте слаг `~author/family-latest` —
см. [последняя версия семейства](https://aitunnel.ru/docs/latest-models).

## Что в ответе

Пример `GET …/models/chat` (цифры из живого каталога меняются — берите из ответа):

```json
{
  "claude-sonnet-4.6": {
    "provider": "anthropic",
    "prompt_cost": 600,
    "completion_cost": 3000,
    "context_size": 1000000,
    "max_output": 64000,
    "description": "Надёжная модель для кода, текстов и анализа",
    "modalities": {
      "input": [
        "text",
        "image",
        "file"
      ],
      "output": [
        "text"
      ]
    },
    "created": 1759161676,
    "batch": {
      "discount": 0.5,
      "window": "24h"
    }
  }
}
```

Общие поля у всех групп:

| Поле                          | Смысл                                                                                                           |
|-------------------------------|-----------------------------------------------------------------------------------------------------------------|
| `provider`                    | Кто крутит модель                                                                                               |
| `description`                 | Коротко, зачем модель                                                                                           |
| `modalities.input` / `output` | `text`, `image`, `audio`, `video`, `file`, `embedding`, `rerank`, `decision`                                    |
| `created`                     | Unix-время появления в каталоге                                                                                 |
| `cache_discount`              | Скидка на чтение кэша промпта (`0.9` = 90% скидки). См. [кеширование промпта](https://aitunnel.ru/docs/caching) |

Цены — в рублях, комиссия уже внутри. Те же числа, что в каталоге на сайте: https://aitunnel.ru/models. Если есть
`batch` — модель ходит в [Batch запросы](https://aitunnel.ru/docs/batch).

У чата `prompt_cost` и `completion_cost` — за 1 млн токенов. У картинок `min_price_per_image` / `max_price_per_image`, у
видео — за секунду, у озвучки — за символы или минуту. Набор полей зависит от группы.

### Цена по длине запроса

У части чат-моделей провайдер поднимает тариф на длинных промптах. У таких моделей есть `price_tiers` — лесенка ступеней
от дешёвой к дорогой:

```json
{
  "qwen3.7-flash": {
    "prompt_cost": 6,
    "completion_cost": 26,
    "price_tiers": [
      {
        "min_prompt_tokens": 0,
        "prompt_cost": 6,
        "completion_cost": 26
      },
      {
        "min_prompt_tokens": 32000,
        "prompt_cost": 20,
        "completion_cost": 80
      },
      {
        "min_prompt_tokens": 256000,
        "prompt_cost": 40,
        "completion_cost": 160
      }
    ]
  }
}
```

Ступень выбирается по числу входных токенов запроса целиком, вместе с историей диалога и системным сообщением. Первый
элемент повторяет `prompt_cost` и `completion_cost`, поэтому считать можно прямо по массиву. Нет поля `price_tiers` —
цена одна на любую длину.

Ориентируйтесь на `price_tiers`, а не на `prompt_cost`, если шлёте длинный контекст: на `qwen3.7-flash` верхняя ступень
дороже базовой в 6,7 раза. Фактически списанную сумму всегда возвращает `usage.cost_rub` в ответе.

## OpenAI-совместимый список

`GET https://api.aitunnel.ru/v1/models` — формат OpenAI: только `id`, `created`, `owned_by`. Нужен ключ. Цен и
возможностей нет. В список попадают ваши пресеты с `owned_by: "preset"`.

### cURL

```bash
curl https://api.aitunnel.ru/v1/models \
  -H "Authorization: Bearer sk-aitunnel-xxx"
```

### Python

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-aitunnel-xxx",
    base_url="https://api.aitunnel.ru/v1/",
)

for model in client.models.list():
    print(model.id, model.owned_by)
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "claude-sonnet-4.6",
      "created": 1759161676,
      "object": "model",
      "owned_by": "anthropic"
    },
    {
      "id": "my-preset",
      "created": 1735689600,
      "object": "model",
      "owned_by": "preset"
    }
  ]
}
```

Для цен и возможностей берите публичный каталог. `/v1/models` — когда нужен стандартный SDK-список или пресеты.