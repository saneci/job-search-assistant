@echo off
chcp 65001 >nul
cd /d "%~dp0.."

rem Каталог данных по умолчанию: platformdirs.user_data_path("job-search-assistant").
set "PIDFILE=%LOCALAPPDATA%\job-search-assistant\logs\uvicorn\server.pid"
set "FOUND="

if not exist "%PIDFILE%" goto :find_port
set /p PID=<"%PIDFILE%"
echo [INFO] Останавливаю веб-сервер, PID %PID%...
taskkill /F /T /PID %PID% >nul 2>&1
if not errorlevel 1 set "FOUND=1"
del "%PIDFILE%" >nul 2>&1

:find_port
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo [INFO] Останавливаю процесс на порту 8000, PID %%p...
    taskkill /F /T /PID %%p >nul 2>&1
    set "FOUND=1"
)

rem Если PID-файла нет, убиваем оставшиеся процессы uvicorn по командной строке.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uvicorn*web.main*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    if defined FOUND (
        echo [INFO] Веб-сервер остановлен.
    ) else (
        echo [INFO] Веб-сервер не запущен.
    )
) else (
    echo [WARN] Порт 8000 всё ещё занят, проверьте процессы вручную.
)
exit /b 0