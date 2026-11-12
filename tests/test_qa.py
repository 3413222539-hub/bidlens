"""问答回归测试：固定问题集，验证答案与引用页码。

不校验模型逐字输出（模型有非确定性），而是校验三件可控的事：
1. 必须有答案（status == answered）；
2. 答案里必须出现关键事实（金额、日期、法规名）；
3. 引用页码必须落在期望页集合里，证明检索确实定位到了原文。

需要网络与模型配置，可用 `-m "not live"` 跳过。
运行：.venv\\Scripts\\python.exe -m pytest tests/test_qa.py -q
"""

import json
import sys
from functools import lru_cache
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tender_assistant import ask_model, build_search, find_candidates, load_config, resolve_answer  # noqa: E402

IMPORTS = Path(__file__).resolve().parent.parent / "data" / "imports"
CASES = json.loads(Path(__file__).with_name("qa_cases.json").read_text(encoding="utf-8"))


def folder_of(prefix):
    matches = [path for path in IMPORTS.iterdir() if path.name.startswith(prefix)]
    if not matches:
        pytest.skip(f"缺少样本目录 {prefix}")
    folder = matches[0]
    if not (folder / "evidence.json").is_file() or not (folder / "document.pdf").is_file():
        pytest.skip(f"{prefix} 缺少证据或 PDF")
    return folder


@lru_cache(maxsize=8)
def search_of(prefix):
    folder = folder_of(prefix)
    return build_search(folder / "evidence.json", folder / "document.pdf")


@lru_cache(maxsize=1)
def config():
    try:
        return load_config()
    except (OSError, ValueError) as error:
        pytest.skip(f"缺少模型配置：{error}")


@lru_cache(maxsize=None)
def ask(prefix, question):
    """同一问题只调用一次模型，跨测试复用结果。"""
    candidates = find_candidates(search_of(prefix), question)
    return resolve_answer(ask_model(question, candidates, config()), candidates)


@pytest.mark.live
@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case['label']}-{case['kind']}")
def test_问答基线(case):
    result = ask(case["prefix"], case["question"])
    assert result["status"] == "answered", f"未回答：{result.get('reason')}"

    answer = result["answer"]
    missing = [text for text in case["must_contain"] if text not in answer]
    assert not missing, f"答案缺少关键事实 {missing}\n答案：{answer[:300]}"

    pages = {source["page"] for source in result["sources"]}
    expected = set(case["expect_pages"])
    assert pages & expected, f"引用页码 {sorted(pages)} 未落在期望 {sorted(expected)}"
    assert result["sources"], "缺少原文引用"


@pytest.mark.live
@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case['label']}-{case['kind']}")
def test_引用编号合法(case):
    """引用必须来自检索候选，防止模型编造页码。"""
    candidates = find_candidates(search_of(case["prefix"]), case["question"])
    result = ask(case["prefix"], case["question"])
    if result["status"] != "answered":
        pytest.skip("回答失败，跳过引用校验")
    for source in result["sources"]:
        assert source["id"] in candidates, f"引用 {source['id']} 不在候选集内"
        assert source["page"] >= 1
        assert source["text"].strip()