@echo off
chcp 65001 >nul
title 翻译流水线 v2.1 - GUI

REM Check Python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ❌ 未找到 Python！请安装 Python 3.11+
    pause
    exit /b 1
)

echo 启动图形界面...
start "" python "%~dp0gui.py"
