import argparse
from collections import Counter, defaultdict
from html.parser import HTMLParser
import json
from math import log1p
from pathlib import Path
import re
from statistics import median
from time import perf_counter_ns
import unicodedata


DATA = Path(__file__).with_name("data")
DOCUMENT = "tender_GT2611P032"
CATEGORIES = {
    "日期": r"截止|开标时间",
    "金额": r"限价|预算|保证金",
    "资格": r"资格|联合体|中小企业|分支机构",
    "评分": r"评分|得分|几分|多少分|满分|测评方案",
}

AMOUNT = r"(?:[¥￥]\s*)?(?:[0-9][0-9,.，]*\s*(?:万\s*)?元|[壹贰叁肆伍陆柒捌玖拾佰仟万]+(?:圆|元)|[¥￥]\s*[0-9][0-9,.，]*)"
# 评分表行/表头特征：分值、满分值、评审因素、评分标准等
SCORE_MARK = (r"评审因素|评分标准|评分细则|满分值|分值构成|分值|满分|[0-9]+(?:\.[0-9]+)?\s*分")
FACT_PATTERNS = (
    (r"保证金", r"投标保证金[^\n]{0,100}?(?:" + AMOUNT + r"|不收取|不提交|不需要|无需|免收)"),
    (r"最高限价|限价", r"最高限价[^\n]{0,44}?(?:" + AMOUNT + r"|[（(]元[)）]\s*[:：]?\s*[0-9][0-9,.，]*)"),
    (r"预算", r"(?:采购预算|项目预算|总预算|预算金额)[^\n]{0,42}?(?:" + AMOUNT + r"|[（(]元[)）]\s*[:：]?\s*[0-9][0-9,.，]*)"),
    (r"投标有效期", r"投标有效期[^\n]{0,36}?[0-9]+\s*(?:日历)?[天日]"),
)


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None
        self.pending = {}

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = {}
            for col, (remaining, text) in list(self.pending.items()):
                self.row[col] = text
                if remaining == 1:
                    del self.pending[col]
                else:
                    self.pending[col] = (remaining - 1, text)
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
            self.attrs = dict(attrs)
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            text = "".join(self.cell).strip()
            colspan = int(self.attrs.get("colspan", "1"))
            rowspan = int(self.attrs.get("rowspan", "1"))
            col = 0
            while any(i in self.row for i in range(col, col + colspan)):
                col += 1
            for i in range(col, col + colspan):
                self.row[i] = text if i == col else ""
                if rowspan > 1:
                    self.pending[i] = (rowspan - 1, self.row[i])
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(" | ".join(self.row[i] for i in sorted(self.row) if self.row[i]))
            self.row = None


def tokens(text):
    normalized = unicodedata.normalize("NFKC", text).lower()
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", normalized):
        if "\u3400" <= part[0] <= "\u9fff":
            yield from (part[i:i + 2] for i in range(max(1, len(part) - 1)))
        else:
            yield part


def row_locator(pdf_pages):
    width = 8
    postings = defaultdict(list)
    for page, text in enumerate(pdf_pages, 1):
        for phrase in {text[i:i + width] for i in range(len(text) - width + 1)}:
            postings[phrase].append(page)

    def locate(row, original_page):
        # The start of a long cell locates the row even when its notes continue on a later page.
        cells = [normalize(cell)[:48] for cell in row.split(" | ")]
        phrases = {cell[i:i + width] for cell in cells for i in range(0, len(cell) - width + 1, width)}
        scores = Counter(page for phrase in phrases for page in postings.get(phrase, ()))
        if scores:
            page, count = scores.most_common(1)[0]
            if count >= 2 and sum(value == count for value in scores.values()) == 1:
                return page, True
            if scores[original_page] >= 2 and scores[original_page] == count:
                return original_page, True
        exact_pages = {page for cell in cells if 12 <= len(cell) < 2 * width
                       for page in postings.get(cell[:width], ()) if cell in pdf_pages[page - 1]}
        if len(exact_pages) == 1:
            return next(iter(exact_pages)), True
        return original_page, False

    return locate


def units_from(blocks, split_tables, pdf_pages=None):
    locate = row_locator(pdf_pages) if pdf_pages is not None else None
    for block in blocks:
        content = block["content"]
        if isinstance(content, list):
            content = " ".join(map(str, content))
        if block["type"] == "table":
            parser = TableRows()
            parser.feed(content)
            rows = parser.rows
            if split_tables and rows:
                for index, row in enumerate(rows, 1):
                    if row:
                        unit = {**block, "text": row, "row": index}
                        if locate is not None:
                            page, verified = locate(row, block["page"])
                            unit.update(page=page, page_verified=verified)
                        yield unit
                continue
            if rows:
                content = "\n".join(rows)
        yield {**block, "text": content, "row": None}


def pdf_text_units(blocks, pdf_pages):
    covered = {block["page"] for block in blocks if normalize(block["text"])}
    for page, text in enumerate(pdf_pages, 1):
        if page in covered or not normalize(text):
            continue
        for index, sentence in enumerate(re.split(r"(?<=。)", re.sub(r"\s+", " ", text).strip()), 1):
            if normalize(sentence):
                yield {"evidence_id": f"P{page}-{index}", "page": page, "row": None,
                       "text": sentence, "origin": "pdf_text", "page_verified": True}


