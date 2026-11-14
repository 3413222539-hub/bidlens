from unittest.mock import Mock

import pytest

import tender_assistant
import web_app
from package_review import read_package_facts
from retrieve_evidence import RoutedSearch, SearchIndex, coverage_gaps, pdf_text_units
from risk_review import collect_candidates
from tender_agent import DocumentAgent, compact_tool_result, run_agent


def test_targeted_initial_searches_cover_copy_count_and_candidate_count():
    from tender_agent import initial_searches
    import re

    copies = initial_searches("报价不能超过多少？电子投标文件交几份？")
    assert any(re.search(pattern, "投标文件份数 | 提交加密电子投标文件1份") for _, pattern in copies)
    assert any(re.search(pattern, "最高限价157万元") for _, pattern in copies)
    candidates = initial_searches("能否从文件确定最终有多少家供应商实际提交投标？")
    assert any(re.search(pattern, "确定中标候选人名单：采购包1：3名") for _, pattern in candidates)


def block(ref, page, text):
    return {"evidence_id": ref, "page": page, "text": text, "row": None}


def test_package_limit_conflict_compares_cover_only_with_matching_package():
    from package_review import package_limit_conflict

    pages = [
        "采购包1：\n采购包预算金额（元）：300\n采购包最高限价（元）：200",
        "开标（报价）一览表\n采购包：1（服务）\n最高限价\n180元",
    ]
    rows = [
        dict(block("E9", 2, "序号 | 报价内容 | 最高限价 | 响应报价 | 价款形式"), row=1),
        dict(block("E9", 2, "1 | 服务 | 180元 | 空 | 总价"), row=2),
    ]
    facts = ({1, 2}, [], [], [], [])
    result = package_limit_conflict("采购包1最高限价多少？", rows, pages, facts)
    assert result["status"] == "needs_review"
    assert {source["page"] for source in result["related_sources"]} == {1, 2}
    assert "200元" in result["reason"] and "180元" in result["reason"]
    assert "300元" not in result["reason"]
    assert package_limit_conflict("采购包2最高限价多少？", rows, pages, facts) is None


def test_package_fact_amount_and_source_are_citable():
    source = {"id": "E7-R2", "page": 12, "text": "投标保证金的金额为40000元。"}
    facts = (set(), [{"source": source, "values": {}, "amount": "40000元",
                     "waiver": False, "text": source["text"]}], [], [], [])
    agent = DocumentAgent(None, facts=facts)
    deposit = agent.tool_package_facts()["facts"]["deposits"][0]
    assert deposit["amount"] == "40000元"
    assert deposit["evidence"] == [source]
    result = agent.resolve({"answer": "投标保证金为40000元", "evidence_ids": ["E7-R2"]})
    assert result["sources"][0]["page"] == 12


def test_bid_deposit_waiver_is_distinct_from_performance_deposit(monkeypatch):
    units = [
        block("E1", 1, "15、投标保证金"),
        block("E2", 1, "无"),
        block("E3", 1, "履约保证金为20000元。"),
    ]
    facts = read_package_facts(None, (units, ["15、投标保证金\n无\n履约保证金为20000元。"]))
    monkeypatch.setattr(web_app, "checklist_facts", lambda _: facts)
    model = Mock(side_effect=AssertionError("已确认事实无需调用模型"))
    monkeypatch.setattr(web_app, "ask_model", model)

    result = web_app.ask(web_app.Question(document_id="test", question="本项目是否收取投标保证金？"))
    assert result["answer"] == "文件明确免收投标保证金。"
    assert result["sources"][0]["text"] == "15、投标保证金\n无"
    assert result["sources"][0]["page"] == 1
    model.assert_not_called()


@pytest.mark.parametrize("field, first, second", [
    ("最高限价", "最高限价：第2包75万元", "最高限价：第2包80万元"),
    ("投标保证金", "投标保证金：第2包5000元", "投标保证金：第2包6000元"),
])
def test_amount_conflict_keeps_both_sources(monkeypatch, field, first, second):
    units = [block("E1", 1, first), block("E2", 2, second),
             block("E3", 3, f"{field}：第3包90万元")]
    facts = read_package_facts(None, (units, [unit["text"] for unit in units]))
    monkeypatch.setattr(web_app, "checklist_facts", lambda _: facts)
    result = web_app.ask(web_app.Question(document_id="test", question=f"第2包{field}是多少？"))
    assert result["status"] == "needs_review"
    assert {source["page"] for source in result["related_sources"]} == {1, 2}


