"""问答回归测试：固定问题集，验证答案与引用页码。

不校验模型逐字输出（模型有非确定性），而是校验三件可控的事：
1. 必须有答案（status == answered）；
2. 答案里必须出现关键事实（金额、日期、法规名）；
3. 引用页码必须落在期望页集合里，证明检索确实定位到了原文。

需要网络与模型配置，可用 `-m "not live"` 跳过。
运行：.venv\\Scripts\\python.exe -m pytest tests/test_qa.py -q
"""

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tender_assistant import ask_model, build_search, find_candidates, load_config, resolve_answer  # noqa: E402
from retrieve_evidence import normalize

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


def case_answer(case):
    expected_scores = case.get("technical_totals")
    if expected_scores is None and "required_source_pages" not in case:
        return ask(case["prefix"], case["question"])

    from web_app import Question, ask as ask_document

    result = ask_document(Question(document_id=folder_of(case["prefix"]).name,
                                   question=case["question"]))
    if expected_scores is None:
        assert set(case["required_source_pages"]) <= {row["page"] for row in result["sources"]}
        return result
    scores = result.get("package_scores", [])
    assert {str(row["package"]): row["max_score"] for row in scores} == expected_scores
    for row in scores:
        assert row["complete"] and row["score_kind"] == "technical"
        assert sum(item["max_score"] for item in row["items"]) == row["max_score"]
        assert all(item["sources"] for item in row["items"])
    return result


@pytest.mark.live
@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case['label']}-{case['kind']}")
def test_问答基线(case):
    result = case_answer(case)
    assert result["status"] == "answered", f"未回答：{result.get('reason')}"

    answer = result["answer"]
    def normalize_amounts(text):
        return re.sub(r'(?<=\d)[,，](?=\d{3}(?:\D|$))', '', text)

    normalized = normalize_amounts(answer)
    missing = [text for text in case["must_contain"] if normalize_amounts(text) not in normalized]
    assert not missing, f"答案缺少关键事实 {missing}\n答案：{answer[:300]}"

    pages = {source["page"] for source in result["sources"]}
    expected = set(case["expect_pages"])
    assert pages & expected, f"引用页码 {sorted(pages)} 未落在期望 {sorted(expected)}"
    assert result["sources"], "缺少原文引用"


@pytest.mark.live
@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case['label']}-{case['kind']}")
def test_引用编号合法(case):
    """引用必须来自检索候选，防止模型编造页码。"""
    search = search_of(case["prefix"])
    candidates = find_candidates(search, case["question"])
    result = case_answer(case)
    if result["status"] != "answered":
        pytest.skip("回答失败，跳过引用校验")
    for source in result["sources"]:
        if "technical_totals" in case:
            assert source["page_verified"] is True
            assert 1 <= source["page"] <= len(search.pdf_pages)
            if source.get("origin") == "pdf_text":
                assert normalize(source["text"]) in normalize(search.pdf_pages[source["page"] - 1])
            else:
                assert any(f"{row['evidence_id']}-R{row['row']}" == source["id"]
                           and row["text"] == source["text"] for row in search.rows.units)
        else:
            assert source["id"] in candidates, f"引用 {source['id']} 不在候选集内"
        assert source["page"] >= 1
        assert source["text"].strip()
