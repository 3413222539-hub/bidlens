"""Measure cold, warm follow-up and concurrent asks on one imported PDF."""

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import web_app as app  # noqa: E402
from pypdf import PdfReader  # noqa: E402

DOC = "a465a17b856a49bcbcd1353ed40e6ed2"
QUESTION = "宣传推广方案最高得几分？"
FOLLOWUP = "评分等级怎么划分？"


def measure(document_id, use_model):
    counts = Counter()
    lock = Lock()

    def counted(key, fn):
        def wrapped(*args, **kwargs):
            with lock:
                counts[key] += 1
            return fn(*args, **kwargs)
        return wrapped

    def ask(followup=False, barrier=None):
        if barrier is not None:
            barrier.wait()
        started = perf_counter()
        result = app.ask(app.Question(
            document_id=document_id, question=FOLLOWUP if followup else QUESTION,
            previous_question=QUESTION if followup else "",
        ))
        return {"seconds": round(perf_counter() - started, 3), "status": result["status"],
                "timing_ms": result.get("timing_ms"), "answer": result.get("answer"),
                "pages": sorted({row["page"] for row in result.get("sources", [])})}

    def decision(messages, config, max_tokens):
        evidence = json.loads(messages[1]["content"]).get("targeted_scoring_evidence", [])
        message = json.loads(messages[-1]["content"])
        if message.get("tool") == "read_pages":
            result = message["result"]
            evidence = result.get("evidence", []) if isinstance(result, dict) else []
        refs = [row["id"] for row in evidence if row["page"] == 42 and "宣传推广方案" in row["text"]]
        if refs:
            return {"action": "answer", "answer": "4 分", "evidence_ids": refs[:1]}
        if len(messages) == 2:
            return {"action": "tool", "tool": "read_pages",
                    "args": {"pages": [42], "keyword": "宣传推广方案"}}
        return {"action": "give_up", "reason": "未命中第 42 页评分证据"}

    def cold():
        app.invalidate_document(document_id)
        counts.clear()

    with patch.object(app, "build_search", counted("index_builds", app.build_search)), \
            patch.object(app, "read_package_facts", counted("fact_builds", app.read_package_facts)), \
            patch.object(PdfReader, "__init__", counted("pdf_reads", PdfReader.__init__)), \
            patch.object(app, "agent_messages_json", app.agent_messages_json if use_model else decision):
        cold()
        first = ask()
        cold_counts = dict(counts)
        counts.clear()
        followup = ask(followup=True)
        warm_counts = dict(counts)
        cold()
        barrier = Barrier(3)
        started = perf_counter()
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(ask, barrier=barrier) for _ in range(3)]
            requests = [job.result() for job in jobs]
        return {"document_id": document_id, "model": "live" if use_model else "stub",
                "cold": {"requests": [first], "counts": cold_counts},
                "followup": {"requests": [followup], "counts": warm_counts},
                "concurrent": {"requests": requests, "counts": dict(counts),
                               "wall_seconds": round(perf_counter() - started, 3)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    report = measure(DOC, args.live)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