def test_incomplete_coverage_labels_answer_and_preserves_native_citation(monkeypatch):
    units = [block("E1", 1, "招标文件")]
    pages = ["招标文件", "未按要求签章的，投标无效。", ""]
    native = list(pdf_text_units(units, pages))
    index = SearchIndex(units + native)
    search = RoutedSearch(index, index)
    search.uncovered_pages = coverage_gaps(units, pages)
    candidates, _, _ = collect_candidates(units, pages)
    ref = candidates[0]["sources"][0]["id"]
    assert ref == native[0]["evidence_id"]
    monkeypatch.setattr(web_app, "search_index", lambda _: search)
    monkeypatch.setattr(web_app, "load_config", lambda: {})
    agent = DocumentAgent(search, uncovered=search.uncovered_pages)
    monkeypatch.setattr(web_app, "build_agent", lambda *_: agent)
    contexts = []

    def decide(messages, config, max_tokens):
        if not contexts:
            contexts.append(messages[-1]["content"])
            return {"action": "tool", "tool": "search", "args": {"query": "未按要求签章"}}
        return {"action": "answer", "answer": "未按要求签章。", "evidence_ids": [ref]}

    monkeypatch.setattr(web_app, "agent_messages_json", decide)

    result = web_app.ask(web_app.Question(document_id="test", question="有哪些无效投标情形？"))
    assert result["partial"] is True
    assert result["uncovered_pages"] == [3]
    assert result["sources"][0]["origin"] == "pdf_text"
    assert result["sources"][0]["page"] == 2
    assert '"uncovered_pages": [3]' in contexts[0]


def test_model_cannot_return_an_unknown_citation(monkeypatch):
    evidence = {"E1": block("E1", 1, "截止时间见前附表。")}
    model = Mock(return_value={"status": "answered", "answer": "明天", "conditions": [],
                               "evidence_ids": ["E404"]})
    monkeypatch.setattr(tender_assistant, "request_json", model)
    result = tender_assistant.ask_model("截止时间是什么？", evidence, {})
    assert tender_assistant.resolve_answer(result, evidence)["status"] == "invalid_response"
    assert model.call_count == 2


def test_scoring_followup_starts_with_the_named_item_evidence(monkeypatch):
    units = [
        block("E1", 44, "项目团队：最高7分"),
        block("E2", 46, "项目团队方案评价：按方案完整程度分为3分、2分、1分、0分"),
    ]
    index = SearchIndex(units)
    search = RoutedSearch(index, index)
    monkeypatch.setattr(web_app, "search_index", lambda _: search)
    monkeypatch.setattr(web_app, "load_config", lambda: {})
    monkeypatch.setattr(web_app, "build_agent", lambda *_: DocumentAgent(search))

    def decide(messages, config, max_tokens):
        import json

        context = json.loads(messages[-1]["content"])
        assert "只查「项目团队方案评价」" in context["question"]
        assert [row["id"] for row in context["targeted_scoring_evidence"]] == ["E2"]
        return {"action": "answer", "answer": "3、2、1、0分", "evidence_ids": ["E2"]}

    monkeypatch.setattr(web_app, "agent_messages_json", decide)
    result = web_app.ask(web_app.Question(document_id="test", question="评分等级怎么划分？",
                                          previous_question="项目团队方案评价最高得几分？"))
    assert result["status"] == "answered"
    assert result["sources"][0]["page"] == 46


def test_tool_budget_preserves_complete_evidence_rows():
    rows = [{"id": f"E{index}", "page": index, "text": "招标条款" * 30}
            for index in range(1, 11)]
    result = compact_tool_result({"found": 10, "evidence": rows}, budget=500)
    assert result["evidence"] == rows[:3]
    assert result["omitted"] == 7
    assert result["found"] == 10


def test_qualification_question_receives_initial_evidence():
    import json

    units = [block("E1", 7, "申请人的资格要求：满足政府采购法第二十二条规定。")]
    index = SearchIndex(units)
    agent = DocumentAgent(RoutedSearch(index, index))

    def decide(messages, config, max_tokens):
        context = json.loads(messages[1]["content"])
        assert context["initial_evidence"][0]["id"] == "E1"
        assert "最后一轮" in messages[-1]["content"]
        return {"action": "answer", "answer": "满足第二十二条规定。", "evidence_ids": ["E1"]}

    result = run_agent(agent, "本项目有哪些资格要求？", {}, decide, max_steps=2)
    assert result["status"] == "answered"
    assert result["sources"][0]["page"] == 7
    assert result["steps"][0]["tool"] == "search"


def test_short_explicit_disqualification_keeps_its_original_page():
    text = "未按要求签章的，投标无效。"
    units = [
        {"evidence_id": "E1", "page": 2, "row": None, "text": text},
        {"evidence_id": "E2", "page": 2, "row": None, "text": "无效投标"},
    ]
    items, _, _ = collect_candidates(units, ["", text])
    assert [item["id"] for item in items] == ["E1"]
    assert items[0]["sources"][0]["page"] == 2


def test_qualification_overview_preserves_notice_under_candidate_budget():
    units = [
        block("E1", 4, "二、申请人的资格要求："),
        block("E2", 4, "满足《中华人民共和国政府采购法》第二十二条规定；"),
        block("E3", 18, "1. 投标人的资格要求"),
        block("E4", 18, "2. 按前附表规定执行。"),
    ]
    search = RoutedSearch(SearchIndex(units[2:]), SearchIndex(units))
    results = tender_assistant.find_candidates(search, "有哪些投标人资格要求？", max_blocks=2)
    assert "E2" in results
