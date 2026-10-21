#!/usr/bin/env sh
# Запуск hh-agent: Git Bash (Windows), WSL, Linux, macOS.
# Аналог run.bat: создаёт .venv при первом запуске и передаёт аргументы в run.py.
set -eu

cd "$(dirname "$0")"

# В Git Bash/MSYS короткое имя chcp не находится — нужен полный путь.
# В Windows это chcp.com (на части систем — chcp.exe).
for chcp_path in "C:/Windows/System32/chcp.com" \
                 "${SYSTEMROOT:-/nonexistent}/System32/chcp.com" \
                 "C:/Windows/System32/chcp.exe"; do
    if [ -x "$chcp_path" ]; then
        "$chcp_path" 65001 >/dev/null 2>&1 || true
        break
    fi
done

find_python() {
    if [ -x ".venv/Scripts/python.exe" ]; then
        echo ".venv/Scripts/python.exe"
    elif [ -x ".venv/bin/python" ]; then
        echo ".venv/bin/python"
    fi
}

PYTHON="$(find_python)"

if [ -z "$PYTHON" ]; then
    echo "Первый запуск: создаю виртуальное окружение..."
    if command -v py >/dev/null 2>&1; then
        py -3 -m venv .venv || exit 1
    elif command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv || exit 1
    else
        python -m venv .venv || exit 1
    fi

    PYTHON="$(find_python)"
    if [ -z "$PYTHON" ]; then
        echo "Не удалось создать окружение. Проверьте, что установлен Python 3.11+." >&2
        exit 1
    fi

    "$PYTHON" -m pip install --upgrade pip || exit 1
    "$PYTHON" -m pip install -r requirements.txt || exit 1
    "$PYTHON" run.py --install-browser || exit 1
fi

exec "$PYTHON" run.py "$@"
