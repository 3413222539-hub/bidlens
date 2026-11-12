"""Answer score overviews from explicitly scoped scoring tables."""

import re
import unicodedata

from document_compare import comparison_packages
from package_review import ORDINAL
from retrieve_evidence import normalize
from risk_review import source_from

HEADING = re.compile(r"第\s*(\d+)\s*(?:个)?(?:标包|包)\s*(?:的)?\s*评标办法")
TECH_TOTAL = re.compile(r"技术部分\s*[：:]\s*(?:\$\\underline\{)?\s*(\d+(?:\.\d+)?)")
MAX_SCORE = re.compile(r"\d+(?:\.\d+)?")
END_HEADING = re.compile(r"[二三四五六]、.*评标办法正文")
OVERVIEW = re.compile(r"哪些评分项|各项最高多少分|评分项(?:及|和)满分")
PACKAGE_PHRASE = rf"(?:第\s*{ORDINAL}\s*(?:标包|标段|包)|(?:采购包|合同包|标包|标段)\s*{ORDINAL})"
SCOPE = rf"(?:{PACKAGE_PHRASE}|(?:所有|各)(?:标包|标段|包)|技术部分)"
SCOPE_OVERVIEW = re.compile(rf"{SCOPE}(?:的)?(?:技术部分)?(?:怎么评分|如何评分|评分标准(?:是什么)?)[？?]?")
PACKAGE_FOLLOWUP = re.compile(rf"(?:那|那么|再看)?\s*{PACKAGE_PHRASE}\s*(?:呢|的呢)?[？?]?")


def is_scoring_overview(question):
    return bool((OVERVIEW.search(question) or SCOPE_OVERVIEW.fullmatch(question))
                and not re.search(r"商务|价格|变更|更正|调整|评分项.*(?:条件|材料)", question))


def scoring_scope(question, previous):
    if is_scoring_overview(question) or (PACKAGE_FOLLOWUP.fullmatch(question) and is_scoring_overview(previous)):
        scoped = re.sub(rf"第\s*({ORDINAL})\s*标段", lambda match: f"第{match[1]}包", question)
        return comparison_packages(scoped.replace("合同包", "采购包"))
    return None


def package_sections(pages):
    """Split PDF text at explicit section headings, including headings mid-page."""
    sections = {}
    current = None
    for page, text in enumerate(pages, 1):
        for raw in text.splitlines():
            line = unicodedata.normalize("NFKC", raw).strip()
            if heading := HEADING.fullmatch(line):
                package = int(heading[1])
                if package in sections:
                    return {}
                current = sections.setdefault(package, {})
            elif END_HEADING.match(line):
                current = None
            elif current is not None and line and line != str(page):
                current.setdefault(page, []).append(line)
    return {package: {page: "\n".join(lines) for page, lines in section.items()}
            for package, section in sections.items() if section}


def score_value(value):
    return float(value) if "." in value else int(value)


def row_source(unit, description, section):
    # Match a meaningful prefix within this package, since MinerU may assign
    # the merged table's starting page to an individual row.
    prefix = normalize(description)[:18]
    if len(prefix) < 8:
        return None
    pages = [page for page, text in section.items() if prefix in normalize(text)]
    if len(pages) != 1:
        return None
    return {**source_from(unit), "page": pages[0], "page_verified": True}


def split_merged_team(description, score, section, package):
    if not isinstance(score, int):
        return []
    fragments = [(page, line.strip()) for page, text in section.items() for line in text.splitlines()]
    head = normalize(description)[:18]
    start = next((index for index, (_, line) in enumerate(fragments)
                  if head in normalize(line)), None)
    if start is None:
        return []
    native = fragments[start:]
    first_page, first_line = native[0]
    first = re.search(r"[。.)）]\s*(\d+)$", first_line)
    if not first:
        return []
    for index, (page, line) in enumerate(native[1:], 1):
        if not line.isdigit() or first[1] + line != str(score):
            continue
        continuation = "\n".join(text for _, text in native[1:index + 1])
        if not re.search(rf"(?:最高|最多)得\s*{line}\s*分", continuation):
            continue
        return [
            (int(first[1]), {"id": f"P{first_page}-score-{package}-team-size",
                            "page": first_page, "text": first_line, "page_verified": True, "origin": "pdf_text"}),
            (int(line), {"id": f"P{page}-score-{package}-team-cert",
                        "page": page, "text": continuation, "page_verified": True, "origin": "pdf_text"}),
        ]
    return []


