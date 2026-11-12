@echo off
chcp 65001 >nul
setlocal

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 没有找到 .venv，请先执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\activate
    echo     pip install -r requirements.txt
    exit /b 1
)

if not exist ".env" (
    echo [错误] 没有找到 .env，请先执行：
    echo     copy .env.example .env
    echo 然后填入 MINERU_API_TOKEN 与 DASHSCOPE_API_KEY
    exit /b 1
)

echo 启动招标条款复核工作台 http://127.0.0.1:8780
.venv\Scripts\python.exe -m uvicorn web_app:app --host 127.0.0.1 --port 8780