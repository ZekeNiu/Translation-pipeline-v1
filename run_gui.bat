@echo off
chcp 65001 >nul
title 翻译流水线 v2.1 - GUI

REM Check Python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ❌ 未找到 Python！请安装 Python 3.8+
    pause
    exit /b 1
)

REM Check .env
if not exist "%~dp0.env" (
    echo ⚠️  未找到 .env 文件，请在界面中手动输入 API Key
)

echo 启动图形界面...
start "" python "%~dp0gui.py"
