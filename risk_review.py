import re
from collections import defaultdict

from retrieve_evidence import coverage_gaps, normalize, pdf_text_units, row_locator
from tender_assistant import read_document_inputs, request_json


RISK_TERMS = r"无效投标|投标无效|投标为无效|投标视为无效|视为无效投标|否决(?:其)?投标|投标(?:将|应)?被(?:拒绝|否决)"
SHORT_INVALIDATION = re.compile(r"^(?:未|不|无|违反|超过|低于|高于).+[,，:：；;，]?\s*(?:其)?投标(?:为|视为)?无效[。；;！!]?$")
SECTION = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[．、. ]*\s*(?:无效投标的(?:条件|情形)|否决投标的(?:条件|情形))\s*$")
NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)+)\s*(?=[^\d.])")
REFERENCE = re.compile(r"第\s*(\d+(?:\.\d+)+)(?:\s*[~～—-]\s*(\d+(?:\.\d+)+))?\s*款")
CHAPTER = re.compile(r"^\s*(第[一二三四五六七八九十]+章)")
LIST_ITEM = re.compile(r"^[（(](\d+)[）)]")
INLINE_ITEM = re.compile(r"(?:^|[:：;；])\s*[（(]([一二三四五六七八九十]|\d+)[）)]")
ATTACHMENT = re.compile(r"^附件([一二三四五六七八九十\d]+)[:：]?$")
TABLE_GROUPS = {"资格审查标准": "资格审查", "符合性审查标准": "符合性审查", "资格性审查": "资格审查", "符合性审查": "符合性审查"}
PROMPT = (
    "你负责从中文招标文件生成投标风险检查项。输入是原文数据。每条候选独立审阅，保持 ID。"
    "仅保留明确的无效/否决投标条件，或资格审查、符合性审查标准。排除目录、总则、"
    "概述、评分扣分、采购项目整体废标、空白模板及没有具体条件的重复项。"
    "保留原文给出的条件限制，例如‘仅最低评标价法的货物采购适用’，不要把条件说成项目已触发。"
    "每个候选最多产出一个检查项。串通投标的多个子项可合并为一个，但保留所有子项。"
    "检查项须有 title（短标题）、condition（准确的触发条件）、check（应检查的材料或动作）。"
    "原文未指定证明材料时，check 写核对动作，不自行发明证明、金额或期限。"
    "保留所有触发分支。‘应符合条件并提供证明，否则无效’意味着不符合条件或未提供证明，不能改成两者同时发生。"
    "标题也须保留比较关系，例如‘高于其他报价’不能写成‘最高报价’。"
    "不要生成新证据或页码。返回 JSON 对象，items 是数组，每项只有 id、title、condition、check。"
    "不应纳入的候选直接省略。"
)


def source_from(unit):
    row = unit.get("row")
    ref = unit["evidence_id"] + (f"-R{row}" if row else "")
    return {"id": ref, "page": unit["page"], "text": unit["text"],
            "page_verified": unit.get("page_verified", True)}