def coverage_gaps(blocks, pdf_pages):
    covered = {block["page"] for block in blocks if normalize(block["text"])}
    return [page for page, text in enumerate(pdf_pages, 1) if page not in covered and not normalize(text)]


def is_heading(unit):
    """标题类证据：很短、没有完整句子，通常是章节号或小节名。

    允许以冒号结尾（「二、申请人的资格要求：」），只要没有句号或分号。
    """
    text = unit["text"].strip()
    return len(text) <= 40 and not re.search(r"[。；;]", text)


# 列表型事实的开头，命中后应带出同页后续分项（如「最高限价：1 标段：…；」）
LIST_START = re.compile(r"(?:最高限价|采购预算|预算金额|投标保证金|各标包|各分包|标包|标段)[^。]{0,20}[:：]")
ROW_HEADER = re.compile(r"序号\s*\|.*(?:评审因素|评分|分值|限价|保证金|标的名称|条款号)")


class SearchIndex:
    def __init__(self, units):
        self.units = list(units)
        self.postings = {}
        lengths = []
        for index, unit in enumerate(self.units):
            counts = Counter(tokens(unit["text"]))
            length = sum(counts.values())
            lengths.append(length)
            for term, count in counts.items():
                self.postings.setdefault(term, []).append((index, count))
        average = sum(lengths) / max(1, len(lengths)) or 1
        self.norms = [1.5 * (0.25 + 0.75 * length / average) for length in lengths]
        self.positions = {(unit["evidence_id"], unit.get("row")): index
                          for index, unit in enumerate(self.units)}
        self.tables = defaultdict(list)
        self.table_headers = {}
        for index, unit in enumerate(self.units):
            if unit.get("row") is not None:
                self.tables[unit["evidence_id"]].append(index)
        # 同一张跨页表被切成多个证据块时，靠表头签名把它们归为一族
        for evidence_id, indexes in self.tables.items():
            header = next((self.units[i]["text"] for i in indexes
                           if self.units[i].get("row") in {1, 2}), "")
            if header:
                self.table_headers[evidence_id] = re.sub(r"\s+", "", header)[:60]
        self.table_families = defaultdict(list)
        for evidence_id, signature in self.table_headers.items():
            self.table_families[signature].append(evidence_id)

    def expand_table(self, unit, budget=9000):
        """命中表格时带出相关数据行。

        - 命中表头（第 1、2 行）：带出整张表；表头签名相同的跨页切片一并带出。
        - 命中评分表的数据行：整表带出，否则只能看到零星一两项评分标准。
        """
        evidence_id = unit["evidence_id"]
        signature = self.table_headers.get(evidence_id)
        if signature is None:
            return []
        family = self.table_families[signature]
        is_header_hit = unit.get("row") in {1, 2}
        # 评分表未必在表头写「分值」，也可能写成「条款号 | 条款内容 | 编列内容」。
        # 因此同时看命中行自身是否带分值与评分标准。
        is_score_row = bool(re.search(r"分值|满分|评分标准|评审因素", signature + unit["text"]))
        if not is_header_hit and not is_score_row:
            return []
        members = set(family) if len(family) <= 8 else {evidence_id}
        expanded, chars = [], 0
        for member in sorted(members):
            for index in self.tables.get(member, []):
                row = self.units[index]
                expanded.append(row)
                chars += len(row["text"])
                if chars > budget:
                    return expanded
        return expanded

    def expand_list(self, unit, budget=900):
        """「最高限价：1 标段：…」这类列表开头带出同页后续分项。"""
        if not LIST_START.search(unit["text"]):
            return []
        start = self.positions.get((unit["evidence_id"], unit.get("row")))
        if start is None:
            return []
        extra = []
        chars = 0
        for follow in self.units[start + 1:]:
            if follow["page"] != unit["page"] or follow.get("row") is not None:
                break
            if is_heading(follow) or len(follow["text"]) > 120:
                break
            extra.append(follow)
            chars += len(follow["text"])
            if chars > budget:
                break
        return extra

    def contextualize(self, hits, budget=1800):
        """标题命中时把同页后续正文一起带出，避免只有标题没有内容。"""
        ordered = []
        seen = set()

        def add(unit):
            key = (unit["evidence_id"], unit.get("row"))
            if key not in seen:
                seen.add(key)
                ordered.append(unit)

        for unit in hits:
            add(unit)
            for extra in self.expand_table(unit):
                add(extra)
            for extra in self.expand_list(unit):
                add(extra)
            if not is_heading(unit):
                continue
            start = self.positions.get((unit["evidence_id"], unit.get("row")))
            if start is None:
                continue
            chars = 0
            for follow in self.units[start + 1:]:
                if follow["page"] != unit["page"] or follow.get("row") is not None:
                    break
                chars += len(follow["text"])
                if chars > budget:
                    break
                add(follow)
        return ordered

    def search(self, question, boost=False, limit=5, required=None, expand=True):
        scores = {}
        category = next((pattern for pattern in CATEGORIES.values() if re.search(pattern, question)), None)
        for term in dict.fromkeys(tokens(question)):
            matches = self.postings.get(term, [])
            weight = log1p((len(self.units) - len(matches) + 0.5) / (len(matches) + 0.5))
            for index, count in matches:
                scores[index] = scores.get(index, 0) + weight * count * 2.5 / (count + self.norms[index])
        if boost and category:
            for index in scores:
                if re.search(category, self.units[index]["text"]):
                    scores[index] *= 1.2
        if required:
            scores = {index: score for index, score in scores.items() if re.search(required, self.units[index]["text"])}
        ranked = sorted(scores, key=lambda index: (-scores[index], index))[:limit]
        hits = [self.units[index] for index in ranked]
        return self.contextualize(hits) if expand else hits


