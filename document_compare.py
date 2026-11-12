import re
from collections import defaultdict
from datetime import datetime

from package_review import VALUE, package_set, standard, yuan
from retrieve_evidence import pdf_text_units
from risk_review import source_from


LIMIT = "最高限价"
DEADLINE = "投标截止时间"
QUALIFICATION = "证照资格要求"
LABELS = {
    LIMIT: r"(?<!单价)(?<!分项)(?:最高投标限价|最高限价)(?!单价)",
    DEADLINE: r"(?:投标截止(?:时间)?|(?:提交|递交)投标文件(?:的)?截止(?:时间)?|(?:投标文件|(?<!澄清)(?<!响应)(?<!招标)文件)(?:的)?(?:提交|递交)(?:的)?截止(?:时间)?)",
}
CHANGE = re.compile(r"(?:更正|变更|修改|调整|延期|顺延)(?:为|至)|(?:现变更|现调整)(?=.{0,8}限价为)|现为")
DATE = re.compile(
    r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?"
    r"\s*(\d{1,2})\s*[:点时]\s*(\d{1,2})(?::(\d{2})|\s*分\s*(\d{1,2})\s*秒)?\s*分?"
)
QUERY_LABELS = {LIMIT: r"限价", DEADLINE: LABELS[DEADLINE] + r"|截止时间",
                QUALIFICATION: r"资格|资质|许可证|登记证|证照"}
AMOUNT = re.compile(rf"(?<![\d.,])(?:[¥￥]\s*({VALUE})\s*(万元|元)?|({VALUE})\s*(万元|元))")
PROJECT_ID = re.compile(r"(?:采购项目编号|(?<!采购)项目编号|招标编号)(?:/包号)?\s*:\s*([A-Z0-9][A-Z0-9/_-]{5,})", re.I)
PACKAGE_AMOUNT = re.compile(rf"(?:采购包|合同包|标包|(?<!标)包)\s*(\d{{1,3}})\s*:\s*({VALUE})\s*(万元|元)?")
CHANGE_LEAD = re.compile(r"^(?:现|再次|现将)?\s*")
LICENSE_NAME = re.compile(r"《([^》]{2,55}(?:许可证|登记证|资质证书))》")
REQUIREMENT = re.compile(r"须|应当|必须|要求|需具|需持|需提供|需提交|具有|具备|持有|提供|提交")
REMOVAL = re.compile(r"删除|取消|不再要求|不再需要|不再提供|无需提供")
ADDITION = re.compile(r"新增|增设|补充要求")
NON_QUALIFICATION = re.compile(r"评分|加分|得分|履约阶段|验收时|中标后")


def comparison_request(question, previous=""):
    if not re.search(r"变更|改了|改到|修改|调整|变化|对比|比对|更正|延后|延期|变了|改吗|还需要|还要|是否仍|澄清后|取消|删除|新增", question):
        return None
    context = question if any(re.search(label, question) for label in QUERY_LABELS.values()) else previous
    fields = [field for field, label in QUERY_LABELS.items() if re.search(label, context)]
    if not fields and (re.search(r"资格|评分|保证金|预算|联系人", question)
                       or not re.search(r"变更|更正|变化|对比|比对|改了什么|改了哪些", question)):
        return None
    return fields or list(QUERY_LABELS), comparison_packages(question) or comparison_packages(context)


def comparison_packages(text):
    text = standard(text)
    packages = package_set(text)
    packages.update(int(match) for match in re.findall(r"(?:^|[\s、,其中])包\s*(\d{1,3})(?!\d)", text))
    for group in re.findall(r"第\s*(\d{1,3}(?:\s*[、,]\s*\d{1,3})+)\s*(?:标)?包", text):
        packages.update(int(number) for number in re.findall(r"\d+", group))
    return packages


