"""公司资质档案与招标要求的匹配判定。

设计原则（对齐 TenderGuard 的三态模型）：
- met      ：公司证据明确满足要求
- unmet    ：公司证据明确违反要求（注意：仅"缺失"不等于 unmet）
- unknown  ：证据缺失、含糊或无法比对

匹配只在有明确公司证据时才给 met/unmet，其余一律 unknown，让业务方去核实。
"""

import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

PROFILES = Path(__file__).with_name("profiles")


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKC", str(text or "")).lower() if c.isalnum())


def profile_path(company_id):
    if not re.fullmatch(r"[0-9a-f]{32}", company_id or ""):
        raise ValueError("公司编号无效")
    return PROFILES / f"{company_id}.json"


def list_profiles():
    if not PROFILES.is_dir():
        return []
    rows = []
    for path in sorted(PROFILES.glob("*.json")):
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        qualifications = profile.get("qualifications") or []
        certifications = profile.get("certifications") or []
        rows.append({
            "id": path.stem,
            "name": profile.get("name", "未命名公司"),
            "credit_code": profile.get("credit_code", ""),
            "qualifications": len(qualifications),
            "certifications": len(certifications),
            "performance": len(profile.get("performance") or []),
            "licence_names": [item.get("type", "") for item in qualifications if isinstance(item, dict)],
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    return rows


def delete_profile(company_id):
    path = profile_path(company_id)
    if not path.is_file():
        raise FileNotFoundError("没有找到这份公司档案")
    path.unlink()
    return {"id": company_id, "deleted": True}


def load_profile(company_id):
    path = profile_path(company_id)
    if not path.is_file():
        raise FileNotFoundError("没有找到这份公司档案")
    return json.loads(path.read_text(encoding="utf-8"))


def save_profile(company_id, profile):
    PROFILES.mkdir(parents=True, exist_ok=True)
    path = profile_path(company_id)
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")
    staging.replace(path)
    return profile


def parse_date(value):
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def licence_names(profile):
    """公司持有的各类证照名称，含有效期。"""
    rows = []
    for item in profile.get("qualifications") or []:
        if isinstance(item, dict) and item.get("type"):
            rows.append({"name": item["type"], "valid_until": parse_date(item.get("valid_until"))})
        elif isinstance(item, str) and item.strip():
            rows.append({"name": item.strip(), "valid_until": None})
    return rows


def certification_names(profile):
    rows = []
    for item in profile.get("certifications") or []:
        if isinstance(item, dict) and item.get("name"):
            rows.append({"name": item["name"], "valid_until": parse_date(item.get("valid_until"))})
        elif isinstance(item, str) and item.strip():
            rows.append({"name": item.strip(), "valid_until": None})
    return rows


def matches_name(held, required):
    """证照名称匹配：支持「出版物经营许可证」这类包含关系的简称。

    中文证照常带书名号与「中华人民共和国」前缀，因此比较归一化后的包含关系，
    并要求至少 6 个字的重合，避免「许可证」这种泛词误判。
    """
    left, right = normalize(held), normalize(required)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= 6 and shorter in longer:
        return True
    # 去掉「中华人民共和国」等通用前缀后再比
    stripped = [re.sub(r"^(中华人民共和国|中国)", "", value) for value in (left, right)]
    return stripped[0] == stripped[1] and len(stripped[0]) >= 5


def expired(entry, today=None):
    today = today or date.today()
    return entry["valid_until"] is not None and entry["valid_until"] < today


def check_licence(profile, required, today=None):
    """判定公司是否持有要求的证照。"""
    pool = licence_names(profile) + certification_names(profile)
    hit = next((entry for entry in pool if matches_name(entry["name"], required)), None)
    if hit is None:
        return {"status": "unknown", "reason": f"公司档案中未登记《{required}》，请补充或人工确认。"}
    if expired(hit, today):
        return {"status": "unmet",
                "reason": f"《{hit['name']}》已于 {hit['valid_until']} 过期，需要换新证。"}
    until = f"，有效期至 {hit['valid_until']}" if hit["valid_until"] else ""
    return {"status": "met", "reason": f"公司持有《{hit['name']}》{until}。"}


def requirement_of(card):
    """从自查卡里提取需要比对的证照名称。"""
    names = re.findall(r"《([^》]+)》", f"{card.get('condition', '')}{card.get('check', '')}")
    return list(dict.fromkeys(name for name in names if name))


# 需要报价决策、无法靠公司档案判定的项目
DECISION_TITLES = {"最高限价", "投标保证金"}


def match_checklist(result, profile, today=None):
    """把标包自查清单转换成「我满足吗」的判定表。

    只对能落到公司具体证照的要求给出 met/unmet，
    保证金、限价这类需要报价决策的项目保留人工核对。
    """
    rows = []
    revision_notes = []
    uncertain_revision = any("证照" in warning or "项目编号不同" in warning
                             for warning in result.get("revision_warnings", []))
    for card in result.get("items", []):
        if card.get("group") != "标包自查":
            continue
        if card.get("applicability") == "excluded":
            continue
        names = requirement_of(card)
        revision = card.get("change_revision") or {}
        action = revision.get("action")
        manual_reason = card.get("review_blocked")
        sources = card.get("sources", [])
        if action:
            sources = revision["original_sources"] + revision["correction_sources"]
            previous = list(dict.fromkeys(re.findall(r"《([^》]+)》", revision["before"])))
            current = list(dict.fromkeys(re.findall(r"《([^》]+)》", revision["after"])))
            if action == "删除":
                names = [name for name in previous if name not in current]
                if card["title"] == "证照资格变更" or not current:
                    manual_reason = "请对照公告确认取消范围和仍需准备的证照。"
                elif not names:
                    revision_notes.append(f"已按澄清公告取消「{'、'.join(current)}」要求。")
                    continue
            else:
                names = current
                if (card["title"] == "证照资格变更" or not names
                        or action == "修改" and len(previous) > 1
                        or re.search(r"或|任一|任选|之一|替代|检验报告|证明材料", revision["after"])):
                    manual_reason = "公告含替代条件、材料要求或待核对范围，请结合原文判断。"
        if uncertain_revision and names:
            manual_reason = "澄清公告的适用范围或证照条款仍待核对。"
        base = {
            "id": card.get("id"),
            "title": card.get("title"),
            "condition": card.get("condition"),
            "check": card.get("check"),
            "applicability": card.get("applicability"),
            "sources": sources,
            "requirements": names,
        }
        if manual_reason:
            base.update(status="unknown", reason=manual_reason)
        elif names:
            verdicts = [check_licence(profile, name, today) for name in names]
            worst = next((v for v in verdicts if v["status"] == "unmet"), None) \
                or next((v for v in verdicts if v["status"] == "unknown"), None) \
                or verdicts[0]
            base.update(status=worst["status"], reason=worst["reason"],
                        details=[{"requirement": name, **verdict}
                                 for name, verdict in zip(names, verdicts)])
        elif card.get("title") in DECISION_TITLES:
            base.update(status="unknown",
                        reason="取决于投标报价决策，需按标包金额自行判断，不属于公司资质。")
        elif card.get("applicability") == "pending":
            base.update(status="unknown", reason="原文适用范围需人工核对后才能判断公司是否满足。")
        else:
            base.update(status="unknown", reason="这条要求需人工核对，公司档案无法自动判定。")
        rows.append(base)
    return {**summarise(rows), "revision_notes": revision_notes}


def summarise(rows):
    counts = {"met": 0, "unmet": 0, "unknown": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    total = len(rows)
    blocking = [row for row in rows if row["status"] == "unmet"]
    if blocking:
        verdict = "unmet"
        headline = f"有 {len(blocking)} 项硬性条件不满足，需要先补齐再投标。"
    elif counts["unknown"]:
        verdict = "unknown"
        headline = f"{counts['unknown']} 项待人工确认，其余已满足。"
    elif total:
        verdict = "met"
        headline = "公司档案已覆盖全部自动可判定项。"
    else:
        verdict = "unknown"
        headline = "该标包没有可自动比对的资质要求。"
    return {"verdict": verdict, "headline": headline, "counts": counts,
            "total": total, "rows": rows}