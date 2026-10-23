# hh-agent

Персональный агент автоматических откликов на hh.ru. Работает по требованию: запустил → выполнил цикл →
завершился. Браузерная автоматизация (Playwright + Chromium), оценка вакансий и генерация писем через
LLM (AITUNNEL), отчёт на email.

## Быстрый старт

```bash
run.bat --help      # Windows
sh run.sh --help    # Git Bash / WSL / Linux / macOS
```

`--help` печатает все команды. Обычный запуск:

```bash
run.bat             # Windows
sh run.sh           # Git Bash / WSL / Linux / macOS
```

Скрипты сами создают `.venv`, ставят зависимости и Chromium, а затем передают аргументы в `run.py`.
При первом запуске браузер откроется для ручного входа в hh.ru — сессия сохранится в `data/state.json`.

Все команды и флаги (`--login`, `--retry`, `--excluded`, `--notify-test`, …) — в
[docs/usage.md](docs/usage.md).

## Настройка

1. `.env` из `.env.example`: нужен `AITUNNEL_API_KEY`, для писем на email — `SMTP_PASSWORD`
   (переменные описаны в самом `.env.example`).
2. `config.yaml` — единственный источник параметров: поиск и фильтры, лимиты и паузы, LLM,
   уведомления, пути. Каждый ключ описан комментарием рядом с ним.
3. `data/profile.md` — профиль для LLM: стек, опыт, примеры писем, правила стиля.

Что где править (`.env`, `config.yaml`, профиль, `SYSTEM_PROMPT.md`) — в
[docs/configuration.md](docs/configuration.md).

## Результат запуска

В консоли видно, что происходит с каждой вакансией, в конце печатается итоговый отчёт. Куда ушли
отклики, что отложено и почему — в дневном отчёте `data/report/<дата>.json`; события — в
`logs/<дата>.jsonl`. Форматы, статусы и повторы — в [docs/logs-and-reports.md](docs/logs-and-reports.md).

Отчёт и письма о капче могут приходить на email — [docs/notifications.md](docs/notifications.md).

## Как это работает

Поиск по `config.yaml` → отсев по стоп-словам и прошлым запускам → Decision-вызов LLM (вероятность
«подходит» против порога) → Generation-вызов (письмо) → отклик в браузере → отчёт.
Полный цикл — [docs/how-it-works.md](docs/how-it-works.md); соглашения разработки и гарантии кода (приветствие, подпись,
обязательное начало письма) — [AGENTS.md](AGENTS.md).

## Документация

| Документ                                             | О чём                                          |
|------------------------------------------------------|------------------------------------------------|
| [docs/installation.md](docs/installation.md)         | ручная установка, выбор браузера, зависимости  |
| [docs/usage.md](docs/usage.md)                       | все команды и флаги, режим повторов            |
| [docs/configuration.md](docs/configuration.md)       | `.env`, `config.yaml`, профиль, промпт         |
| [docs/logs-and-reports.md](docs/logs-and-reports.md) | консоль, JSONL-события, дневной отчёт, статусы |
| [docs/notifications.md](docs/notifications.md)       | email: SMTP, виды писем, Gmail                 |
| [docs/troubleshooting.md](docs/troubleshooting.md)   | проблемы и их решения                          |
| [docs/how-it-works.md](docs/how-it-works.md)         | цикл работы и гарантии кода                    |
| [AGENTS.md](AGENTS.md)                               | соглашения проекта для разработки              |
| [docs/aitunnel/](docs/aitunnel/)                     | документация LLM-провайдера                    |

## Тесты

```bash
py -m pip install -r requirements-dev.txt
py -m pytest
```

## Ограничения

- Капча не обходится, API hh.ru не используется.
- Отправка откликов автоматизирована: используйте на свой риск и соблюдайте правила hh.ru.
- Сессия и переписка хранятся локально; наружу уходят только запросы к LLM-провайдеру.
