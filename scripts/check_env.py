"""首次启动自检：检查依赖、配置、可选服务连通性。

用法：
    python scripts/check_env.py
"""

import importlib
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ok = True


def line(status, text, hint=""):
    mark = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}[status]
    print(f"  {mark} {text}")
    if hint:
        print(f"      → {hint}")


print("=" * 66)
print("招标条款复核工作台 · 环境自检")
print("=" * 66)

print("\n[1] Python")
version = sys.version_info
if version >= (3, 11):
    line("ok", f"Python {version.major}.{version.minor}.{version.micro}")
else:
    ok = False
    line("fail", f"Python {version.major}.{version.minor} 版本过低", "需要 3.11 或更高")

print("\n[2] 运行依赖")
for module, package in [("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                        ("pypdf", "pypdf"), ("PIL", "Pillow"),
                        ("multipart", "python-multipart")]:
    try:
        importlib.import_module(module)
        line("ok", package)
    except ImportError:
        ok = False
        line("fail", f"{package} 未安装", "pip install -r requirements.txt")

print("\n[3] 配置 .env")
env_path = ROOT / ".env"
if not env_path.is_file():
    ok = False
    line("fail", "缺少 .env 文件", "cp .env.example .env 然后填入 Key")
else:
    config = {}
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = raw.partition("=")
        if sep and not name.strip().startswith("#"):
            config[name.strip()] = value.strip().strip("\"'")
    for key in ("MINERU_API_TOKEN", "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "DASHSCOPE_MODEL"):
        value = config.get(key, "")
        if key == "DASHSCOPE_BASE_URL":
            line("ok" if value else "fail", f"{key} = {value or '未设置'}")
            if not value:
                ok = False
        elif value:
            line("ok", f"{key} 已配置（{len(value)} 字符）")
        else:
            ok = False
            line("fail", f"{key} 未配置", "见 .env.example 的获取说明")

print("\n[4] 模型接口连通性")
api_key = config.get("DASHSCOPE_API_KEY") if env_path.is_file() else ""
base_url = (config.get("DASHSCOPE_BASE_URL") or "").rstrip("/")
model = config.get("DASHSCOPE_MODEL") or ""
if api_key and base_url and model:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 4,
        "enable_thinking": False,
    }
    request = Request(f"{base_url}/chat/completions",
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"},
                      method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            data = json.load(response)
        line("ok", f"模型调用成功（{model}）")
    except HTTPError as error:
        ok = False
        line("fail", f"模型接口返回 HTTP {error.code}",
             "401 说明 Key 无效；404 说明 BASE_URL 或模型名不对")
    except (URLError, TimeoutError):
        line("warn", "无法连接模型接口", "检查网络或代理；离线时问答功能不可用")
    except (KeyError, ValueError):
        line("warn", "模型返回格式异常", "可能该地址不支持 OpenAI 兼容协议")
else:
    line("warn", "跳过（配置不完整）")

print("\n[5] 目录")
for name, hint in [("data", "首次导入时自动创建"), ("profiles", "首次新建公司档案时自动创建")]:
    path = ROOT / name
    if path.is_dir():
        count = len(list(path.iterdir()))
        line("ok", f"{name}/ 存在" + (f"（{count} 项）" if count else "（空）"))
    else:
        line("warn", f"{name}/ 不存在", hint)

print("\n[6] 测试依赖")
try:
    importlib.import_module("pytest")
    line("ok", "pytest 已安装")
except ImportError:
    line("warn", "pytest 未安装", "pip install -r requirements-dev.txt 后可运行测试")

print("\n" + "=" * 66)
if ok:
    print("结论：可以启动")
    print("\n启动命令：")
    print("    python -m uvicorn web_app:app --host 127.0.0.1 --port 8780")
else:
    print("结论：请先修复上面的 ❌ 项")
    sys.exit(1)