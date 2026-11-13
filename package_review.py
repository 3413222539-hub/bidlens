import re
import unicodedata
from decimal import Decimal

from risk_review import read_risk_inputs, source_from
from retrieve_evidence import normalize, row_locator


DIGITS = "一二三四五六七八九十"
ORDINAL = rf"(?:[0-9]{{1,3}}|[{DIGITS[:-1]}]?十[{DIGITS[:-1]}]?|[{DIGITS[:-1]}])"
PACKAGE = re.compile(rf"第\s*({ORDINAL})\s*(?:标)?包|(?:采购包|标包|标段)\s*({ORDINAL})(?!\d)|(?<![\d.])({ORDINAL})\s*标段(?=\s*[:：])")
VALUE = r"\d+(?:,\d{3})*(?:\.\d+)?"
MONEY = re.compile(rf"(?:[¥￥]\s*({VALUE})|({VALUE})\s*(万元|元))")
NO_DEPOSIT = re.compile(r"[☑✓√]\s*(?:不需要|不收取|不提交)|(?:本项目)?(?:不收取|无需提交|不需要提交)(?:投标)?保证金|(?:^|[|:：])\s*不提交(?:[,.，。;；]|$)")
METHOD = re.compile(r"[☑✓√]\s*(综合评分法|最低评标价法)")
CONTRACT = re.compile(r"^合同包\s*([0-9]{1,3})\s*(?:\([^)]*\))?\s*[:：]$")


def standard(text):
    return unicodedata.normalize("NFKC", text).replace("\\", "")


def package_number(match):
    value = next(group for group in match.groups() if group)
    if value.isascii():
        return int(value)
    if "十" in value:
        tens, ones = value.split("十", 1)
        return (DIGITS.index(tens) + 1 if tens else 1) * 10 + (DIGITS.index(ones) + 1 if ones else 0)
    return DIGITS.index(value) + 1


def package_set(text):
    matches = list(PACKAGE.finditer(text))
    numbers = {package_number(match) for match in matches}
    for left, right in zip(matches, matches[1:]):
        if re.fullmatch(r"\s*[~～—至到-]\s*", text[left.end():right.start()]):
            numbers.update(range(package_number(left), package_number(right) + 1))
    return numbers


def unique_sources(sources):
    return list({source["id"]: source for source in sources}.values())


def stated_amount(text):
    match = MONEY.search(text)
    return (match[1] + "元" if match[1] else match[2] + match[3]) if match else None


def yuan(amount):
    value = Decimal(amount.removesuffix("万元").removesuffix("元").replace(",", ""))
    return value * 10000 if amount.endswith("万元") else value


def single_amounts(amounts):
    return {yuan(amount): amount for amount in reversed(list(amounts))}


def package_spans(text):
    markers = list(PACKAGE.finditer(text))
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        span = re.split(r"形式|收受人|特别提醒", text[marker.end():end], maxsplit=1)[0]
        yield package_number(marker), span[:70]


def amounts_by_package(text):
    return {number: amount for number, span in package_spans(text) if (amount := stated_amount(span))}


def deposits_by_package(text):
    values = {}
    for number, span in package_spans(text):
        amount = stated_amount(span)
        waived = bool(re.match(r"\s*[:：]?\s*(?:免收|不收取|不提交|不需要|无需)", span))
        if amount or waived:
            values[number] = amount, waived
    return values


def submission_quotes(text):
    numbered = re.findall(r"[12]\)\s*(以(?:转账|支票).+?)(?=[12]\)\s*以(?:转账|支票)|$)", text)
    if numbered:
        return numbered
    phrases = (
        r"以(?:银行)?转账(?:方式|形式)(?:交纳|缴纳|提交)投标保证金[^。；;]{1,170}?(?=[,，]查询电话|[。；;]|$)",
        r"以(?:电子)?保函(?:方式|形式)(?:交纳|缴纳|提交)投标保证金.{1,650}?(?=\([2-9]\)|$)",
    )
    return [match[0].strip(" ;；") for pattern in phrases if (match := re.search(pattern, text))]


