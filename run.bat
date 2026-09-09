@echo off
chcp 65001 >nul
title 翻译流水线 v2.1

echo ================================================
echo       翻译流水线 v2.1 — 多 AI 厂商
echo ================================================
echo.

REM Check Python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ❌ 未找到 Python！请安装 Python 3.11+
    pause
    exit /b 1
)

REM Check .env
if not exist "%~dp0.env" (
    echo ⚠️  未找到 .env 文件
    echo    请复制 .env.example 为 .env 并填入 API Key
    echo.
)

REM Get folder path
set "FOLDER=%~1"
if "%FOLDER%"=="" (
    echo 用法：
    echo   1) 拖拽 MinerU 文件夹到此 .bat 文件上
    echo   2) 或在命令行运行：run.bat "C:\MinerU\output"
    echo.
    set /p FOLDER="📂 请输入 MinerU 文件夹路径: "
)

echo 📂 输入: "%FOLDER%"
echo.

python "%~dp0translate.py" "%FOLDER%"

if %ERRORLEVEL% neq 0 (
    echo.
    echo ❌ 任务未完全完成，请查看上方错误或结果目录中的质量报告。可能的原因：
    echo   - .env 中的 API Key 无效
    echo   - 文件夹路径不正确
    echo   - 网络连接问题
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ✅ 翻译完成！
echo 📁 输出位置: %~dp0translations\
pause
