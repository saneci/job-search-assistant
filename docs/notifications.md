# Уведомления на email

Канал один — SMTP; что именно присылать, решают раздельные настройки в `config.yaml`.

## Настройки

Транспорт — `notifications.email`:

- `enabled: true` включает канал (без него писем не будет вообще);
- `smtp_host`, `smtp_port`, `use_ssl` — реквизиты сервера (в `config.yaml`);
- `sender`, `recipient` — адреса, личные данные: в `config.local.yaml`
  ([configuration.md](configuration.md));
- `SMTP_PASSWORD` (и при необходимости `SMTP_USERNAME`) — в `.env`.

Виды писем:

- `notifications.report` — письмо с итоговым отчётом после запуска;
- `notifications.captcha` — письмо о капче;
- у каждого вида есть `enabled` и необязательный `recipient`: если он не указан, письмо уходит
  на общий `notifications.email.recipient`.

Проверка канала без запуска агента: `run.bat --notify-test` (по письму на каждый включённый вид).

## Gmail

1. Включите двухэтапную аутентификацию.
2. Создайте «пароль приложения»: аккаунт Google → Безопасность → Пароли приложений.
3. В `config.yaml`, раздел `notifications.email`: `smtp_host: smtp.gmail.com`, `smtp_port: 465`,
   `use_ssl: true`;
   в `config.local.yaml` — `sender` и `recipient` (могут быть одним и тем же адресом),
   в `.env` — `SMTP_PASSWORD` (пароль приложения, не пароль аккаунта).

## Письмо о капче

Когда hh.ru показывает капчу (страница поиска, страница вакансии или форма отклика), приходит
**одно** короткое письмо с темой `Необходимо пройти капчу на hh.ru`: вакансия, ссылка и что сделать.
Повторная капча в той же попытке и остановка запуска второго письма не шлют.

Капча не обходится: приложение ждёт ручного решения `browser.captcha_wait_seconds` секунд
(в headless-режиме не ждёт) и сохраняет скриншот `logs/<run_id>-captcha.png`.

## Если SMTP блокирует вход

`--notify-test` пишет «SMTP-сервер закрыл соединение» — провайдер блокирует SMTP-вход (бывает
у Gmail из некоторых сетей). Отправляйте через другой SMTP: `smtp_host: smtp.yandex.ru`
(или `smtp.mail.ru`), `sender` — адрес этого ящика, `SMTP_USERNAME` и `SMTP_PASSWORD` — логин
и пароль приложения от него. `recipient` может остаться Gmail: ограничение касается только отправки.

Остальные вопросы по письмам — в [troubleshooting.md](troubleshooting.md#письма-на-email).
