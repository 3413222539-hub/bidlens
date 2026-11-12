from pathlib import Path

import pytest
from fastapi import HTTPException

import web_app
from document_compare import DEADLINE, LIMIT, QUALIFICATION, compare_documents
from package_review import read_package_facts


def evidence(*texts):
    return ([{"evidence_id": f"E{i}", "page": page, "row": None, "text": text}
             for i, (page, text) in enumerate(texts, 1)], [])


def compare(original, notice, fields=(LIMIT, DEADLINE), packages=()):
    facts = read_package_facts(None, original)
    return compare_documents(facts, original, notice, fields, set(packages), "original", "notice")["changes"]


def test_public_notice_deadline_and_budget_cannot_stand_in_for_price_limit():
    # 中国政府采购网，渭南师范学院数智校园建设项目的原公告与恢复采购公告。
    original = evidence((1, "预算金额：3,500,000.00元"), (2, "采购包1：不接受联合体投标"),
                        (3, "四、提交投标文件截止时间、开标时间和地点"),
                        (3, "时间：2026年06月24日 09时30分00秒"))
    notice = evidence((1, "原公告的投标文件提交截止时间：2026-06-24 09:30:00，更正为：2026-09-09 09:30:00。"),
                      (2, "采购包最高限价由350万元变更为320万元。"))
    changes = {item["field"]: item for item in compare(original, notice)}
    deadline = changes[DEADLINE]
    assert (deadline["status"], deadline["before"], deadline["after"]) == (
        "changed", "2026-06-24 09:30", "2026-09-09 09:30")
    assert {s["page"] for s in deadline["original_sources"]} == {3}
    assert {s["page"] for s in deadline["correction_sources"]} == {1}
    assert changes[LIMIT]["status"] == "needs_review"
    assert changes[LIMIT]["before"] == "" and changes[LIMIT]["after"] == "320万元"


def test_public_notice_shared_deadline_does_not_apply_to_other_packages():
    # 北京市公共资源交易服务平台，北京卫生职业学院更正公告。
    original = evidence((1, "项目包含第9包、第10包、第11包"),
                        (2, "投标截止时间、开标时间：2026年7月31日09点30分。"))
    notice = evidence((1, "第10包、第11包投标截止时间由2026年7月31日09点30分更正为2026年8月14日9点00分。"))
    assert {item["package"] for item in compare(original, notice)} == {10, 11}
    assert compare(original, notice, packages={9}) == []
    assert compare(original, notice, packages={11})[0]["status"] == "changed"


def test_public_notice_deadline_split_between_blocks():
    # 北京市公共资源交易服务平台，政府网站系统及内容年度运维服务更正公告。
    original = evidence((2, "四、提交投标文件截止时间、开标时间和地点"),
                        (2, "2026-06-11 09:00（北京时间）"))
    notice = evidence((1, "原采购文件中投标文件提交截止时间“2026年6月11日9时00分”"),
                      (1, "变更为“2026年6月12日9时00分”"))
    change, = compare(original, notice)
    assert (change["before"], change["after"]) == ("2026-06-11 09:00", "2026-06-12 09:00")
    assert len(change["correction_sources"]) == 2


def test_ask_compares_requested_package_and_keeps_document_ids(monkeypatch):
    original = evidence((4, "最高限价：第1包100万元；第2包75万元"))
    notice = evidence((1, "第2包最高限价由750000元调整为80万元。"))
    documents = {"original": original, "notice": notice}
    monkeypatch.setattr(web_app, "comparison_files", lambda key: (Path("."), documents[key]))
    monkeypatch.setattr(web_app, "load_config", lambda: pytest.fail("比对无需调用模型"))
    result = web_app.ask(web_app.Question(document_id="original", correction_id="notice",
                                          question="第2包限价改了吗？"))
    change, = result["changes"]
    assert (change["package"], change["before"], change["after"], change["status"]) == (2, "75万元", "80万元", "changed")
    assert [(s["document_id"], s["page"]) for s in change["original_sources"]] == [("original", 4)]
    assert [(s["document_id"], s["page"]) for s in change["correction_sources"]] == [("notice", 1)]
    with pytest.raises(HTTPException, match="请选择另一份"):
        web_app.ask(web_app.Question(document_id="original", correction_id="original", question="第2包限价改了吗？"))


