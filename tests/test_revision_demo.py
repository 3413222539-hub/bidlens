from pypdf import PdfReader

import web_app
from document_compare import DEADLINE, LIMIT, QUALIFICATION
from scripts.seed_revision_demo import NOTICE_ID, ORIGINAL_ID, seed


def test_keyless_demo_reconfirmation_keeps_old_history(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "IMPORTS", tmp_path / "imports")
    monkeypatch.setattr(web_app, "TEAM", False)

    def require_no_model():
        raise AssertionError("The demo must run without API credentials")

    monkeypatch.setattr(web_app, "load_config", require_no_model)
    seed(tmp_path)
    before = web_app.review_state(ORIGINAL_ID)
    unaffected = web_app.get_checklist(ORIGINAL_ID, None, 1, NOTICE_ID)
    assert not any(row.get("change_revision") for row in unaffected["items"])

    response = web_app.ask(web_app.Question(document_id=ORIGINAL_ID, correction_id=NOTICE_ID,
                                            question="第2包有哪些变更？"))
    assert {row["field"] for row in response["changes"]} == {DEADLINE, LIMIT, QUALIFICATION}
    checklist = web_app.get_checklist(ORIGINAL_ID, None, 2, NOTICE_ID)
    changes = {row["change_revision"]["field"]: row for row in checklist["items"] if row.get("change_revision")}
    assert set(changes) == {DEADLINE, LIMIT, QUALIFICATION}
    assert all(item["previous_confirmation"]["by"] == "演示复核员" for item in changes.values())
    assert "80万元" in PdfReader(web_app.IMPORTS / NOTICE_ID / "document.pdf").pages[0].extract_text()

    revised_id = changes[LIMIT]["id"]
    web_app.set_confirmation(ORIGINAL_ID, web_app.Confirmation(
        scope="package-2", item_id=revised_id, confirmed=True, reviewer="验收复核员",
    ), None)
    seed(tmp_path)
    after = web_app.review_state(ORIGINAL_ID)
    assert after["package-1"] == before["package-1"]
    assert after["package-2"]["price-limit"] == before["package-2"]["price-limit"]
    assert after["package-2"][revised_id]["by"] == "验收复核员"


def test_eligibility_uses_the_selected_correction(tmp_path, monkeypatch):
    import company_profile

    monkeypatch.setattr(web_app, "IMPORTS", tmp_path / "imports")
    monkeypatch.setattr(company_profile, "PROFILES", tmp_path / "profiles")
    monkeypatch.setattr(web_app, "TEAM", False)
    seed(tmp_path)
    company = web_app.create_company(web_app.CompanyProfile(
        name="测试公司", qualifications=[{"type": "食品经营许可证", "valid_until": "2000-01-01"}],
    ))["id"]
    original = web_app.eligibility(ORIGINAL_ID, company, None, 2)
    assert original["verdict"] == "unmet"
    revised = web_app.eligibility(ORIGINAL_ID, company, None, 2, NOTICE_ID)
    assert revised["verdict"] != "unmet"
    assert revised["counts"]["unmet"] == 0
    assert all("食品经营许可证" not in row["requirements"] for row in revised["rows"])
    unchanged = web_app.eligibility(ORIGINAL_ID, company, None, 1, NOTICE_ID)
    assert not any(row["requirements"] for row in unchanged["rows"])