def table_items(units, section, package):
    items = {}
    active = False
    for unit in units:
        if unit.get("row") is None:
            continue
        cells = unit["text"].split(" | ")
        if cells[:2] == ["6.3.5", "技术部分评分标准"]:
            cells = cells[2:]
            active = True
        if not active:
            continue
        if re.match(r"\d+\.\d+", cells[0]) or not MAX_SCORE.fullmatch(cells[-1]):
            break
        score = score_value(cells[-1])
        if len(cells) == 2 and "项目团队" in items and "团队管理方案" in cells[0]:
            title, description = "项目团队", cells[0]
        elif len(cells) in (3, 4):
            title = re.split(r"\(\d+\)未提供", cells[0], maxsplit=1)[0].strip()
            description = cells[1]
        else:
            break
        source = row_source(unit, description, section)
        if not source:
            continue
        item = items.setdefault(title, {"name": title, "parts": [], "sources": []})
        item["parts"].append(score)
        item["sources"].append(source)
        if title == "项目团队" and "人员数量" in description and "最多得" in description:
            split = split_merged_team(description, score, section, package)
            if split:
                item["parts"][-1:] = [value for value, _ in split]
                item["sources"][-1:] = [proof for _, proof in split]
    for item in items.values():
        item["max_score"] = round(sum(item["parts"]), 6)
    return list(items.values())


def technical_scores(units, sections):
    anchors = []
    for index, unit in enumerate(units):
        if unit.get("row") and unit["text"].startswith("6.3.1 |") and (match := TECH_TOTAL.search(unit["text"])):
            anchors.append((index, unit["page"], score_value(match[1])))
    results = {}
    for package, section in sections.items():
        total_match = TECH_TOTAL.search("\n".join(section.values()))
        total = score_value(total_match[1]) if total_match else None
        matches = [offset for offset, (_, page, score) in enumerate(anchors)
                   if section and page == min(section) and score == total]
        items = []
        if len(matches) == 1:
            offset = matches[0]
            end = anchors[offset + 1][0] if offset + 1 < len(anchors) else len(units)
            items = table_items(units[anchors[offset][0]:end], section, package)
        complete = bool(items) and round(sum(item["max_score"] for item in items), 6) == total
        results[package] = {"max_score": total, "complete": complete, "items": items, "score_kind": "technical"}
    return results


def package_score_answer(units, pages, requested, known_packages):
    sections = package_sections(pages)
    scores = technical_scores(units, sections) if sections else overall_scores(units, pages)
    if not scores:
        return None
    scopes = sorted(requested or (set(scores) | set(known_packages)))
    results = []
    sources = {}
    lines = []
    for package in scopes:
        entry = scores.get(package, {"max_score": None, "complete": False, "items": []})
        results.append({"package": package, **entry})
        items, total, complete = entry["items"], entry["max_score"], entry["complete"]
        if items:
            label = "技术部分满分" if entry["score_kind"] == "technical" else "综合评分满分（含报价）"
            lines.append(f"第 {package} 包｜{label} {total:g} 分")
            if entry["score_kind"] == "overall":
                lines.append("原文按综合评分列项，以下分值包含报价。")
            if len(entry.get("shared_packages", ())) > 1:
                shared = "、".join(map(str, entry["shared_packages"]))
                lines.append(f"第 {shared} 包共用这套标准。")
            for item in items:
                parts = "（" + " + ".join(f"{value:g}" for value in item["parts"]) + "）" if len(item["parts"]) > 1 else ""
                pages_text = "、".join(str(page) for page in sorted({row["page"] for row in item["sources"]}))
                lines.append(f"• {item['name']}：{item['max_score']:g} 分{parts}（PDF 第 {pages_text} 页）")
                sources.update((row["id"], row) for row in item["sources"])
        if not complete:
            lines.append(f"第 {package} 包待复核：评分表范围、满分或评分项未能核齐。")
    if not sources:
        return {"status": "needs_review", "reason": "\n".join(lines), "package_scores": results}
    return {"status": "answered", "answer": "\n".join(lines), "conditions": [],
            "sources": list(sources.values()), "package_scores": results,
            "partial": any(not row["complete"] for row in results)}