def read_package_facts(folder, inputs=None):
    units, pdf_text = inputs if inputs is not None else read_risk_inputs(folder / "evidence.json", folder / "document.pdf")
    packages, deposits, limits, licenses, methods = set(), [], [], [], []
    locate = row_locator([normalize(page) for page in pdf_text])
    contract = None
    table_columns = {}
    deposit_heading = None
    pending_heading = None
    for position, unit in enumerate(units):
        source = source_from(unit)
        text = standard(source["text"])
        page = source["page"]
        fields = text.split(" | ")
        label = fields[1].strip() if len(fields) >= 3 else ""
        if page <= 25 and unit["row"] == 1 and fields[0] in {"采购包", "合同包", "标包"}:
            table_columns[unit["evidence_id"]] = (fields, source)
        elif page <= 25 and (header := table_columns.get(unit["evidence_id"])) and len(fields) == len(header[0]) and fields[0].isdigit():
            number = int(fields[0])
            packages.add(number)
            native = normalize(pdf_text[page - 1])
            for column, value in zip(header[0], fields):
                if not re.fullmatch(VALUE, value) or not ("金额单位人民币元" in native or "元" in column):
                    continue
                amount = value + "元"
                if column.startswith("采购包最高限价") or column.startswith("最高限价"):
                    limits.append(([source, header[1]], {number: amount}))
                if column.startswith("投标保证金"):
                    deposits.append({"source": {**source, "text": header[1]["text"] + "\n" + source["text"]},
                                     "values": {number: (amount if yuan(amount) else None, not yuan(amount))},
                                     "waiver": False, "amount": None, "text": text, "branches": []})
        if page <= 25 and (label in {"项目名称", "招标范围", "采购预算", "最高限价"}
                           or page <= 10 and len(text) < 280 and PACKAGE.search(text)):
            packages.update(package_set(text))
        if page <= 25 and (heading := CONTRACT.fullmatch(text)):
            number = int(heading[1])
            packages.add(number)
            contract = number, source, position
        explicit_deposit = re.search(r"(?:投标保证金(?:的)?金额(?:为)?|须交纳保证金为)\s*[:：]?\s*(?:[¥￥\d壹贰叁肆伍陆柒捌玖拾万]|无(?:[。.;；]|$))", text)
        heading_waiver = bool(deposit_heading and position == deposit_heading[0] + 1
                              and page == deposit_heading[1]["page"] and text.strip().rstrip("。.") == "无")
        if heading_waiver:
            source = {**source, "text": deposit_heading[1]["text"] + "\n" + source["text"]}
        if page <= 25 and (label.startswith("投标保证金") or explicit_deposit or heading_waiver
                           or re.match(r"^(?:本项目)?(?:投标保证金[:：]|不收取投标保证金)", text)
                           or len(fields) >= 3 and re.match(r"^投标保证金[:：]", fields[2])):
            values = deposits_by_package(text)
            if any(word in text for word in ("不予退还的其他情形", "投标保证金可以不予退还")) and not explicit_deposit:
                continue
            amount = stated_amount(text[explicit_deposit.start():explicit_deposit.start() + 75]) if explicit_deposit else None
            waiver = (heading_waiver or bool(NO_DEPOSIT.search(text)) or bool(explicit_deposit and explicit_deposit[0].endswith("无"))
                      or bool(amount and yuan(amount) == 0))
            branches = []
            for index, quote in enumerate(submission_quotes(text), 1):
                branch_page, verified = locate(quote, page)
                branches.append({"text": quote, "source": {**source, "id": f"{source['id']}-B{index}",
                                 "text": quote, "page": branch_page, "page_verified": verified}})
            deposits.append({"source": source, "values": values, "waiver": waiver, "amount": amount, "text": text, "branches": branches})
        if re.fullmatch(r"(?:\d+[、. ]+)?投标保证金[:：]?", text.strip()):
            deposit_heading = position, source
            pending_heading = source
        limit_label = re.search(r"(?:项目)?最高限价(?:\(如有\))?\s*[:：]", text)
        if page <= 25 and (label == "最高限价" or limit_label and not text.startswith("合同包最高限价") and not unit["row"]):
            value_text = fields[2] if label == "最高限价" else text[limit_label.end():]
            values = amounts_by_package(value_text)
            if not values and not PACKAGE.search(value_text) and (amount := stated_amount(value_text[:40])):
                values = {None: amount}
            packages.update(number for number in values if number is not None)
            limits.append(([source], values))
        elif (contract and position - contract[2] <= 4 and page <= contract[1]["page"] + 1
              and text.startswith("合同包最高限价") and (amount := stated_amount(text))):
            limits.append(([source, contract[1]], {contract[0]: amount}))
        if page <= 12 and re.match(rf"^(?:\d+(?:\.\d+)*[.、 ]+)?标段\s*{ORDINAL}", text) and "须具有" in text:
            names = re.findall(r"《([^》]+许可证)》", text)
            scope = package_set(text)
            if scope and names:
                licenses.append((source, scope, names))
        if page <= 70 and (match := METHOD.search(text)) and "评标方法" in text:
            methods.append((source, match[1]))
    if pending_heading and not any(rule["source"]["page"] == pending_heading["page"] for rule in deposits):
        deposits.append({"source": pending_heading, "values": {}, "waiver": False,
                         "amount": None, "text": pending_heading["text"], "branches": [], "heading_only": True})
    return packages, deposits, limits, licenses, methods


