"""用常见招标问题批量评测各文件的问答质量。

对每份文件提出标准的四类问题（与页面快捷入口一致），
记录状态、引用条数与答案正文，便于人工判断质量。
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tender_assistant import ask_model, build_search, find_candidates, load_config, resolve_answer  # noqa: E402

IMPORTS = Path(__file__).resolve().parent.parent / "data" / "imports"

DOCS = [
    ("1a2c94fe", "评测02-人民银行征信中心（128页·单项目）"),
    ("a465a17b", "北京政务网站与新媒体（206页·4包）"),
    ("072efbb6", "苏州科大图书采购（67页·5包）"),
    ("47e3a561", "宝鸡文理学院（80页·2包）"),
    ("97ebd11e", "陕西智慧体育（80页·1包·免收保证金）"),
    ("9bcb6456", "榆林中医医院（114页·3包）"),
    ("c51bc035", "陕西乒羽网球中心（62页·1包）"),
]

QUESTIONS = [
    ("资格", "这份招标文件规定了哪些投标人资格要求？"),
    ("限价保证金", "各标包最高限价是多少？是否要求提交投标保证金？"),
    ("时间提交", "投标截止时间和文件提交方式是什么？"),
    ("评分", "技术部分有哪些评分项？各项最高多少分？"),
]


def folder_of(prefix):
    for path in IMPORTS.iterdir():
        if path.name.startswith(prefix):
            return path
    return None


def main():
    config = load_config()
    results = []
    for prefix, label in DOCS:
        folder = folder_of(prefix)
        if not folder or not (folder / "evidence.json").is_file():
            print(f"跳过 {label}（缺少证据）", flush=True)
            continue
        print(f"\n{'=' * 78}\n{label}\n{'=' * 78}", flush=True)
        started = time.monotonic()
        search = build_search(folder / "evidence.json", folder / "document.pdf")
        entry = {"prefix": prefix, "label": label, "items": []}
        for kind, question in QUESTIONS:
            begin = time.monotonic()
            candidates = find_candidates(search, question)
            try:
                result = resolve_answer(ask_model(question, candidates, config), candidates)
            except Exception as error:  # noqa: BLE001
                result = {"status": "error", "reason": f"{type(error).__name__}: {error}"}
            elapsed = time.monotonic() - begin
            record = {
                "kind": kind,
                "question": question,
                "status": result["status"],
                "elapsed": round(elapsed, 1),
                "candidate_count": len(candidates),
                "candidate_chars": sum(len(block["text"]) for block in candidates.values()),
            }
            if result["status"] == "answered":
                record["answer"] = result["answer"]
                record["conditions"] = result["conditions"]
                record["sources"] = [{"id": s["id"], "page": s["page"]} for s in result["sources"]]
                pages = sorted({s["page"] for s in result["sources"]})
                print(f"  [{kind}] answered {elapsed:.1f}s 引用{len(result['sources'])}条 页{pages}", flush=True)
                print(f"      {result['answer'][:150]}", flush=True)
            else:
                record["reason"] = result.get("reason", "")
                print(f"  [{kind}] {result['status']} {elapsed:.1f}s 候选{len(candidates)}条", flush=True)
                print(f"      {record['reason'][:100]}", flush=True)
            entry["items"].append(record)
        entry["total_seconds"] = round(time.monotonic() - started, 1)
        results.append(entry)

    out = Path(__file__).with_name("_qa_quality.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'=' * 78}\n汇总\n{'=' * 78}")
    print(f"{'文件':<34} {'资格':<10} {'限价':<10} {'时间':<10} {'评分':<10}")
    for entry in results:
        cells = []
        for item in entry["items"]:
            mark = "OK" if item["status"] == "answered" else item["status"][:6]
            cells.append(f"{mark}({len(item.get('sources', []))})")
        print(f"{entry['label'][:32]:<34} {cells[0]:<10} {cells[1]:<10} {cells[2]:<10} {cells[3]:<10}")
    answered = sum(1 for entry in results for item in entry["items"] if item["status"] == "answered")
    total = sum(len(entry["items"]) for entry in results)
    print(f"\n回答成功 {answered}/{total}，结果写入 {out.name}")


if __name__ == "__main__":
    main()