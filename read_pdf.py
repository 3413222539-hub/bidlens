from pathlib import Path

from pypdf import PdfReader

pdf_path = Path("data/sample.pdf")

reader = PdfReader(pdf_path)

print(f"PDF 总页数：{len(reader.pages)}")

for page_number, page in enumerate(reader.pages, start=1):
    text = page.extract_text() or ""

    print(f"\n--- PDF 第 {page_number} 页 ---")
    print(text[:200])