def collect_candidates(units, pdf_text):
    candidates = []
    standards = defaultdict(list)
    numbered = defaultdict(list)
    section = ""
    active = None
    list_parent = None
    attachments = {}
    attachment_group = None
    attachment_table = None
    chapter = ""
    review_group = None
    review_page = 0
    locate = row_locator([normalize(text) for text in pdf_text])
    for unit in units:
        source = source_from(unit)
        text = source["text"].strip()
        chapter_match = CHAPTER.match(text) if not unit["row"] else None
        if chapter_match:
            chapter = chapter_match[1]
        match = NUMBER.match(text)
        number = match[1] if match else ""
        if number:
            numbered[number].append((chapter, source))
        if unit["row"]:
            cells = text.split(" | ")
            group = next((TABLE_GROUPS[cell] for cell in cells if cell in TABLE_GROUPS), None)
            if attachment_group and unit["row"] == 1 and any("审查" in cell for cell in cells):
                attachment_table = unit["evidence_id"]
            if attachment_table == unit["evidence_id"] and len(cells) >= 2 and cells[0].isdigit():
                group = attachment_group
            if group and len(cells) >= (2 if attachment_table == unit["evidence_id"] else 3):
                if number:
                    standards[number].append(source)
                candidates.append({"id": source["id"], "number": number, "group": group, "sources": [source]})
            elif review_group and source["page"] <= review_page + 1 and text.startswith(("未", "不")):
                candidates.append({"id": source["id"], "number": number, "group": review_group, "sources": [source]})
            elif re.search(RISK_TERMS, text) and (len(text) >= 18 or SHORT_INVALIDATION.fullmatch(cells[-1].strip())):
                sources = [source]
                for i, match in enumerate(re.finditer(RISK_TERMS, text), 1):
                    quote = text[max(0, match.start() - 65):min(len(text), match.end() + 20)]
                    page, verified = locate(quote, source["page"])
                    if verified and page != source["page"] and all(existing["page"] != page for existing in sources):
                        sources.append({"id": f"{source['id']}-S{i}", "page": page, "text": quote, "page_verified": True})
                candidates.append({"id": source["id"], "number": number, "group": "无效投标", "sources": sources})
            continue
        invalid_review = re.search(r"(资格(?:性)?审查|符合性审查)不合格\s*[:：]?$", text)
        if invalid_review and "下列" in text:
            review_group = "符合性审查" if invalid_review[1].startswith("符合") else "资格审查"
            review_page = source["page"]
        elif number or CHAPTER.match(text) or re.match(r"^[（(]2[）)]", text):
            review_group = None
        attachment_table = None
        attachment = ATTACHMENT.fullmatch(text)
        attachment_group = attachments.get(attachment[1]) if attachment else None
        for ref, kind in re.findall(r"附件([一二三四五六七八九十\d]+)\s*(资格(?:性)?|符合(?:性)?)\s*(?:审查|检查)", text):
            attachments[ref] = "资格审查" if kind.startswith("资格") else "符合性审查"
        heading = SECTION.match(text)
        if heading:
            section, active = heading[1], None
            continue
        if section and number.startswith(section + "."):
            active = {"id": source["id"], "number": number, "group": "无效投标", "sources": [source]}
            candidates.append(active)
            continue
        child = LIST_ITEM.match(text)
        if list_parent and child:
            parent, intro = list_parent
            candidates.append({"id": source["id"], "number": f"{parent}（{child[1]}）", "group": "无效投标",
                               "sources": [source, intro]})
            continue
        list_parent = None
        if number and re.search(r"下列(?:情况|情形)|有下列", text) and re.search(RISK_TERMS, text):
            list_parent = number, source
            continue
        if active and unit["page"] <= active["sources"][0]["page"] + 1 and not number and len(active["sources"]) < 10:
            previous = active["sources"][-1]["text"].rstrip()
            if child or (not previous.endswith(("。", "；", ";")) and not re.match(r"^第[一二三四五六七八九十]+章", text)):
                active["sources"].append(source)
                continue
        if number or re.match(r"^第[一二三四五六七八九十]+章", text):
            section, active = "", None
        overview = re.search(r"有下列(?:情形|情况)之一.*(?:无效|否决)|相互矛盾.*以本条规定为准", text)
        if not overview and re.search(RISK_TERMS, text) and (len(text) >= 18 or SHORT_INVALIDATION.fullmatch(text)):
            candidates.append({"id": source["id"], "number": number, "group": "无效投标", "sources": [source]})

    for unit in pdf_text_units(units, pdf_text):
        if re.search(RISK_TERMS, normalize(unit["text"])):
            candidates.append({"id": unit["evidence_id"], "number": "", "group": "无效投标",
                               "origin": "pdf_text", "sources": [{**source_from(unit), "origin": "pdf_text"}]})
    return candidates, standards, numbered


def read_risk_inputs(evidence_path, pdf_path):
    _, rows, pdf_text = read_document_inputs(evidence_path, pdf_path)
    return rows, pdf_text


def split_independent_risks(candidates, pdf_text):
    locate = row_locator([normalize(text) for text in pdf_text])
    for item in candidates:
        source = item["sources"][0]
        text = source["text"]
        markers = list(INLINE_ITEM.finditer(text))
        intro = text[:markers[0].start()] if markers else ""
        if (len(item["sources"]) != 1 or len(markers) < 2
                or not re.search(r"下列(?:情况|情形)之一", intro)
                or not re.search(RISK_TERMS, intro)):
            yield item
            continue
        numbers = [marker[1] for marker in markers]
        if numbers not in (list("一二三四五六七八九十"[:len(markers)]),
                           [str(i) for i in range(1, len(markers) + 1)]):
            yield item
            continue
        children = []
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            clause = text[marker.end():end].strip()
            if not clause or "。" in clause.rstrip("。"):
                break
            page, verified = locate(clause, source["page"])
            if not verified:
                break
            child_id = f"{item['id']}-C{index + 1}"
            # The first quote can retain the shared lead-in when both are on the same page.
            quote = text[:end].strip() if index == 0 and page == source["page"] else clause
            children.append({**item, "id": child_id, "context": intro if quote == clause else "",
                             "sources": [{**source, "id": child_id, "text": quote,
                                          "page": page, "page_verified": True}]})
        yield from children if len(children) == len(markers) else [item]