# 条款自身带适用范围/触发条件，需人工判断本标包是否落入。
# 注意：「如有虚假」「如有遗漏」里的「如有」是条件从句，不是范围限定，
# 因此只在「法规/标准/产品/要求」等名词前才算限定。
CONDITIONAL = (
    r"如(?:本)?(?:须知|招标文件|前附表)(?:中)?规定适用",
    r"如设置最高限价",
    r"如(?:有)?(?:进口)?产品(?:属于|为|涉及)",
    r"若采用(?:资格预审|电子招标|联合体|分包)",
    r"仅(?:适用|限)",
    r"(?:货物|工程|服务)采购项目",
)


def limited_to_method(text, chosen_methods):
    """条款把适用范围限定在某个评标方法或采购类型上时返回 True。"""
    if chosen_methods and re.search(r"(?:采用|适用|仅).{0,6}(?:最低评标价法|综合评分法)", text):
        mentioned = set(re.findall(r"(最低评标价法|综合评分法)", text))
        if mentioned and not mentioned & chosen_methods:
            return True
    return False


def is_conditional(text):
    """条款是否自带「如…适用」「如设置…」这类前置条件。"""
    return any(re.search(pattern, text) for pattern in CONDITIONAL)


def is_deposit_risk(item):
    text = standard(item["sources"][0]["text"])
    return ("保证金" in item["title"] and not re.search(r"串通|退还|没收|账户转出", item["title"])
            and bool(re.search(r"投标保证金\s*\|\s*|投标保证金(?:金额|形式|有效期)|未[^。；]{0,26}(?:交纳|缴纳|提交|提供)(?:投标)?保证金", text)))


def checklist_card(key, title, state, condition, check, sources, **extra):
    return {"id": key, "number": "", "group": "标包自查", "title": title, "applicability": state,
            "condition": condition, "check": check, "sources": unique_sources(sources), "references": [], **extra}


def deposit_card(deposits, items, selected, requires_selection):
    if not items and not any(not rule.get("heading_only") for rule in deposits):
        return None
    picked = [(rule["source"], rule["values"][selected][0]) for rule in deposits
              if selected in rule["values"] and rule["values"][selected][0]]
    if not requires_selection:
        picked.extend((rule["source"], amount) for rule in deposits
                      if not rule["values"] and not rule["waiver"] and (amount := rule.get("amount")))
    scoped_waivers = [rule["source"] for rule in deposits if selected in rule["values"] and rule["values"][selected][1]]
    general_waivers = [rule["source"] for rule in deposits
                       if rule["waiver"] and not rule["values"] and not PACKAGE.search(rule["text"])]
    waivers = scoped_waivers + general_waivers
    amounts = single_amounts(amount for _, amount in picked)
    applicable = "pending"
    condition = "请核对本标包是否要求提交投标保证金、金额及提交方式。"
    check = "对照投标人须知前附表和正文。"
    if not amounts and (scoped_waivers or general_waivers and not any(rule["values"] for rule in deposits)):
        applicable, condition, check = "excluded", "文件明确免收投标保证金。", "该项无需准备，仍可核对原文。"
    elif len(amounts) == 1 and not waivers:
        applicable, condition = "applies", f"投标保证金 {next(iter(amounts.values()))}。"
        check = "核对所选标包的金额、提交方式和时限。"
    elif len(amounts) > 1 or waivers and amounts:
        condition = "文件中的保证金金额或是否收取存在冲突，请对照原文。"
    branches = {}
    for rule in deposits:
        if rule["values"] and selected not in rule["values"]:
            continue
        for branch in rule["branches"]:
            branches.setdefault(branch["text"], branch)
    checks = list(dict.fromkeys(
        f"{item['condition']}；{item['check']}" for item in items
        if not PACKAGE.search(standard(item["condition"] + item["check"]))
    )) if applicable != "excluded" else []
    origins = [source for source, _ in picked] + waivers + [source for item in items for source in item["sources"]]
    if applicable == "pending":
        origins = [rule["source"] for rule in deposits if not rule["values"] or selected in rule["values"]] + origins
    return checklist_card("bid-deposit", "投标保证金", applicable, condition, check,
                          origins or [rule["source"] for rule in deposits],
                          branches=list(branches.values()) if applicable == "applies" else [], checks=checks,
                          merged_count=len(items))