def values(field, text):
    if field == LIMIT:
        # OCR can insert a space inside an amount such as "4847902.0 0元".
        text = re.sub(r"(?<=\d[.])\s+(?=\d)|(?<=\d)\s+(?=\d+\s*(?:万元|元))", "", text)
        amounts = [(match[1] + (match[2] or "元")) if match[1] else match[3] + match[4]
                   for match in AMOUNT.finditer(text)]
        return [(yuan(amount), amount) for amount in amounts]
    found = []
    for match in DATE.finditer(text):
        try:
            year, month, day, hour, minute, colon_seconds, chinese_seconds = match.groups()
            value = datetime(*map(int, (year, month, day, hour, minute, colon_seconds or chinese_seconds or "0")))
        except ValueError:
            continue
        found.append((value, value.strftime("%Y-%m-%d %H:%M:%S" if value.second else "%Y-%m-%d %H:%M")))
    return found


def read_clauses(units, pages):
    for unit in [*units, *pdf_text_units(units, pages)]:
        source = source_from(unit)
        if unit.get("origin"):
            source["origin"] = unit["origin"]
        yield standard(unit["text"]), source


def original_deadlines(units, pages):
    result = []
    heading = None
    blocks = list(read_clauses(units, pages))
    latest = next((index for index, (text, _) in enumerate(blocks)
                   if text.strip(" :") == "更正后最新公告"), None)
    for text, source in blocks[latest + 1:] if latest is not None else blocks:
        for clause in re.split(r"[。；;\n]", text):
            clause = clause.strip()
            if not clause:
                continue
            if heading and heading[1]["page"] == source["page"] and (
                re.match(r"^(?:\d+[.、]\s*)?时间\s*:", clause) or DATE.match(clause)
            ):
                dates = values(DEADLINE, clause)
                if len(dates) == 1:
                    result.append((package_set(heading[0]), dates[0], [heading[1], source]))
            heading = None
            label = re.search(LABELS[DEADLINE], clause)
            if not label:
                if "前递交投标文件" in clause or "前提交投标文件" in clause:
                    dates = values(DEADLINE, clause)
                    if len(dates) == 1:
                        result.append((package_set(clause), dates[0], [source]))
                continue
            tail = clause[label.end():]
            if re.match(r"\s*(?:之前|之后|前|后|之日起)", tail):
                continue
            dates = values(DEADLINE, tail[:100])
            if len(dates) == 1:
                result.append((package_set(clause), dates[0], [source]))
            elif not dates and re.fullmatch(r"\s*(?:(?:[、,及和]开标时间)?[及和]地点|[、,]开标时间)?[:\s]*", tail):
                heading = clause, source
    return result


def listed_limits(text):
    matches = list(PACKAGE_AMOUNT.finditer(text))
    # Unlabelled package amounts inherit yuan only when the preceding total is in yuan.
    total = values(LIMIT, text[:matches[0].start()]) if matches else []
    inherited = "元" if len(total) == 1 and not total[0][1].endswith("万元") else None
    return {int(match[1]): values(LIMIT, match[2] + (match[3] or inherited))[0]
            for match in matches if match[3] or inherited}


def original_package_limits(units):
    headers = {}
    for unit in units:
        if unit["row"] is None or unit["page"] > 25:
            continue
        text = standard(unit["text"])
        cells = text.split(" | ")
        ref = unit["evidence_id"]
        if "包号" in cells and "包名称" in cells:
            for index, cell in enumerate(cells):
                if re.fullmatch(r"(?:包|采购包)?最高限价\(元\)", cell):
                    headers[ref] = index, cells.index("包名称"), source_from(unit)
        elif ref in headers:
            amount_column, name_column, header = headers[ref]
            if len(cells) <= max(amount_column, name_column):
                continue
            scope = comparison_packages(cells[name_column])
            if len(scope) == 1 and re.fullmatch(VALUE, cells[amount_column]):
                amount = cells[amount_column] + "元"
                yield scope, (yuan(amount), amount), [source_from(unit), header]


