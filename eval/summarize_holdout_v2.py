"""Summarize a full v2 run without publishing answer excerpts or local IDs."""
import argparse
import json
from pathlib import Path
from statistics import median


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    run = json.loads(args.run.read_text(encoding="utf-8"))
    if manifest["frozen_sha256"] != run["manifest_sha256"]:
        raise ValueError("Run does not match the frozen manifest")
    cases = {case["id"]: case["kind"] for case in manifest["cases"]}
    variants = ("vector", "current", "hybrid")
    expected = {(variant, case) for variant in variants for case in cases}
    rows = {(row["variant"], row["id"]): row for row in run["results"]}
    if set(rows) != expected or len(run["results"]) != len(expected):
        raise ValueError("Expected one result for each case and variant")
    print("| 方案 | 题型 | 完整正确 | 引用支持 | 恰当拒答 | 运行错误 | 耗时中位数（秒） | 输入 / 输出 token |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for variant in variants:
        for kind in ("all", "simple", "multi", "insufficient"):
            group = [row for (name, key), row in rows.items()
                     if name == variant and (kind == "all" or cases[key] == kind)]
            finished = [row for row in group if row["outcome"] == "completed"
                        and (row.get("answer") or {}).get("status") != "invalid_response"]
            assessments = [row["assessment"] for row in finished]
            correct = sum(a.get("fully_correct") is True for a in assessments)
            supported = sum(a.get("citation_supported") is True for a in assessments)
            abstention = sum(row["assessment"].get("appropriate_abstention") is True
                             for row in finished if cases[row["id"]] == "insufficient")
            count = len(group)
            missing = sum(cases[row["id"]] == "insufficient" for row in group)
            elapsed = f'{median(row["seconds"] for row in finished):.2f}' if finished else "—"
            incoming = sum(row.get("cost", {}).get("input_tokens", 0) for row in group)
            outgoing = sum(row.get("cost", {}).get("output_tokens", 0) for row in group)
            abstention_text = f"{abstention}/{missing}" if missing else "—"
            print(f"| {variant} | {kind} | {correct}/{count} | {supported}/{count} | {abstention_text} | "
                  f"{count-len(finished)} | {elapsed} | {incoming:,} / {outgoing:,} |")


if __name__ == "__main__":
    main()
