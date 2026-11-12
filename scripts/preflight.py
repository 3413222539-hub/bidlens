"""提交前安全检查：密钥、隐私信息、不该提交的文件。

用法：
    python scripts/preflight.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent

PATTERNS = {
    "疑似 API Key": re.compile(r"\bsk-[A-Za-z0-9_\-\.]{16,}"),
    "Bearer Token": re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    "私钥": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "硬编码密钥赋值": re.compile(
        r"(?:api[_-]?key|secret|access[_-]?token)\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']", re.I),
    "中国手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "统一社会信用代码": re.compile(r"(?<![0-9A-Z])91[0-9A-Z]{16}(?![0-9A-Z])"),
    "邮箱": re.compile(r"[\w.\-]+@[\w\-]+\.[\w.]{2,}"),
}

ALLOWED_EMAILS = {"dev@example.com"}
ALLOWED_DOMAINS = {"example.com", "example.org"}
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".md", ".json", ".yaml", ".yml",
                 ".ini", ".txt", ".bat", ".sh", ".example", ".ipynb"}
BINARY_SUFFIXES = {".pdf", ".sqlite3", ".db", ".zip", ".png", ".jpg", ".jpeg", ".ico"}
SKIP_HINTS = ("占位", "示例", "placeholder", "example", "形如", "例如", "须为")


def tracked():
    result = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line]


def main():
    files = tracked()
    print(f"检查 {len(files)} 个已跟踪文件\n")
    issues = []

    for name in files:
        path = ROOT / name
        suffix = path.suffix.lower()

        if suffix in BINARY_SUFFIXES:
            issues.append(("不该提交的文件类型", name, "", f"{path.stat().st_size} 字节"))
            continue
        if path.stat().st_size > 512 * 1024:
            issues.append(("超过 512KB", name, "", f"{path.stat().st_size} 字节"))
        if suffix not in TEXT_SUFFIXES and not name.startswith("."):
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for number, line in enumerate(text.splitlines(), 1):
            if any(hint in line for hint in SKIP_HINTS):
                continue
            for label, pattern in PATTERNS.items():
                for match in pattern.finditer(line):
                    hit = match.group(0)
                    if label == "邮箱":
                        domain = hit.split("@")[-1].lower()
                        if hit in ALLOWED_EMAILS or domain in ALLOWED_DOMAINS:
                            continue
                        if hit.endswith(("@qq.com", "@foxmail.com")) and "placeholder" in line.lower():
                            continue
                    issues.append((label, name, number, hit[:60]))

    print("=" * 68)
    if issues:
        print("发现问题：\n")
        for label, name, line, hit in issues:
            where = f"{name}:{line}" if line else name
            print(f"  [{label}] {where}\n      {hit}")
        print(f"\n共 {len(issues)} 处，请人工确认后再提交")
        return 1

    print("✅ 未发现密钥、手机号、信用代码、越界文件")

    # 确认关键忽略规则
    print("\n检查 .gitignore 关键规则：")
    for target in ("data/imports/check.pdf", "profiles/check.json", ".env", "__pycache__/check.pyc"):
        result = subprocess.run(["git", "check-ignore", "-q", target], cwd=ROOT)
        if result.returncode == 0:
            print(f"  ✅ 已忽略 {target}")
        else:
            print(f"  ❌ 未忽略 {target}")
            issues.append(("gitignore", target, "", ""))

    print("\n" + "=" * 68)
    if issues:
        return 1
    print("结论：可以提交与发布")
    return 0


if __name__ == "__main__":
    sys.exit(main())