def announced_changes(units, pages):
    clauses = [(part.strip(), source) for text, source in read_clauses(units, pages)
               for part in re.split(r"[。；;\n]", text) if part.strip()]
    for index, (clause, source) in enumerate(clauses):
        change = CHANGE.search(clause)
        if not change:
            continue
        before, after = clause[:change.start()], clause[change.end():]
        sources = [source]
        nearby = [(text, ref) for text, ref in clauses[max(0, index - 4):index] if ref["page"] == source["page"]]
        if not after.strip(" :") and index + 1 < len(clauses) and clauses[index + 1][1]["page"] == source["page"]:
            after, next_source = clauses[index + 1]
            sources.append(next_source)
        for field, pattern in LABELS.items():
            prior = before
            refs = sources.copy()
            if not re.search(pattern, prior) and not CHANGE_LEAD.sub("", before).strip():
                for offset in range(len(nearby) - 1, -1, -1):
                    text, ref = nearby[offset]
                    if CHANGE.search(text):
                        break
                    if re.search(pattern, text):
                        prior = text
                        refs.insert(0, ref)
                        for continuation, continuation_ref in nearby[offset + 1:]:
                            if PACKAGE_AMOUNT.match(continuation):
                                prior += " " + continuation
                                refs.append(continuation_ref)
                        break
            label = re.search(pattern, prior)
            if not label or field == LIMIT and re.search(r"分项|单价|小计|暂估价|其他项目", prior):
                continue
            old_text = prior[label.end():]
            if field == LIMIT and (new_packages := listed_limits(after)):
                old_packages = listed_limits(old_text)
                for package, value in new_packages.items():
                    yield field, {package}, old_packages.get(package), value, refs
                continue
            old, new = values(field, old_text), values(field, after)
            if prior != before and not new:
                continue
            scope = comparison_packages(prior + " " + after)
            valid = len(old) <= 1 and len(new) == 1
            yield field, scope, old[0] if len(old) == 1 else None, new[0] if valid else None, refs


def citations(sources, document_id, label):
    return list({source["id"]: {**source, "document_id": document_id, "document_label": label}
                 for source in sources}.values())


def license_changes(original, correction, packages, selected, original_id, correction_id, question):
    original_clauses = []
    for text, source in read_clauses(*original):
        names = set(LICENSE_NAME.findall(text))
        if names and REQUIREMENT.search(text) and not (NON_QUALIFICATION.search(text) or REMOVAL.search(text)):
            original_clauses.append((comparison_packages(text), names, source))

    explicit_names = set(LICENSE_NAME.findall(question))
    known_names = {name for _, names, _ in original_clauses for name in names}
    notices = []
    for text, source in read_clauses(*correction):
        for clause in re.split(r"[。；;\n]", text):
            names = set(LICENSE_NAME.findall(clause))
            if not names or NON_QUALIFICATION.search(clause):
                continue
            known_names.update(names)
            if change := CHANGE.search(clause):
                before, after = clause[:change.start()].strip(" :"), clause[change.end():].strip(" :")
                prior_names = set(LICENSE_NAME.findall(before))
                next_names = set(LICENSE_NAME.findall(after))
                if prior_names and next_names == prior_names and REMOVAL.match(after.strip("“「\" ")):
                    notices.append((clause, before, after, prior_names, names, "删除", source))
                elif prior_names and next_names and REQUIREMENT.search(after) and not REMOVAL.search(after):
                    notices.append((clause, before, after, prior_names, names, "修改", source))
            elif removal := REMOVAL.search(clause):
                if LICENSE_NAME.search(clause[removal.end():]) and not re.search(r"或|或者|任一", clause[removal.end():]):
                    notices.append((clause, "", clause.strip(), names, names, "删除", source))
            elif addition := ADDITION.search(clause):
                if LICENSE_NAME.search(clause[addition.end():]) and REQUIREMENT.search(clause):
                    notices.append((clause, "", clause.strip(), set(), names, "新增", source))

    requested = explicit_names or {name for name in known_names if name in question}
    rows = []
    revisions = defaultdict(list)
    for clause, before, after, prior_names, names, action, source in notices:
        if requested and not requested & names:
            continue
        scope = comparison_packages(clause)
        if selected and scope and not selected & scope:
            continue
        targets = sorted(scope & selected if selected else scope) if scope else (
            sorted(packages) if len(packages) == 1 else [None])
        for package in targets:
            if selected and package is not None and package not in selected:
                continue
            scoped = [(old_names, ref) for old_scope, old_names, ref in original_clauses
                      if package in old_scope or not old_scope and (package is None or packages == {package})]
            matching = [ref for old_names, ref in scoped if prior_names and prior_names <= old_names]
            reason = ""
            if ((selected or len(packages) > 1) and package is None
                    or package is not None and packages and package not in packages):
                reason = "无法确认变更适用的标包。"
            elif action != "新增" and not matching:
                reason = "原招标文件中未核实被变更的证照条款。"
            elif action == "新增" and any(names & old_names for old_names, _ in scoped):
                reason = "原招标文件已出现该证照要求，请核对是否为重复或调整。"
            elif action == "新增" and not scoped:
                reason = "公告明确新增要求，原招标文件的资格条款仍待核对。"
            row = {
                "field": QUALIFICATION, "action": action, "package": package,
                "before": "\n".join(ref["text"] for ref in matching) or before,
                "after": after, "status": "needs_review" if reason else "changed", "reason": reason,
                "original_sources": citations(matching or [ref for _, ref in scoped], original_id, "原招标文件"),
                "correction_sources": citations([source], correction_id, "澄清公告"),
            }
            rows.append(row)
            for name in names:
                revisions[package, name].append(row)
    for related in revisions.values():
        if len({(row["action"], row["after"]) for row in related}) > 1:
            for row in related:
                row.update(status="needs_review", reason="同一标包的证照出现多条变更，请逐条核对。")
    return rows


