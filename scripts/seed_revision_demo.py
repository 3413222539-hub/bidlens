"""Create a local synthetic revision demo. No network or API keys required."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, TextStringObject

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "tender-review-demo-v1"
ORIGINAL_ID = uuid5(NAMESPACE_URL, PREFIX + "/original").hex
NOTICE_ID = uuid5(NAMESPACE_URL, PREFIX + "/notice").hex

ORIGINAL_PAGES = [
    ["演示招标文件", "采购项目编号：DEMO-REVIEW-001", "最高限价：第1包50万元；第2包75万元。"],
    ["投标截止时间：2026年9月22日14时30分。", "标段2须具有《食品经营许可证》。"],
]
NOTICE_PAGES = [[
    "演示澄清公告", "采购项目编号：DEMO-REVIEW-001",
    "第2包最高限价由75万元调整为80万元。",
    "第2包投标截止时间由2026年9月22日14时30分",
    "更正为2026年9月28日14时30分。",
    "第2包取消《食品经营许可证》资格要求。",
]]
NOTICE_EVIDENCE = [NOTICE_PAGES[0][:3] + ["".join(NOTICE_PAGES[0][3:5]), NOTICE_PAGES[0][5]]]


def write_pdf(path, pages):
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/STSong-Light"),
        NameObject("/Encoding"): NameObject("/UniGB-UCS2-H"),
        NameObject("/DescendantFonts"): ArrayObject([DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/CIDFontType0"),
            NameObject("/BaseFont"): NameObject("/STSong-Light"),
            NameObject("/CIDSystemInfo"): DictionaryObject({
                NameObject("/Registry"): TextStringObject("Adobe"),
                NameObject("/Ordering"): TextStringObject("GB1"),
                NameObject("/Supplement"): NumberObject(4),
            }),
        })]),
    })
    for lines in pages:
        page = writer.add_blank_page(width=595, height=842)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        operations = [
            f"BT /F1 16 Tf 45 {780 - row * 36} Td <{line.encode('utf-16-be').hex()}> Tj ET"
            for row, line in enumerate(lines)
        ]
        content = DecodedStreamObject()
        content.set_data("\n".join(operations).encode("ascii"))
        page.replace_contents(content)
    writer.write(path)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def seed_document(imports, document_id, name, pages, evidence_pages, confirmations=None):
    target = imports / document_id
    if target.exists():
        metadata = target / "document.json"
        if metadata.is_file() and json.loads(metadata.read_text(encoding="utf-8")).get("demo") == PREFIX:
            return
        raise FileExistsError(f"目标目录已存在且不属于本演示：{target}")

    with TemporaryDirectory(dir=imports, prefix=".revision-demo-") as directory:
        folder = Path(directory) / document_id
        folder.mkdir()
        write_pdf(folder / "document.pdf", pages)
        evidence = []
        for page, lines in enumerate(evidence_pages, 1):
            for line in lines:
                evidence.append({"evidence_id": f"E{len(evidence) + 1:04d}", "page": page,
                                 "type": "text", "content": line})
        write_json(folder / "evidence.json", evidence)
        meta = {"id": document_id, "name": name, "pages": len(pages),
                "state": "ready", "risk_state": "ready", "demo": PREFIX}
        write_json(folder / "document.json", meta)
        write_json(folder / "risk_report.json", {"items": [], "candidate_count": 0, "uncovered_pages": 0})
        if confirmations is not None:
            write_json(folder / "review_state.json", confirmations)
        folder.rename(target)


def seed(data_dir):
    imports = data_dir / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    reviewer = {"by": "演示复核员", "at": "2026-09-21T08:00:00+00:00"}
    seed_document(imports, ORIGINAL_ID, "演示 · 原招标文件.pdf", ORIGINAL_PAGES, ORIGINAL_PAGES, {
        "package-1": dict.fromkeys(["price-limit", "bid-deadline"], reviewer),
        "package-2": dict.fromkeys(["price-limit", "bid-deadline", "license-E0005"], reviewer),
    })
    seed_document(imports, NOTICE_ID, "演示 · 澄清公告.pdf", NOTICE_PAGES, NOTICE_EVIDENCE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    seed(args.data_dir)
    print("演示文件已就绪。启动工作台，打开「演示 · 原招标文件.pdf」，关联「演示 · 澄清公告.pdf」。")


if __name__ == "__main__":
    main()