@pytest.mark.parametrize("correction, reason", [
    ("最高限价由75万元调整为80万元。", "未明确"),
    ("第2包最高限价由76万元调整为80万元。", "原值与原招标文件不一致"),
    ("第2包最高限价更正为80万元。第2包最高限价更正为82万元。", "多个值"),
])
def test_ambiguous_changes_require_review(correction, reason):
    original = evidence((3, "最高限价：第1包50万元；第2包75万元"))
    change, = compare(original, evidence((1, correction)), packages={2})
    assert change["status"] == "needs_review"
    assert reason in change["reason"]


def test_ordinary_questions_with_attachment_use_existing_qa(monkeypatch):
    monkeypatch.setattr(web_app, "checklist_facts", lambda _: (
        set(), [], [([{"id": "E1", "page": 2, "text": "最高限价：75万元"}], {None: "75万元"})], [], []))
    monkeypatch.setattr(web_app, "comparison_files", lambda _: pytest.fail("普通问答无需读取澄清公告"))
    result = web_app.ask(web_app.Question(document_id="original", correction_id="notice", question="最高限价是多少？"))
    assert result["status"] == "answered"
    assert "75万元" in result["answer"]


def test_mismatched_project_identifiers_require_relinking(monkeypatch):
    documents = {
        "original": evidence((1, "项目编号：CG-2026-001")),
        "notice": evidence((1, "原公告的采购项目编号：CG-2026-002")),
    }
    monkeypatch.setattr(web_app, "comparison_files", lambda key: (Path("."), documents[key]))
    result = web_app.ask(web_app.Question(document_id="original", correction_id="notice",
                                          question="最高限价有什么变化？"))
    assert result["status"] == "needs_review"
    assert "项目编号不同" in result["reason"]


def test_amount_list_split_over_blocks_uses_each_packages_own_value():
    original = evidence((5, "最高限价：922850元"))
    original[0].extend([
        {"evidence_id": "T", "page": 5, "row": 1, "text": "序号 | 包号 | 包名称 | 包最高限价(元)"},
        {"evidence_id": "T", "page": 5, "row": 2, "text": "1 | 001 | 包1网络建设 | 349560"},
        {"evidence_id": "T", "page": 5, "row": 3, "text": "2 | 002 | 包2网络建设 | 573290"},
    ])
    notice = evidence(
        (8, "本项目最高限价为922850元，其中包1:349560"),
        (8, "包2：573290。"),
        (8, "原招标公告部分设备数量。"),
        (8, "变更为"),
        (8, "本项目最高限价为287130元，其中包1：108320 包2：178810。"),
    )
    change, = compare(original, notice, packages={2})
    assert (change["package"], change["before"], change["after"], change["status"]) == (
        2, "573290元", "178810元", "changed")
    assert {source["page"] for source in change["correction_sources"]} == {8}
    assert compare(original, notice, packages={3}) == []


def test_prior_correction_uses_latest_notice_section_for_the_baseline():
    original = evidence((1, "投标截止时间为2026年2月26日15:00。"),
                        (1, "更正后最新公告"), (3, "投标文件递交截止时间：2026-03-18 15:00"))
    notice = evidence((1, "投标文件递交的截止时间为2026年4月1日15:00。"),
                      (1, "地点为原开标地点。"),
                      (1, "再次更正为：投标文件递交的截止时间为2026年4月15日15:30。"))
    change, = compare(original, notice)
    assert (change["before"], change["after"]) == ("2026-03-18 15:00", "2026-04-15 15:30")
    assert "原值与原招标文件不一致" in change["reason"]


def test_public_cao_county_notice_changes_only_listed_packages():
    original = evidence((2, "最高限价：第2包500万元；第3包510万元；第4包200万元"))
    notice = evidence((1, "第3包段采购文件最高限价变更为：4847902.0 0元；"
                          "第4包段采购文件最高限价变更为1757500.00元。"))
    change, = compare(original, notice, packages={3})
    assert (change["package"], change["after"], change["status"]) == (3, "4847902.00元", "changed")
    assert compare(original, notice, packages={2}) == []