def answer_package_fact(question, facts):
    """Resolve a standalone amount question using the same facts as the checklist."""
    if re.fullmatch(r"\s*(?:各标包|各采购包|本项目)?最高限价(?:是)?多少[？?；;，,]\s*是否(?:要求|需要)提交投标保证金[？?]?\s*", question):
        # For a project with no identified packages, these are project-wide facts.
        # Leave multi-package questions to the existing evidence-based workflow.
        if facts[0]:
            return None
        answers = [answer_package_fact(part, facts) for part in ("最高限价", "投标保证金")]
        if all(answer and answer["status"] == "answered" for answer in answers):
            return {"status": "answered", "answer": "\n".join(answer["answer"] for answer in answers),
                    "conditions": [], "sources": unique_sources(
                        source for answer in answers for source in answer["sources"])}
        return next((answer for answer in answers if answer and answer["status"] == "needs_review"), None)
    query = normalize(question)
    scope = PACKAGE.search(query)
    if scope:
        query = query[:scope.start()] + query[scope.end():]
    query = re.sub(r"^(?:请问|本项目|该项目|本标包)+", "", query)
    if re.fullmatch(r"(?:是否收取|是否需要提交|要不要交|需不需要交|需要交)?投标保证金(?:是多少|多少|金额是多少|怎么收取|是否收取|吗)?", query):
        key = "bid-deposit"
    elif re.fullmatch(r"最高限价(?:是多少|多少|金额是多少|是什么)?", query):
        key = "price-limit"
    else:
        return None
    packages = facts[0]
    selected = package_number(scope) if scope else next(iter(packages)) if len(packages) == 1 else None
    if (scope or packages) and selected not in packages:
        return None
    items = package_checklist({"items": []}, facts, selected)["items"]
    card = next((item for item in items if item["id"] == key), None)
    if not card or not card["sources"]:
        return None
    conflict = "存在冲突" in card["condition"] or "原文金额有差异" in card["check"]
    if conflict:
        return {"status": "needs_review", "reason": card["condition"] + " " + card["check"],
                "related_sources": card["sources"]}
    if card["applicability"] == "pending":
        return None
    return {"status": "answered", "answer": card["condition"], "conditions": [],
            "sources": card["sources"]}


def package_checklist(report, facts, selected):
    packages, deposits, limits, licenses, methods = facts
    cards = []
    deposit_items = [item for item in report["items"] if is_deposit_risk(item)]
    single_project = not packages or len(packages) == 1 and selected in packages
    if deposit := deposit_card(deposits, deposit_items, selected, not single_project):
        cards.append(deposit)
    if selected is not None or single_project:
        matching_limits = []
        for sources, values in limits:
            amount = values.get(selected) or (values.get(None) if single_project else None)
            if amount:
                matching_limits.append((sources, amount))
        amounts = single_amounts(amount for _, amount in matching_limits)
        if amounts:
            cards.append(checklist_card("price-limit", "最高限价", "applies" if len(amounts) == 1 else "pending",
                                        "最高限价 " + "／".join(sorted(amounts.values())) + "。",
                                        "核对本标包报价。" if len(amounts) == 1 else "原文金额有差异，请逐一核对。",
                                        [source for sources, _ in matching_limits for source in sources]))
        for source, scope, names in licenses:
            applies = selected in scope
            scope_text = "、".join(map(str, sorted(scope)))
            names_text = "和".join(f"《{name}》" for name in names)
            if applies:
                condition = f"原文适用于第 {scope_text} 标包，要求{names_text}。"
                check = f"准备有效期内的{names_text}。"
            else:
                condition = f"原文适用于第 {scope_text} 标包，要求{names_text}。"
                check = f"本条只对应第 {scope_text} 标包，本包无需准备。"
            cards.append(checklist_card(f"license-{source['id']}", "特定资格许可证",
                                        "applies" if applies else "excluded",
                                        condition, check, [source]))

    chosen_methods = {value for _, value in methods}
    handled = {item["id"] for item in deposit_items} | {source["id"] for source, _, _ in licenses}
    for item in report["items"]:
        if item["id"] in handled:
            continue
        text = standard(item["sources"][0]["text"])
        scope = item.get("scope", "")
        scope_sources = item.get("scope_sources", [])
        # 无效投标与审查标准是通用规则：只要参与了本标包投标，这些条款就适用。
        # 只有原文明确限定了范围、且无法判断本标包是否在内时，才留给人工核对。
        if chosen_methods == {"综合评分法"} and re.search(r"(?:仅|适用于|采用)最低评标价法", text):
            state = "excluded"
            scope = "条款限最低评标价法，本文件勾选综合评分法。"
            scope_sources = [source for source, _ in methods]
        elif scope or limited_to_method(text, chosen_methods) or is_conditional(text):
            state = "pending"
            if not scope:
                scope = "原文带适用范围或前置条件，请确认本标包是否落入该条件。"
        else:
            state = "applies"
        cards.append({**item, "applicability": state, "scope": scope, "scope_sources": scope_sources})
    return {**report, "items": cards, "packages": sorted(packages), "selected": selected}
