import argparse
import io
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import ZipFile


API_BASE = "https://mineru.net/api/v4"
TRANSIENT_STATUS = {429, 502, 503, 504}


def open_mineru(request, timeout, operation, upload=None, retry=True):
    attempts = 3 if retry else 1
    for attempt in range(attempts):
        if upload is not None:
            upload.seek(0)
        try:
            return urlopen(request, timeout=timeout)
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in TRANSIENT_STATUS or attempt == attempts - 1:
                raise RuntimeError(f"MinerU{operation}返回 HTTP {status}") from None
            time.sleep(2 ** (attempt + 1))


def read_token():
    from app_config import require
    return require("MINERU_API_TOKEN")["MINERU_API_TOKEN"]


def call_api(path, token, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        API_BASE + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    operation = "提交任务" if data is not None else "查询任务"
    with open_mineru(request, timeout=30, operation=operation, retry=data is None) as response:
        result = json.load(response)
    if result.get("code") != 0:
        raise RuntimeError(f"MinerU 返回错误码 {result.get('code')}")
    return result["data"]


def parse_pdf(pdf_path, first_page=1):
    """上传一份 PDF，返回保留原 PDF 页序号的证据块。"""
    pdf_path = Path(pdf_path)
    if pdf_path.suffix.lower() != ".pdf" or not pdf_path.is_file():
        raise ValueError("请提供存在的 PDF 文件")
    if first_page < 1:
        raise ValueError("PDF 起始页必须大于等于 1")

    token = read_token()
    batch = call_api("/file-urls/batch", token, {
        "files": [{"name": pdf_path.name, "is_ocr": True}],
        "language": "ch",
        "model_version": "vlm",
        "enable_formula": True,
        "enable_table": True,
    })
    with pdf_path.open("rb") as source:
        upload = Request(
            batch["file_urls"][0], data=source, method="PUT",
            # The presigned URL expects no content type in the signature.
            headers={"Content-Length": str(pdf_path.stat().st_size), "Content-Type": ""},
        )
        with open_mineru(upload, timeout=180, operation="上传文件", upload=source):
            pass

    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = call_api(f"/extract-results/batch/{batch['batch_id']}", token)
        items = result["extract_result"]
        if items and items[0]["state"] == "failed":
            raise RuntimeError("MinerU 解析失败，请在 MinerU 控制台查看任务详情")
        if items and items[0]["state"] == "done":
            break
        time.sleep(4)
    else:
        raise TimeoutError("等待 MinerU 解析超时")

    with open_mineru(items[0]["full_zip_url"], timeout=60, operation="下载解析结果") as response:
        archive_bytes = response.read()
    with ZipFile(io.BytesIO(archive_bytes)) as archive:
        content_file = next(
            (name for name in archive.namelist() if name.endswith("_content_list.json")),
            None,
        )
        if content_file is None:
            raise RuntimeError("MinerU 结果中缺少 content_list.json")
        blocks = json.loads(archive.read(content_file))

    evidence = []
    for block in blocks:
        if block["type"] not in {"text", "equation", "table", "list"}:
            continue
        # 保留表格结构，以及列表和公式各自的内容字段。
        content = block.get("table_body") or block.get("text") or block.get("list_items")
        if content:
            evidence.append({
                "evidence_id": f"E{len(evidence) + 1:04d}",
                "page": first_page + block["page_idx"],
                "type": block["type"],
                "content": content,
                "bbox": block.get("bbox"),
            })
    return evidence


def main():
    parser = argparse.ArgumentParser(description="将 PDF 解析成带页码的证据 JSON")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--first-page", type=int, default=1)
    args = parser.parse_args()
    try:
        evidence = parse_pdf(args.pdf, first_page=args.first_page)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(evidence, output, ensure_ascii=False, indent=2)
    except URLError as exc:
        parser.exit(1, f"请求 MinerU 失败（{type(exc).__name__}）\n")
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"已保存 {len(evidence)} 条证据到 {args.output}")


if __name__ == "__main__":
    main()
