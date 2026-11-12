"""Compare checklist facts against manually checked PDF values and page anchors."""

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from package_review import package_checklist, read_package_facts
from retrieve_evidence import normalize
from risk_review import collect_candidates, read_risk_inputs, split_independent_risks


def amount_in(text):
    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)(万)?元", text)
    if not match:
        return None
    return Decimal(match[1].replace(",", "")) * (10000 if match[2] else 1)


def evaluate(spec):
    folder = ROOT / "data" / "imports" / spec["document_id"]
    result = {"sample": spec["sample"], "facts": [], "candidates": [], "model": None}
    if not (folder / "document.json").is_file():
        return result | {"error": "document not ready"}
    with (folder / "document.pdf").open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != spec["sha256"]:
            return result | {"error": "PDF hash mismatch"}
    report_path = folder / "risk_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {"items": []}
    units, native_pages = read_risk_inputs(folder / "evidence.json", folder / "document.pdf")
    facts = read_package_facts(folder, (units, native_pages))
    result["packages"] = sorted(facts[0])
    if report_path.is_file():
        result["model"] = len(report["items"])
    for test in spec["facts"]:
        selected = test.get("package")
        if selected is None and len(facts[0]) == 1:
            selected = next(iter(facts[0]))
        checklist = package_checklist(report, facts, selected)["items"]
        item = next((row for row in checklist if row["id"] == test["id"]), None)
        value = amount_in(item["condition"]) if item else None
        expected = test.get("yuan")
        correct = bool((selected is None or selected in facts[0]) and item and item["applicability"] == test["state"]
                       and (expected is None or value == Decimal(expected)))
        pages = [source["page"] for source in item["sources"]] if item else []
        result["facts"].append({**test, "correct": correct, "page_hit": correct and bool(set(test["pages"]) & set(pages)),
                                "actual": item["condition"] if item else None, "actual_pages": pages})
    candidates, _, _ = collect_candidates(units, native_pages)
    candidates = list(split_independent_risks(candidates, native_pages))
    for test in spec.get("risks", []):
        def hits(items):
            return sorted({source["page"] for item in items for source in item["sources"]
                           if all(normalize(term) in normalize(source["text"]) for term in test["terms"])})

        pages = hits(candidates)
        model_pages = hits(report["items"]) if report_path.is_file() else None
        result["candidates"].append({**test, "candidate_pages": pages, "model_source_pages": model_pages})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--check", action="store_true", help="Exit with status 1 if a gold assertion fails")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    rows = []
    for spec in manifest:
        rows.append(evaluate(spec))
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(spec["sample"], flush=True)
    if args.check:
        failed = [
            row["sample"] for row in rows
            if row.get("error") or any(not fact["page_hit"] for fact in row["facts"])
            or any(risk["page"] not in risk["candidate_pages"]
                   or (risk["model_source_pages"] is not None and risk["page"] not in risk["model_source_pages"])
                   for risk in row["candidates"])
        ]
        if failed or not rows:
            print("Regression failed: " + (", ".join(failed) or "empty manifest"), file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
