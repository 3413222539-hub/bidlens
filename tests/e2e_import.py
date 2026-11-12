"""端到端验证：上传 PDF → 自动解析 → 自动生成风险清单 → 按标包自查。

用法：
    python tests/e2e_import.py <pdf 路径> [...]

验证点：
1. 导入后 job.json 出现在目录里（任务状态落盘）
2. 导入完成后 risk_report.json 自动存在，无需手动点按钮（自动生成清单）
3. 按标包自查能返回卡片与页码
"""

import json
import mimetypes
import sys
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8767"
LABELS = {
    "queued": "排队",
    "downloading": "下载",
    "parsing": "MinerU 解析",
    "analyzing": "分析风险条款",
    "ready": "完成",
    "failed": "失败",
}


def get_json(path):
    with urlopen(BASE + path, timeout=300) as response:
        return json.load(response)


def post_file(path):
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/pdf"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = Request(BASE + "/api/imports/upload", data=body, method="POST",
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urlopen(request, timeout=300) as response:
        return json.load(response)


def wait(prefix, doc_id, timeout=1800):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        job = get_json(f"/api/imports/{doc_id}")
        stage, risk = job.get("state"), job.get("risk_state")
        progress = f" 第{job['page_start']}-{job['page_end']}/{job['pages']}页" if job.get("page_start") else ""
        line = f"{prefix}[{LABELS.get(stage, stage)}] risk={risk or '-'}{progress}"
        if line != last:
            print(f"    {line}", flush=True)
            last = line
        if stage == "failed":
            return job
        if stage == "ready" and risk in {"ready", "failed", None}:
            return job
        time.sleep(3)
    raise TimeoutError(f"{prefix} 超时")


def run(pdf):
    path = Path(pdf)
    print(f"\n=== {path.name} ===", flush=True)
    started = time.monotonic()
    job = post_file(path)
    doc_id = job["id"]
    print(f"  任务 {doc_id[:8]}", flush=True)
    final = wait("  ", doc_id)
    folder = ROOT / "data" / "imports" / doc_id
    has_job = (folder / "job.json").is_file()
    has_report = (folder / "risk_report.json").is_file()
    print(f"  job.json={has_job} risk_report.json={has_report} state={final.get('state')} "
          f"risk={final.get('risk_state')} 耗时={time.monotonic() - started:.0f}s", flush=True)
    if final.get("risk_error"):
        print(f"  risk_error: {final['risk_error']}", flush=True)
    if not has_report:
        return {"name": path.name, "id": doc_id, "report": False}

    report = json.loads((folder / "risk_report.json").read_text(encoding="utf-8"))
    meta = json.loads((folder / "document.json").read_text(encoding="utf-8"))
    print(f"  清单 {len(report['items'])} 条（候选 {report.get('candidate_count')}）", flush=True)

    listed = get_json(f"/api/documents/{doc_id}/checklist")
    packages = listed.get("packages") or []
    print(f"  识别标包: {packages}", flush=True)
    summary = {"name": path.name, "id": doc_id, "pages": meta.get("pages"),
               "risk": len(report["items"]), "packages": packages, "cards": {}}
    for package in packages or [None]:
        suffix = f"?package={package}" if package else ""
        data = get_json(f"/api/documents/{doc_id}/checklist{suffix}")
        cards = [item for item in data["items"] if item.get("group") == "标包自查"]
        summary["cards"][str(package)] = cards
        label = f"第 {package} 包" if package else "项目通用"
        print(f"  {label} 自查卡 {len(cards)} 张", flush=True)
        for card in cards:
            pages = sorted({source["page"] for source in card["sources"]})
            extra = f" 合并{card['merged_count']}" if card.get("merged_count") else ""
            extra += f" 分支{len(card.get('branches') or [])}" if card.get("branches") else ""
            print(f"    [{card['applicability']}] {card['title']} | "
                  f"{card['condition'][:52]}{extra} | 页{pages}", flush=True)
    return summary


def main():
    results = []
    for pdf in sys.argv[1:]:
        try:
            results.append(run(pdf))
        except Exception as error:  # noqa: BLE001
            print(f"  ! 失败: {error}", flush=True)
            results.append({"name": Path(pdf).name, "error": str(error)})
    (ROOT / "tests" / "_e2e_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\n结果已写入 tests/_e2e_result.json")


if __name__ == "__main__":
    main()