@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Первый запуск: создаю виртуальное окружение...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
    ".venv\Scripts\python.exe" -m playwright install chromium || goto :error
)

".venv\Scripts\python.exe" run.py %*
pause
exit /b 0

:error
echo.
echo Не удалось подготовить окружение. Проверьте, что установлен Python 3.11+.
pause
exit /b 1
