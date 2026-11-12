"""Verify the revision checklist and print endpoints with imported public PDFs."""

import argparse
import json
import sys
from pathlib import Path
from socket import socket
from threading import Thread
from time import sleep
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import urlopen

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import web_app as app  # noqa: E402

CASES = [
    ("安宁庄西路", "3d011b38aecc4126bd1252989414e9b5", "e2f74754580d429f8c1a1dae772eb800",
     None, "bid-deadline", "2026-09-22 14:30", "2026-09-28 14:30", 2, 1),
    ("新乡医学院", "89ec8c1a075349e6b25ccc41c73a22d7", "05dd22c211d3453d8716b10184b37ec4",
     2, "price-limit", "573290元", "178810元", 5, 8),
]


def verify(url, case, confirmations):
    name, original, notice, package, item_id, before, after, old_page, new_page = case
    endpoint = f"{url}/api/documents/{original}/checklist"
    scope = "package-general" if package is None else f"package-{package}"
    params = {} if package is None else {"package": package}

    def read(path, query):
        with urlopen(path + "?" + urlencode(query), timeout=90) as response:
            return response.read().decode("utf-8")

    initial = json.loads(read(endpoint, params))
    old_record = {"by": "验收复核员", "at": "2026-11-12T08:00:00+00:00"}
    confirmations[original] = {scope: {item["id"]: old_record for item in initial["items"]}}
    assert item_id in confirmations[original][scope]
    params["correction_id"] = notice
    updated = json.loads(read(endpoint, params))
    revisions = [item for item in updated["items"] if item["id"].startswith(item_id + ":")]
    assert len(revisions) == 1
    item = revisions[0]
    change = item["change_revision"]
    assert (change["before"], change["after"]) == (before, after)
    assert item["previous_confirmation"] == old_record
    assert item["id"] not in confirmations[original][scope]
    assert {(s["document_id"], s["page"]) for s in change["original_sources"]} == {(original, old_page)}
    assert {(s["document_id"], s["page"]) for s in change["correction_sources"]} == {(notice, new_page)}
    remaining = {row["id"] for row in updated["items"]} - {item["id"]}
    assert remaining == confirmations[original][scope].keys() - {item_id}
    printed = read(endpoint + "/print", params)
    assert all(word in printed for word in ("变更后待复核", after, old_record["by"], "澄清公告"))
    return {"name": name, "package": package, "before": before, "after": after,
            "original_page": old_page, "notice_page": new_page, "history_preserved": True,
            "unaffected_confirmation_ids": len(remaining), "print_view": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    confirmations = {}
    with patch.object(app, "TEAM", False), patch.object(app, "review_state", lambda doc: confirmations.get(doc, {})):
        with socket() as sock:
            sock.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app.app, log_level="warning"))
            worker = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
            worker.start()
            try:
                for _ in range(200):
                    if server.started:
                        break
                    sleep(0.05)
                results = [verify(url, case, confirmations) for case in CASES]
            finally:
                server.should_exit = True
                worker.join(5)
    report = {"confirmation_fixture": "in-memory", "cases": results}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    args.output.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
