# Установка

Обычно ничего делать не нужно: `run.bat` / `run.sh` при первом запуске сами создают `.venv`,
ставят зависимости и Chromium. Ручная установка нужна для отладки или нестандартного окружения.

Требуется Python 3.11+ (проверено на 3.15.1). Все зависимости — готовые wheels, компилятор C++
не нужен: YAML читается чистым Python-парсером `ruamel.yaml`.

```bash
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows
# python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # macOS/Linux
```

Браузер:

- по умолчанию `browser.channel: chrome` — используется установленный Google Chrome, скачивать
  ничего не нужно;
- если Chrome нет: поставьте `browser.channel: ""` и выполните `run.bat --install-browser`
  (или `.venv\Scripts\python.exe -m playwright install chromium`).

Проверка окружения: `run.bat --help` не читает конфиг, а `run.bat --install-browser` читает — покажет
ошибки конфигурации без запуска браузера и LLM.