SCOPE_TOKEN = r"(?:采购包|合同包|标包|标段)\s*(\d+|[一二三四五六七八九十])"
SCOPE_LINE = re.compile(rf"{SCOPE_TOKEN}(?:\s*[、,]\s*{SCOPE_TOKEN})*\s*[:：]?")
TRAILING_SCOPE = re.compile(r"([0-9]+(?:\s*[、,]\s*[0-9]+)*)\s*(?:标包|标段)\s*[:：]")
NUMBERED_FACTOR = re.compile(r"([1-9])、\s*([^：:()\d]{2,20}?)\s*\(?\s*(\d+(?:\.\d+)?)\s*分\s*\)?")
NAMED_FACTOR = re.compile(r"([^()]+)\(\s*(\d+(?:\.\d+)?)\s*分\)")
SCORE_CONTEXT = re.compile(r"综合评分法|评分标准|评分因素|评审内容及分值构成")
CHAPTER_HEADING = re.compile(r"^第\s*[一二三四五六七八九十0-9]+\s*章(?:\s|[^。])")


def overall_scope(line):
    if match := TRAILING_SCOPE.fullmatch(line):
        return {int(value) for value in re.findall(r"\d+", match[1])}
    stripped = line.strip("【】[]")
    if not SCOPE_LINE.fullmatch(stripped):
        return set()
    return {int(value) if value.isdigit() else "一二三四五六七八九十".index(value) + 1
            for value in re.findall(SCOPE_TOKEN, stripped)}


def overall_sections(pages):
    sections = []
    section = None
    for page, text in enumerate(pages, 1):
        for raw in text.splitlines():
            line = unicodedata.normalize("NFKC", raw).strip()
            if CHAPTER_HEADING.match(line):
                section = None
            elif scope := overall_scope(line):
                section = (scope, {})
                sections.append(section)
            elif section and line and not line.startswith("第") and line != str(page):
                section[1].setdefault(page, []).append(line)
    return sections


def numbered_factors(section):
    rows = []
    for page, lines in section.items():
        for line in lines:
            if match := NUMBERED_FACTOR.fullmatch(line):
                rows.append((int(match[1]), match[2].strip(), score_value(match[3]), page, line))
    if ([row[0] for row in rows[:4]] != [1, 2, 3, 4]
            or rows[0][3] > min(section) + 1 or not re.search("报价|价格", rows[0][1])):
        return []
    return [{"name": name, "parts": [score], "max_score": score,
             "sources": [{"id": f"P{page}-factor-{number}", "page": page, "text": line,
                          "page_verified": True, "origin": "pdf_text"}]}
            for number, name, score, page, line in rows[:4]]


def table_factors(table, section):
    items = {}
    active = None
    for unit in table:
        cells = unit["text"].split(" | ")
        label = next((NAMED_FACTOR.fullmatch(cell) for cell in cells[:-1]
                      if NAMED_FACTOR.fullmatch(cell)), None)
        if label:
            name, total = label[1].strip(), score_value(label[2])
            active = items.setdefault(name, {"name": name, "parts": [], "max_score": total, "sources": []})
        if not active or len(cells) < 2:
            continue
        description = cells[-1]
        source = row_source(unit, description, section)
        if not source:
            continue
        part = cells[-2]
        if MAX_SCORE.fullmatch(part):
            active["parts"].append(score_value(part))
        elif label and len(cells) == 2:
            active["parts"].append(active["max_score"])
        else:
            continue
        active["sources"].append(source)
    return list(items.values())


def overall_scores(units, pages):
    tables = {}
    for unit in units:
        if unit.get("row") is not None:
            tables.setdefault(unit["evidence_id"], []).append(unit)
    scores = {}
    for scope, fragments in overall_sections(pages):
        section = {page: "\n".join(lines) for page, lines in fragments.items()}
        # The scope label must precede a complete scoring overview, rather than
        # a similarly labelled procurement requirement elsewhere in the file.
        if not section:
            continue
        start = min(section)
        nearby = " ".join(pages[max(0, start - 2):start])
        if not SCORE_CONTEXT.search(nearby):
            continue
        items = numbered_factors(fragments)
        if not items:
            candidates = [table_factors(table, section) for table in tables.values()
                          if start in {unit["page"] for unit in table}]
            matches = [rows for rows in candidates if sum(row["max_score"] for row in rows) == 100]
            if len(matches) != 1:
                continue
            items = matches[0]
        if not items or sum(item["max_score"] for item in items) != 100:
            continue
        # Treat a missing or inconsistent subrow as an incomplete breakdown.
        complete = all(item["sources"] and sum(item["parts"]) == item["max_score"] for item in items)
        for package in sorted(scope):
            if package in scores:
                return {}
            scores[package] = {"max_score": 100, "complete": complete, "items": items,
                               "score_kind": "overall", "shared_packages": sorted(scope)}
    return scores
