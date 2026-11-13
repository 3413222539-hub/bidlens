"""按标包生成可打印/另存为 PDF 的自查清单页面。"""

from datetime import datetime
from html import escape

LABELS = {"applies": "适用", "excluded": "不适用", "pending": "待核对"}
ORDER = ["applies", "pending", "excluded"]

STYLE = """
@page { size: A4; margin: 16mm 14mm; }
* { box-sizing: border-box; }
html { background: #eef0f3; }
body { margin: 0; color: #16181d; background: transparent;
       font: 11pt/1.6 "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "SimSun", sans-serif; }
/* 工具栏在纸外，纸面按 A4 比例居中，不铺满整个窗口 */
.toolbar { max-width: 210mm; margin: 20px auto 0; padding: 0 16px;
           display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.sheet { width: 210mm; max-width: calc(100% - 32px); margin: 14px auto 32px;
         padding: 16mm 14mm; background: #fff; border-radius: 2px;
         box-shadow: 0 1px 3px rgba(16,24,40,.12), 0 8px 24px rgba(16,24,40,.08); }
@media print {
  html { background: #fff; }
  .sheet { width: auto; max-width: none; margin: 0; padding: 0;
           border-radius: 0; box-shadow: none; }
}
header { border-bottom: 2px solid #16181d; padding-bottom: 8px; margin-bottom: 12px; }
h1 { font-size: 16pt; margin: 0 0 6px; line-height: 1.35; overflow-wrap: anywhere; word-break: break-word; }
h2 { font-size: 12.5pt; margin: 18px 0 8px; padding-left: 6px; border-left: 3px solid #16181d; }
.meta { color: #55595f; font-size: 9.5pt; margin: 0; }
.summary { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 10px 0 4px; font-size: 10pt; }
.summary b { font-weight: 700; }
.card { border: 1px solid #c9ccd1; border-radius: 4px; padding: 9px 11px; margin-bottom: 9px;
        break-inside: avoid; page-break-inside: avoid; }
.head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
.head h3 { font-size: 11.5pt; margin: 0; }
.tag { font-size: 8.5pt; padding: 1px 6px; border-radius: 3px; border: 1px solid currentColor; white-space: nowrap; }
.tag.applies { color: #0b6b3a; }
.tag.pending { color: #8a5a00; }
.tag.excluded { color: #6b7280; }
.done { margin-left: auto; font-size: 8.5pt; color: #0b6b3a; border: 1px solid currentColor;
        border-radius: 3px; padding: 1px 6px; }
.row { margin: 3px 0; }
.row b { display: block; font-size: 9pt; color: #4c5158; font-weight: 600; }
.src { color: #3a3f46; font-size: 9.5pt; }
.branches { margin: 6px 0 0; padding-left: 14px; font-size: 10pt; }
.branches li { margin-bottom: 4px; }
.muted { color: #6b7280; }
.toolbar button { font: inherit; padding: 7px 16px; border: 1px solid #16181d; border-radius: 4px;
                  background: #16181d; color: #fff; cursor: pointer; }
.toolbar button:hover { background: #2b3038; }
.toolbar .hint { font-size: 9.5pt; color: #55595f; }
.empty { color: #6b7280; padding: 12px 0; }
@media print { .toolbar { display: none; } }
"""

SCRIPT = """
// 只有明确带上 ?print=1 才自动唤起打印对话框，避免一打开就弹窗盖住预览
if (new URLSearchParams(location.search).get('print') === '1') {
  window.addEventListener('load', function () {
    setTimeout(function () { window.print(); }, 350);
  });
}
"""


def sources_text(item):
    pages = sorted({source["page"] for source in item.get("sources", [])})
    if not pages:
        return "原文：未定位"
    text = "原文：第 " + "、".join(str(page) for page in pages) + " 页"
    if any(source.get("page_verified") is False for source in item.get("sources", [])):
        text += "（含表格行页码待核对）"
    return text