def apply_deposit_scope(items, units):
    deposit_waivers = []
    for unit in units:
        text = normalize(unit["text"])
        if ("投标保证金" in text and not re.search(r"第[一二三四五六七八九十\d]+(?:标)?包", text)
                and re.search(r"本项目(?:不收取|不需要提交|无需提交|不提交)(?:投标)?保证金", text)):
            deposit_waivers.append(source_from(unit))
    if not deposit_waivers:
        return
    for item in items:
        text = item["sources"][0]["text"]
        if re.search(r"未[^。；]{0,26}(?:交纳|缴纳|提交|提供)(?:投标)?保证金|投标保证金金额不足", text):
            item["scope"] = "文件另有本项目免收投标保证金的规定。请对照适用范围依据，核对这项通用条款是否适用。"
            item["scope_sources"] = deposit_waivers[:2]


def extract_batch(batch, config):
    payload = [{"id": item["id"], "group": item["group"],
                "text": "\n".join(filter(None, [item.get("context"), *(source["text"] for source in item["sources"])]))[:4800]} for item in batch]
    allowed = {item["id"] for item in batch}
    try:
        result = request_json(PROMPT, {"candidates": payload}, config, max_tokens=3200)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ValueError("模型未返回有效的风险清单")
        indexed = {}
        for entry in result["items"]:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) and entry[key].strip() for key in ("id", "title", "condition", "check")):
                raise ValueError("模型返回的风险条目缺少必要字段")
            if entry["id"] not in allowed or entry["id"] in indexed:
                raise ValueError("模型返回了未知或重复的证据编号")
            indexed[entry["id"]] = entry
        return indexed
    except (KeyError, IndexError, TypeError, ValueError):
        if len(batch) < 2:
            raise
        # A malformed response should only require reprocessing this batch.
        middle = len(batch) // 2
        return extract_batch(batch[:middle], config) | extract_batch(batch[middle:], config)


def extract_items(evidence_path, pdf_path, config):
    units, pdf_text = read_risk_inputs(evidence_path, pdf_path)
    candidates, standards, numbered = collect_candidates(units, pdf_text)
    candidates = list(split_independent_risks(candidates, pdf_text))
    methods = set(re.findall(r"[☑✓√]\s*(综合评分法|最低评标价法)", "\n".join(pdf_text)))
    items = []
    for offset in range(0, len(candidates), 10):
        batch = candidates[offset:offset + 10]
        indexed = extract_batch(batch, config)
        for item in batch:
            entry = indexed.get(item["id"])
            if not entry:
                continue
            text = "\n".join(source["text"] for source in item["sources"])
            references = []
            refs = dict.fromkeys(ref for match in REFERENCE.finditer(text) for ref in match.groups() if ref)
            chapters = set(re.findall(r"第[一二三四五六七八九十]+章", text))
            target_chapter = next(iter(chapters)) if len(chapters) == 1 else None
            for ref in refs:
                if ref == item["number"]:
                    continue
                if target_chapter:
                    targets = [source for chapter, source in numbered.get(ref, []) if chapter == target_chapter]
                else:
                    matches = numbered.get(ref, [])
                    targets = standards.get(ref) or ([matches[0][1]] if len(matches) == 1 else [])
                references.append({"number": ref, "sources": targets})
            scope = ""
            if methods == {"综合评分法"} and re.search(r"(?:仅|适用于|采用)最低评标价法", text):
                scope = "原文限定最低评标价法适用，本文件前附表勾选综合评分法，请核对适用范围。"
            items.append({**item, **{key: entry[key].strip() for key in ("title", "condition", "check")},
                          "references": references, "scope": scope})
    apply_deposit_scope(items, units)
    gaps = coverage_gaps(units, pdf_text)
    return {"items": items, "candidate_count": len(candidates),
            "uncovered_pages": len(gaps), "uncovered_page_numbers": gaps}