class RoutedSearch:
    def __init__(self, whole, rows):
        self.whole = whole
        self.rows = rows

    def search(self, question, boost=True, limit=8):
        # Route date and scoring questions to the finer index. Keep larger
        # blocks for other queries so unrelated short rows do not crowd them out.
        use_rows = any(re.search(CATEGORIES[key], question) for key in ("评分", "日期"))
        index = self.rows if use_rows else self.whole
        baseline = index.search(question, boost, limit)
        extra = []
        if boost and re.search(CATEGORIES["评分"], question):
            # 评分问题：评分表里很少出现「技术部分」这类问法，直接按分值特征召回
            scored = self.rows.search(question, boost, limit=6, required=SCORE_MARK)
            extra.extend(scored)
        pattern = next((pattern for intent, pattern in FACT_PATTERNS if re.search(intent, question)), None)
        if boost and pattern:
            extra.extend(self.rows.search(question, boost, limit=6, required=pattern))
            # 金额类问题同时问限价和保证金时，确保两类事实都在候选里
            for intent, fact in FACT_PATTERNS[:2]:
                if re.search(intent, question) and fact != pattern:
                    extra.extend(self.rows.search(f"{question} {intent}", boost, limit=4, required=fact))
        if not extra:
            return baseline
        merged, seen = [], set()
        expansions = self.rows.contextualize(extra)
        for unit in extra + expansions + baseline:
            key = (unit["evidence_id"], unit["row"])
            if key not in seen:
                seen.add(key)
                merged.append(unit)
        return merged


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).lower() if c.isalnum())


def evaluate(index, cases, boost):
    found = []
    times = []
    for case in cases:
        # Gold evidence participates only in evaluation, never in scoring.
        expected = [normalize(part) for part in case["evidence"].split("；")]
        start = perf_counter_ns()
        if isinstance(index, RoutedSearch):
            results = index.search(case["question"], boost)
        else:
            results = index.search(case["question"], boost, expand=False)
        times.append((perf_counter_ns() - start) / 1_000_000)
        match = next(
            ((rank, unit) for rank, unit in enumerate(results, 1)
             if unit["page"] == case["page"]
             and all(part in normalize(unit["text"]) for part in expected)),
            None,
        )
        found.append((case, match))
    return found, median(times)


def main():
    parser = argparse.ArgumentParser(description="招标文件证据检索与基线对比")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--mode", choices=["table", "row", "boost", "route"], default="route")
    args = parser.parse_args()
    blocks = json.loads((DATA / f"{DOCUMENT}_evidence.json").read_text(encoding="utf-8-sig"))
    whole = SearchIndex(units_from(blocks, False))
    rows = SearchIndex(units_from(blocks, True))
    routed = RoutedSearch(whole, rows)
    if args.question:
        index = {"table": whole, "row": rows, "boost": rows, "route": routed}[args.mode]
        for unit in index.search(args.question, args.mode in {"boost", "route"}):
            row = f" 行 {unit['row']}" if unit["row"] is not None else ""
            print(f"{unit['evidence_id']} PDF 第 {unit['page']} 页{row}\n{unit['text']}\n")
        return
    cases = json.loads((DATA / f"{DOCUMENT}_gold.json").read_text(encoding="utf-8-sig"))["cases"]
    scoring_total = sum(case["category"] == "评分" for case in cases)
    for label, index, boost in [("整表", whole, False), ("按行", rows, False),
                                ("按行+加权", rows, True), ("按问题选索引+加权", routed, True)]:
        found, elapsed = evaluate(index, cases, boost)
        matched = [(case, match) for case, match in found if match]
        row_hits = sum(unit["row"] is not None for case, (_, unit) in matched if case["category"] == "评分")
        first = sum(rank == 1 for _, (rank, _) in matched)
        print(f"\n{label} | 证据覆盖 {len(matched)}/{len(cases)}"
              f" | 首位覆盖 {first}/{len(cases)} | 评分行 {row_hits}/{scoring_total} | 中位耗时 {elapsed:.2f} ms")
        for case, match in found:
            detail = "未命中"
            if match:
                rank, unit = match
                detail = f"第 {rank} 名 {unit['evidence_id']} 页 {unit['page']} 行 {unit['row'] or '-'}"
            print(f"  {case['id']} {detail}")


if __name__ == "__main__":
    main()