@pytest.mark.parametrize("clause, action", [
    ("第2包取消《食品经营许可证》资格要求。", "删除"),
    ("第2包新增资格要求：须提供《食品生产许可证》。", "新增"),
    ("第2包须提供《食品经营许可证》更正为：须提供《食品生产许可证》或《食品经营许可证》。", "修改"),
])
def test_license_comparison_via_question_keeps_package_and_citations(monkeypatch, clause, action):
    original = evidence((1, "最高限价：第1包50万元；第2包75万元"),
                        (4, "第2包投标人须提供《食品经营许可证》。"))
    documents = {"original": original, "notice": evidence((2, clause))}
    monkeypatch.setattr(web_app, "comparison_files", lambda key: (Path("."), documents[key]))
    result = web_app.ask(web_app.Question(document_id="original", correction_id="notice",
                                          question="澄清后，第2包的证照资格要求有什么变化？"))
    change, = result["changes"]
    assert (change["field"], change["package"], change["action"], change["status"]) == (
        QUALIFICATION, 2, action, "changed")
    assert {s["page"] for s in change["original_sources"]} == {4}
    assert {s["page"] for s in change["correction_sources"]} == {2}
    if action == "修改":
        assert "或" in change["after"]
    other = web_app.ask(web_app.Question(document_id="original", correction_id="notice",
                                         question="第1包还需要《食品经营许可证》吗？"))
    assert other["changes"] == []


def test_license_scoring_change_and_other_named_license_are_excluded():
    original = evidence((4, "投标人须具有《食品经营许可证》。"))
    notice = evidence((1, "删除《食品经营许可证》评分加分。"), (2, "取消《食品经营许可证》资格要求。"))
    rows = compare_documents(read_package_facts(None, original), original, notice, [QUALIFICATION], set(),
                             "original", "notice", "还需要《食品生产许可证》吗？")["changes"]
    assert rows == []


def test_public_cao_county_qualification_change_keeps_certificate_and_package_scope():
    # The before clause is reproduced in the public correction notice.
    original = evidence((2, "最高限价：第1包500万元；第2包510万元；第3包200万元；第4包220万元"),
                        (3, "第1、2、3包段特定资格要求：提供《肥料登记证》和监督检验报告。"))
    notice = evidence((1, "第1、2、3包段特定资格要求提供《肥料登记证》和监督检验报告"
                          "更正为提供《肥料登记证》和带有CMA标记的检验报告。"))
    change, = compare(original, notice, fields=[QUALIFICATION], packages={2})
    assert change["status"] == "changed"
    assert "《肥料登记证》" in change["before"] and "《肥料登记证》" in change["after"]
    assert "CMA" in change["after"]
    assert compare(original, notice, fields=[QUALIFICATION], packages={4}) == []


def test_price_change_requires_confirmation_only_for_affected_package(monkeypatch):
    from checklist_export import render_checklist

    original = evidence((4, "最高限价：第1包100万元；第2包75万元"))
    notice = evidence((1, "第2包最高限价由750000元调整为80万元。"))
    facts = read_package_facts(None, original)
    previous = {"by": "复核员甲", "at": "2026-11-10T10:30:00+00:00"}
    confirmed = {"package-1": {"price-limit": previous},
                 "package-2": {"price-limit": previous, "other-item": previous}}
    monkeypatch.setattr(web_app, "TEAM", False)
    monkeypatch.setattr(web_app, "comparison_files",
                        lambda key: (Path("."), {"original": original, "notice": notice}[key]))
    monkeypatch.setattr(web_app, "get_risks", lambda _: {"items": [], "candidate_count": 0})
    monkeypatch.setattr(web_app, "checklist_facts", lambda _: facts)
    monkeypatch.setattr(web_app, "review_state", lambda _: confirmed)

    reports = [web_app.get_checklist("original", None, package, "notice") for package in (1, 2)]

    unchanged, changed = (next(item for item in result["items"] if item["title"] == "最高限价")
                          for result in reports)
    assert unchanged["id"] == "price-limit"
    assert "price_revision" not in unchanged
    assert changed["id"] not in confirmed["package-2"]
    assert changed["previous_confirmation"] == previous
    assert changed["price_revision"]["original_sources"][0]["page"] == 4
    assert changed["price_revision"]["correction_sources"][0]["page"] == 1
    assert confirmed["package-2"]["other-item"] == previous
    meta = {"name": "招标文件", "pages": 4}
    pending = render_checklist(meta, reports[1], 2, confirmed["package-2"])
    assert "变更后待复核" in pending
    assert "复核员甲" in pending
    confirmed["package-2"][changed["id"]] = {"by": "复核员乙", "at": "2026-11-12T10:00:00+00:00"}
    exported = render_checklist(meta, reports[1], 2, confirmed["package-2"])
    assert "变更后待复核" not in exported
    assert "复核员甲" in exported


