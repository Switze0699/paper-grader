@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Batch Grader - Web Mode

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   First run: setting up environment, 1-3 minutes...
    echo.
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

if not exist ".env" (
    echo.
    echo   [TIP] API key is not configured yet.
    echo   Open ".env" with Notepad, replace the text after ZHIPU_API_KEY=
    echo   with your key, save, then run this file again.
    echo.
    pause
    exit /b
)

".venv\Scripts\python.exe" -m app.server

echo.
echo   Program stopped.
echo.
pause
