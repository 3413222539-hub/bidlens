"""Run reproducible checks through the current /api/ask handler with local imports."""

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web_app import IMPORTS, Question, ask  # noqa: E402


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_scoring_cases(cross_document=False):
    if cross_document:
        documents = read_json(ROOT / "eval/package_scoring_cross_document_gold.json")
    else:
        documents = [read_json(ROOT / "eval/package_scoring_gold.json")]
    return [{**case, "kind": "标包评分", "prefix": gold["prefix"],
             "scoring_gold": {package: gold["packages"].get(str(package)) for package in case["packages"]}}
            for gold in documents for case in gold["cases"]]


def load_cases():
    manifest = read_json(ROOT / "eval/agent_cases.json")
    legacy = read_json(ROOT / "tests/qa_cases.json")
    cases = []
    for row in legacy:
        if row["prefix"] not in manifest["legacy_prefixes"]:
            continue
        case_id = f'{row["prefix"]}-{row["kind"]}'
        cases.append({
            "id": case_id, "kind": row["kind"],
            "prefix": row["prefix"], "label": row["label"], "question": row["question"],
            "status": "answered", "pages": row["expect_pages"],
            "patterns": [re.escape(normalize(value)) for value in row["must_contain"] if value != "分"],
            **manifest.get("reference_overrides", {}).get(case_id, {}),
        })
    comparisons = read_json(ROOT / "eval/document_compare_pdf_acceptance.json")
    for row in comparisons:
        if row["name"] in manifest["comparison_names"]:
            cases.append({
                "id": row["name"], "kind": "公告比对", "prefix": row["document_id"],
                "question": row["question"], "status": row["status"],
                "correction_id": row["correction_id"], "changes": row["changes"],
            })
    return cases + manifest["cases"]


def normalize(value):
    text = re.sub(r"[\s,，]", "", unicodedata.normalize("NFKC", value))
    return re.sub(r"(?<!\d)(\d{4})(?:年|-)(\d{1,2})(?:月|-)(\d{1,2})(?:日|(?!\d))",
                  lambda match: f"{match[1]}-{int(match[2]):02d}-{int(match[3]):02d}", text)


def resolve_document(prefix):
    paths = sorted(IMPORTS.glob(prefix + "*"))
    if len(paths) != 1:
        return None
    folder = paths[0]
    if all((folder / filename).is_file() for filename in ("document.pdf", "evidence.json")):
        return folder.name
    return None


def citations(rows):
    return {(row.get("document_id"), row["page"]) for row in rows
            if row.get("page_verified", True)}


def compare_checks(expected, actual):
    fields = ("field", "package", "before", "after", "status")
    expected_rows = {tuple(row.get(key) for key in fields): row for row in expected}
    actual_rows = {tuple(row.get(key) for key in fields): row for row in actual}
    checks = {"comparison_values_and_scope": set(expected_rows) == set(actual_rows)}
    if checks["comparison_values_and_scope"] and expected_rows:
        checks["comparison_citation_pages"] = all(
            bool(citations(row[key]) & citations(actual_rows[identity][key]))
            for identity, row in expected_rows.items()
            for key in ("original_sources", "correction_sources")
            if citations(row[key])
        )
    return checks


def scoring_checks(gold, actual):
    scores = {row["package"]: row for row in actual}
    scope_ok = set(gold) == set(scores) and len(actual) == len(scores)
    if not scope_ok:
        return {"package_scope": False}
    values_ok = citations_ok = True
    for package, expected in gold.items():
        row = scores[package]
        if expected is None:
            values_ok &= not row["complete"] and not row["items"] and row["max_score"] is None
            continue
        values_ok &= row["complete"] and row["max_score"] == expected["max_score"]
        if "score_kind" in expected:
            values_ok &= (row.get("score_kind") == expected["score_kind"]
                          and row.get("shared_packages", []) == expected["shared_packages"])
        items = {item["name"]: item for item in row["items"]}
        values_ok &= len(items) == len(row["items"]) and set(items) == {item[0] for item in expected["items"]}
        for name, parts, pages in expected["items"]:
            item = items.get(name, {})
            values_ok &= item.get("parts") == parts and item.get("max_score") == sum(parts)
            citations_ok &= {page for _, page in citations(item.get("sources", []))} == set(pages)
    return {"package_scope": scope_ok, "scoring_items_and_values": values_ok,
            "scoring_citation_pages": citations_ok}


