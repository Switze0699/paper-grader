@echo off
cd /d "%~dp0"
title 批改模拟器

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   首次使用，正在配置运行环境，大约需要 1-3 分钟，请耐心等待……
    echo.
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

if not exist ".env" (
    echo.
    echo   [提示] 还没有配置 AI 密钥。
    echo   请用记事本打开 .env 文件，
    echo   把 ZHIPU_API_KEY= 后面换成你的智谱密钥，保存后重新双击本文件。
    echo.
    pause
    exit /b
)

".venv\Scripts\python.exe" -m app.main

if errorlevel 1 (
    echo.
    echo   程序出错了。请截屏上面的提示发给开发者，
    echo   或打开 logs 文件夹里的 app.log 查看详细原因。
    echo.
    pause
)