def compare_documents(facts, original, correction, fields, selected, original_id, correction_id, question=""):
    packages, _, limits, _, _ = facts
    originals = {LIMIT: [(set() if package is None else {package}, (yuan(amount), amount), sources)
                         for sources, amounts in limits for package, amount in amounts.items()],
                 DEADLINE: original_deadlines(*original) if DEADLINE in fields else []}
    if LIMIT in fields:
        originals[LIMIT].extend(original_package_limits(original[0]))
    packages = packages | {package for scope, _, _ in originals[LIMIT] for package in scope}
    grouped = defaultdict(list)
    for field, scope, old, new, sources in announced_changes(*correction):
        if field not in fields or selected and scope and not selected & scope:
            continue
        if scope:
            targets = sorted(scope & selected if selected else scope)
        elif len(packages) == 1:
            targets = sorted(packages)
        else:
            targets = [None]
        for package in targets:
            if selected and package is not None and package not in selected:
                continue
            grouped[field, package].append((old, new, sources))

    rows = []
    for (field, package), entries in grouped.items():
        # Only an explicit package amount, or a single-package original, can provide a scoped price.
        candidates = [
            (value, sources) for scope, value, sources in originals[field]
            if (package in scope if scope else package is None or field == DEADLINE or packages == {package})
        ]
        old_values = {value[0]: value[1] for value, _ in candidates}
        new_values = {value[0]: value[1] for _, value, _ in entries if value}
        claimed_old = {value[0] for value, _, _ in entries if value}
        old_text = " / ".join(old_values.values())
        new_text = " / ".join(new_values.values())
        reason = ""
        if package is not None and packages and package not in packages:
            reason = "原招标文件中未确认该标包。"
        elif package is None and (selected or len(packages) > 1):
            reason = "澄清公告未明确变更适用的标包。"
        elif not old_values:
            reason = "原招标文件中未核实原值。"
        elif len(old_values) > 1 or len(new_values) > 1 or any(value is None for _, value, _ in entries):
            reason = "同一字段出现多个值或无法确定变更后的值。"
        elif claimed_old and claimed_old != set(old_values):
            reason = "澄清公告所述原值与原招标文件不一致。"
        elif set(old_values) == set(new_values):
            reason = "两份文件提取到的值相同。"
        rows.append({
            "field": field, "package": package, "before": old_text, "after": new_text,
            "status": "needs_review" if reason else "changed", "reason": reason,
            "original_sources": citations((source for _, sources in candidates for source in sources), original_id, "原招标文件"),
            "correction_sources": citations((source for _, _, sources in entries for source in sources), correction_id, "澄清公告"),
        })
    if QUALIFICATION in fields:
        rows.extend(license_changes(original, correction, packages, selected, original_id, correction_id, question))
    return {"status": "comparison", "changes": rows}


def project_ids(inputs):
    return {match[1].upper() for text, _ in read_clauses(*inputs) for match in PROJECT_ID.finditer(text)}
