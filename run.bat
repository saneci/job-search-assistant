@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON=py"
where py >nul 2>nul || set "PYTHON=python"

if not exist ".venv\Scripts\python.exe" (
    echo Первый запуск: создаю виртуальное окружение...
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo Не удалось создать окружение. Проверьте, что установлен Python 3.11+.
        pause
        exit /b 1
    )

    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 (
        echo Не удалось обновить pip.
        pause
        exit /b 1
    )

    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Не удалось установить зависимости.
        pause
        exit /b 1
    )

    if exist "requirements-web.txt" (
        ".venv\Scripts\python.exe" -m pip install -r requirements-web.txt
        if errorlevel 1 (
            echo Не удалось установить зависимости веб-интерфейса.
            pause
            exit /b 1
        )
    )

    ".venv\Scripts\python.exe" run.py --install-browser
    if errorlevel 1 (
        echo Не удалось подготовить браузер для Playwright.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" run.py %*
exit /b %errorlevel%