def card(item, confirmed):
    state = item.get("applicability", "pending")
    parts = [
        '<section class="card">',
        '<div class="head">',
        f'<span class="tag {escape(state)}">{LABELS.get(state, "待核对")}</span>',
        f'<h3>{escape(item.get("title", ""))}</h3>',
    ]
    if item.get("review_blocked"):
        parts.append('<span class="tag pending">暂停确认</span>')
    elif item.get("id") in confirmed:
        parts.append('<span class="done">已确认</span>')
    elif item.get("change_revision") or item.get("price_revision"):
        parts.append('<span class="tag pending">变更后待复核</span>')
    parts.append("</div>")
    parts.append(f'<div class="row"><b>触发条件</b>{escape(item.get("condition", ""))}</div>')
    parts.append(f'<div class="row"><b>检查材料或动作</b>{escape(item.get("check", ""))}</div>')
    parts.append(f'<div class="row src">{escape(sources_text(item))}</div>')
    if item.get("review_blocked"):
        parts.append(f'<div class="row muted">{escape(item["review_blocked"])}</div>')
    if revision := (item.get("change_revision") or item.get("price_revision")):
        for key, label in (("original_sources", "原招标文件"), ("correction_sources", "澄清公告")):
            pages = "、".join(str(page) for page in sorted({source["page"] for source in revision[key]}))
            parts.append(f'<div class="row src">{label} · 第 {escape(pages)} 页</div>')
        if previous := item.get("previous_confirmation"):
            name = escape(previous.get("by") or "未知")
            at = escape(previous.get("at") or "时间未记录")
            parts.append(f'<div class="row muted">历史确认 · {name} · {at}</div>')
    for event in item.get("revision_history", []):
        parts.append(f'<div class="row"><b>变更历史 · {escape(event["effective_at"])}</b>{escape(event["before"])} → {escape(event["after"])}</div>')
        for label, key in (("之前依据", "original_sources"), ("本次公告", "correction_sources")):
            for source in event[key]:
                parts.append(f'<div class="row src">{label} · {escape(source.get("document_label", "原文依据"))} · 第 {source["page"]} 页</div>')
        if record := event.get("previous_confirmation"):
            parts.append(f'<div class="row muted">变更前确认 · {escape(record.get("by", ""))} · {escape(record.get("at", ""))}</div>')
    if item.get("scope"):
        parts.append(f'<div class="row muted">{escape(item["scope"])}</div>')
    branches = item.get("branches") or []
    if branches:
        parts.append('<div class="row"><b>提交方式要求</b><ul class="branches">')
        for branch in branches:
            page = branch.get("source", {}).get("page")
            parts.append(f'<li>{escape(branch.get("text", ""))}'
                         f'<span class="src">（第 {escape(str(page))} 页）</span></li>')
        parts.append("</ul></div>")
    checks = item.get("checks") or []
    if checks:
        parts.append('<div class="row"><b>合并的检查动作</b><ul class="branches">')
        for check in checks:
            parts.append(f"<li>{escape(check)}</li>")
        parts.append("</ul></div>")
    if item.get("number"):
        parts.append(f'<div class="row muted">对应条款 {escape(item["number"])}</div>')
    parts.append("</section>")
    return "".join(parts)


def render_checklist(meta, result, package, confirmed):
    items = result["items"]
    counts = {state: sum(item.get("applicability") == state for item in items) for state in ORDER}
    done = sum(item.get("id") in confirmed and not item.get("review_blocked") for item in items)
    title = "投标自查清单"
    subtitle = f"第 {package} 标包" if package is not None else "项目通用"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(meta.get('name', ''))} · {escape(subtitle)} · {title}</title>",
        f"<style>{STYLE}</style></head><body>",
        '<div class="toolbar">',
        '<button type="button" onclick="window.print()">打印 / 另存为 PDF</button>',
        '<span class="hint">打印时请选择 A4、纵向，并关闭「页眉和页脚」以去掉浏览器地址栏。</span>',
        "</div>",
        '<div class="sheet">',
        "<header>",
        f"<h1>{escape(meta.get('name', ''))}</h1>",
        f'<p class="meta">{escape(subtitle)} · 共 {meta.get("pages", "")} 页 · 生成时间 {generated}</p>',
        '<div class="summary">',
        f"<span>适用 <b>{counts['applies']}</b></span>",
        f"<span>待核对 <b>{counts['pending']}</b></span>",
        f"<span>不适用 <b>{counts['excluded']}</b></span>",
        f"<span>已确认 <b>{done}</b> / {len(items)}</span>",
        "</div>",
        "</header>",
    ]
    for warning in result.get("revision_warnings", []):
        html.append(f'<p class="meta">{escape(warning)}</p>')
    for state in ORDER:
        group = [item for item in items if item.get("applicability") == state]
        if not group:
            continue
        html.append(f'<h2>{LABELS[state]}（{len(group)} 项）</h2>')
        html.extend(card(item, confirmed) for item in group)
    if not items:
        html.append('<p class="empty">该标包没有检查项。</p>')
    html.append(f'<p class="meta">页码指原招标文件 PDF 页码，可据此回原文核对。</p>')
    html.append("</div>")
    html.append(f"<script>{SCRIPT}</script></body></html>")
    return "".join(html)
