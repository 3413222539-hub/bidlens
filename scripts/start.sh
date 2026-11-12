#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x ".venv/bin/python" ]; then
  echo "[错误] 没有找到 .venv，请先执行："
  echo "    python -m venv .venv && source .venv/bin/activate"
  echo "    pip install -r requirements.txt"
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "[错误] 没有找到 .env，请先执行：cp .env.example .env"
  echo "然后填入 MINERU_API_TOKEN 与 DASHSCOPE_API_KEY"
  exit 1
fi

echo "启动招标条款复核工作台 http://127.0.0.1:8780"
exec .venv/bin/python -m uvicorn web_app:app --host 127.0.0.1 --port 8780