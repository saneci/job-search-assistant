#!/usr/bin/env sh
# Остановка веб-сервера отчётов, запущенного агентом в фоне.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# В Git Bash на Windows используем batch-скрипт: он умеет убивать дерево процессов.
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        if command -v cygpath >/dev/null 2>&1; then
            exec cmd //c "$(cygpath -w "$SCRIPT_DIR/stop-server.bat")"
        fi
        exec cmd //c "$SCRIPT_DIR\\stop-server.bat"
        ;;
esac

cd "$SCRIPT_DIR/.."

PYTHON=".venv/Scripts/python.exe"
[ -x "$PYTHON" ] || PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

PID_FILE="$("$PYTHON" -c "from agent.config import DEFAULT_DATA_DIR; print(DEFAULT_DATA_DIR / 'logs' / 'uvicorn' / 'server.pid')")"
FOUND=0

if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE")"
    echo "Останавливаю веб-сервер (PID $PID)..."
    pkill -P "$PID" 2>/dev/null || true
    kill "$PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    FOUND=1
fi

# PID-файла может не быть, если сервер запущен вручную: ищем по порту и команде.
if command -v lsof >/dev/null 2>&1; then
    for PID in $(lsof -ti tcp:8000 2>/dev/null || true); do
        kill "$PID" 2>/dev/null || true
        FOUND=1
    done
fi

if command -v pkill >/dev/null 2>&1; then
    if pkill -f "uvicorn web.main:app" 2>/dev/null; then
        FOUND=1
    fi
fi

if [ "$FOUND" -eq 1 ]; then
    echo "Веб-сервер остановлен."
else
    echo "Веб-сервер не запущен."
fi
