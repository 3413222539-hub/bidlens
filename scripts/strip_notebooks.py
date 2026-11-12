"""清除 notebook 的单元格输出与执行计数，避免泄露真实招标文件原文。"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent

for path in sorted(ROOT.glob("*.ipynb")):
    notebook = json.loads(path.read_text(encoding="utf-8"))
    cleared = 0
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        if cell.get("outputs"):
            cleared += len(cell["outputs"])
        cell["outputs"] = []
        cell["execution_count"] = None
    path.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{path.name}: 清除 {cleared} 个输出")