def test_deadline_and_license_changes_preserve_unaffected_confirmations(monkeypatch):
    original = evidence((1, "最高限价：第1包50万元；第2包75万元"),
                        (3, "投标截止时间：2026年9月22日14时30分"),
                        (4, "标段2须具有《食品经营许可证》。"))
    notice = evidence((2, "第2包投标截止时间由2026年9月22日14时30分更正为2026年9月28日14时30分。"),
                      (3, "第2包取消《食品经营许可证》资格要求。"))
    documents = {"original": original, "notice": notice}
    old_record = {"by": "复核员甲", "at": "2026-11-10T10:30:00+00:00"}
    facts = read_package_facts(None, original)
    confirmed = {}
    monkeypatch.setattr(web_app, "TEAM", False)
    monkeypatch.setattr(web_app, "comparison_files", lambda key: (Path("."), documents[key]))
    monkeypatch.setattr(web_app, "get_risks", lambda _: {"items": [], "candidate_count": 0})
    monkeypatch.setattr(web_app, "checklist_facts", lambda _: facts)
    monkeypatch.setattr(web_app, "review_state", lambda _: confirmed)

    for number in (1, 2):
        original_cards = web_app.get_checklist("original", None, number)["items"]
        confirmed[f"package-{number}"] = {item["id"]: old_record for item in original_cards}
    unaffected = web_app.get_checklist("original", None, 1, "notice")["items"]
    assert all(item["id"] in confirmed["package-1"] for item in unaffected)
    affected = web_app.get_checklist("original", None, 2, "notice")["items"]
    changes = {item["change_revision"]["field"]: item for item in affected if "change_revision" in item}
    assert set(changes) == {DEADLINE, QUALIFICATION}
    assert changes[DEADLINE]["change_revision"]["after"] == "2026-09-28 14:30"
    assert changes[QUALIFICATION]["change_revision"]["action"] == "删除"
    for field, page in ((DEADLINE, 2), (QUALIFICATION, 3)):
        item = changes[field]
        assert item["id"] not in confirmed["package-2"]
        assert item["previous_confirmation"] == old_record
        assert {source["page"] for source in item["change_revision"]["correction_sources"]} == {page}
    assert any(item["id"] == "price-limit" and "change_revision" not in item for item in affected)


def test_ambiguous_deadline_change_keeps_the_old_confirmation(monkeypatch):
    original = evidence((3, "投标截止时间：2026年9月22日14时30分"))
    notice = evidence((1, "投标截止时间由2026年9月23日14时30分更正为2026年9月28日14时30分。"))
    documents = {"original": original, "notice": notice}
    monkeypatch.setattr(web_app, "TEAM", False)
    monkeypatch.setattr(web_app, "comparison_files", lambda key: (Path("."), documents[key]))
    monkeypatch.setattr(web_app, "get_risks", lambda _: {"items": [], "candidate_count": 0})
    monkeypatch.setattr(web_app, "checklist_facts", lambda _: read_package_facts(None, original))
    monkeypatch.setattr(web_app, "review_state", lambda _: {"package-general": {"bid-deadline": {"by": "甲"}}})
    result = web_app.get_checklist("original", None, None, "notice")
    deadline = next(item for item in result["items"] if item["id"] == "bid-deadline")
    assert "change_revision" not in deadline
    assert any("原值" in warning for warning in result["revision_warnings"])

def test_verified_license_change_without_original_card_has_a_review_item(monkeypatch):
    original = evidence((1, "最高限价：第1包50万元；第2包75万元"),
                        (3, "第2包特定资格要求：提供《肥料登记证》和监督检验报告。"))
    notice = evidence((1, "第2包特定资格要求提供《肥料登记证》和监督检验报告"
                          "更正为提供《肥料登记证》和带有CMA标记的检验报告。"))
    documents = {"original": original, "notice": notice}
    monkeypatch.setattr(web_app, "TEAM", False)
    monkeypatch.setattr(web_app, "comparison_files", lambda key: (Path("."), documents[key]))
    monkeypatch.setattr(web_app, "get_risks", lambda _: {"items": [], "candidate_count": 0})
    monkeypatch.setattr(web_app, "checklist_facts", lambda _: read_package_facts(None, original))
    monkeypatch.setattr(web_app, "review_state", lambda _: {})
    affected = web_app.get_checklist("original", None, 2, "notice")["items"]
    item, = [row for row in affected if row.get("change_revision")]
    assert item["title"] == "证照资格变更" and "CMA" in item["condition"]
    assert item["change_revision"]["original_sources"][0]["page"] == 3
    assert item["change_revision"]["correction_sources"][0]["page"] == 1
    unaffected = web_app.get_checklist("original", None, 1, "notice")["items"]
    assert not any(row.get("change_revision") for row in unaffected)