def evaluate(case, result):
    checks = {"status": result.get("status") == case["status"]}
    if "scoring_gold" in case:
        checks.update(scoring_checks(case["scoring_gold"], result.get("package_scores", [])))
    if "changes" in case:
        checks.update(compare_checks(case["changes"], result.get("changes", [])))
    patterns = case.get("patterns", [])
    missing = [pattern for pattern in patterns if not re.search(pattern, normalize(result.get("answer") or ""))]
    if patterns:
        checks["answer_anchors"] = not missing
    if "pages" in case:
        pages = {page for _, page in citations(result.get("sources", []))}
        checks["citation_page_hit"] = bool(pages & set(case["pages"]))
    if "uncovered_pages" in case:
        checks["coverage_warning"] = (result.get("partial") is True
                                      and result.get("uncovered_pages") == case["uncovered_pages"])
    return checks, missing


def run_case(case):
    record = {key: case[key] for key in ("id", "kind", "question")}
    document_id = resolve_document(case["prefix"])
    correction = case.get("correction_id", "")
    if correction:
        correction = resolve_document(correction)
    if not document_id or correction is None:
        return {**record, "outcome": "skipped", "reason": "样本缺失或前缀匹配到多份文件"}
    started = perf_counter()
    try:
        result = ask(Question(document_id=document_id, correction_id=correction,
                              question=case["question"], previous_question=case.get("previous_question", "")))
    except Exception as error:
        return {**record, "outcome": "error", "error_type": type(error).__name__,
                "http_status": getattr(error, "status_code", None),
                "seconds": round(perf_counter() - started, 3)}
    elapsed = round(perf_counter() - started, 3)
    checks, missing = evaluate(case, result)
    steps = result.get("steps", [])
    return {
        **record, "document_id": document_id, "correction_id": correction or None,
        "previous_question": case.get("previous_question"), "seconds": elapsed,
        "route": "agent" if "steps" in result else "rules", "status": result.get("status"),
        "outcome": "pass" if all(checks.values()) else "fail", "checks": checks,
        "missing_anchors": missing, "answer": result.get("answer"), "reason": result.get("reason"),
        "sources": [{key: source[key] for key in ("id", "page", "page_verified") if key in source}
                    for source in result.get("sources", [])],
        "changes": [{key: row[key] for key in ("field", "package", "before", "after", "status")}
                    for row in result.get("changes", [])],
        "tools": [step.get("tool") for step in steps if step["action"] == "tool"],
        **({"package_scores": [
            {**row, "items": [{**item, "sources": [{"id": source["id"], "page": source["page"]}
                                                   for source in item["sources"]]}
                               for item in row["items"]]}
            for row in result["package_scores"]
        ]} if "package_scores" in result else {}),
        "timing_ms": result.get("timing_ms"),
        "steps": [{key: step[key] for key in ("step", "action", "tool", "model_ms", "tool_ms",
                                             "evidence_pages", "omitted") if key in step}
                  for step in steps],
        "uncovered_pages": result.get("uncovered_pages", []),
    }


def summarize(records):
    outcome = dict(Counter(record["outcome"] for record in records))
    by_check = {}
    for record in records:
        for name, passed in record.get("checks", {}).items():
            counts = by_check.setdefault(name, {"passed": 0, "checked": 0})
            counts["checked"] += 1
            counts["passed"] += int(passed)
    latency = {}
    for route in ("agent", "rules"):
        times = sorted(row["seconds"] for row in records if row.get("route") == route)
        if times:
            latency[route] = {"n": len(times), "p50_seconds": round(median(times), 3),
                              "p95_seconds": times[math.ceil(.95 * len(times)) - 1]}
    return {"outcomes": outcome, "checks": by_check, "latency": latency}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--suite", choices=("agent", "package-scoring", "scoring-cross-document"), default="agent")
    parser.add_argument("--case", action="append", help="Run only these case IDs")
    args = parser.parse_args()
    source_cases = (load_cases() if args.suite == "agent"
                    else load_scoring_cases(cross_document=args.suite == "scoring-cross-document"))
    cases = [case for case in source_cases if not args.case or case["id"] in args.case]
    if args.workers < 1 or not cases:
        parser.error("select at least one case and use a positive worker count")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    records = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_case, case): case["id"] for case in cases}
        for completed in as_completed(futures):
            row = completed.result()
            records[row["id"]] = row
            print(f'{len(records)}/{len(cases)} {row["id"]}: {row["outcome"]} '
                  f'{row.get("seconds", 0):.2f}s', flush=True)
            ordered = [records[case["id"]] for case in cases if case["id"] in records]
            report = {"workers": args.workers, "planned": len(cases), "completed": len(records),
                      "summary": summarize(ordered), "cases": ordered}
            staging = args.output.with_suffix(args.output.suffix + ".tmp")
            staging.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            staging.replace(args.output)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return int(any(row["outcome"] != "pass" for row in ordered))


if __name__ == "__main__":
    sys.exit(main())
