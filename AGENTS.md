# AGENTS.md

给 AI 编程助手（Claude Code / Codex / Cursor / Copilot 等）和人类贡献者的项目指南。

## 一句话

招标文件 PDF → 带页码的证据 → 风险清单 / 标包自查 / 资质判定 → 可导出 PDF。所有结论可跳回原页核对。

## 启动

```bash
# 依赖
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 配置（三项必填）
cp .env.example .env

# 运行
python -m uvicorn web_app:app --host 127.0.0.1 --port 8780
```

团队模式（需登录）：

```powershell
$env:TENDER_TEAM="1"; python -m uvicorn web_app:app --port 8780
```

```bash
TENDER_TEAM=1 python -m uvicorn web_app:app --port 8780
```

## 改动前必读

**修改抽取或检索后，必须跑回归测试**：

```bash
pytest tests/test_packages.py -q      # 约 3 分钟，纯本地
pytest tests/test_qa.py -q            # 约 3 分钟，需模型 API
```

`tests/baseline.json` 是 17 份文档的全量快照。若改动**有意**改变输出，删除该文件后重跑一次生成新基线，并人工确认差异合理。

## 数据结构

导入的每份文档在 `data/imports/<32位hex>/` 下：

| 文件 | 内容 | 是否可重建 |
|---|---|---|
| `document.pdf` | 原件 | 否 |
| `evidence.json` | MinerU 解析出的证据块（`evidence_id` / `page` / `type` / `content` / `bbox`） | 是（需 MinerU） |
| `document.json` | 元数据（名称、页数、状态） | 是 |
| `job.json` | 任务状态（`state` / `risk_state` / 错误信息） | 是 |
| `risk_report.json` | 风险清单（`items` / `candidate_count` / `uncovered_pages`） | 是（需模型） |
| `review_state.json` | 人工确认状态（团队模式下改用 SQLite） | 否 |

公司档案在 `profiles/<32位hex>.json`。团队数据在 `data/team.sqlite3`。

## 证据编号规则

| 前缀 | 含义 |
|---|---|
| `E0012` | MinerU 证据块 |
| `E0012-R3` | 该表格的第 3 行 |
| `E0012-B2` | 该证据的第 2 个提交方式分支 |
| `P15` | 第 15 页整页原文（Agent 精读产生） |
| `P11-2` | 纯文本兜底（PDF 第 11 页第 2 句，`origin=pdf_text`） |

**任何引用编号必须能在证据集中找到**，否则视为幻觉。

## 三态语义（不要混用）

### 标包适用性 `applicability`

| 值 | 含义 |
|---|---|
| `applies` | 该条款对本标包生效（通用废标规则默认就是它） |
| `pending` | 原文限定了范围，无法判断本标包是否落入 —— **必须给出 `scope` 说明理由** |
| `excluded` | 明确不适用（如条款限最低评标价法但本项目用综合评分法） |

⚠️ 不要把「没核对过」当成 `pending`。核对状态由确认记录的独立字段表达。

### 资质判定 `status`

| 值 | 含义 |
|---|---|
| `met` | 公司证据明确满足 |
| `unmet` | 公司证据明确违反（如证照过期） |
| `unknown` | 证据缺失 / 含糊 / 不可比 |

⚠️ **「档案里没登记」不等于 `unmet`**，只能判 `unknown`。

## 代码约定

- 后端 PEP 8，类型标注尽量补；不引入 ORM，SQLite 用标准库 `sqlite3`
- 前端原生 JS，**不使用 innerHTML 拼接用户数据**，一律 `textContent`（XSS 防护）
- 新增文件写入用临时文件 + `replace()` 原子替换，避免读到半个文件
- 缓存 key 必须带文件指纹（`stamp()` 返回 `mtime_ns + size`），否则文件变了缓存不失效
- `@lru_cache` 的私有实现命名为 `_xxx`，对外暴露无参包装函数

## 常见坑

| 现象 | 原因 |
|---|---|
| 改了代码但行为没变 | `@lru_cache` 未失效；检查缓存 key 是否带文件指纹 |
| 前端改了看不到效果 | 浏览器缓存；`index.html` 里的 `?v=` 版本号需要更新 |
| 中文输出乱码（Windows 终端） | PowerShell 按 GBK 解码 UTF-8；脚本里写 `sys.stdout.reconfigure(encoding="utf-8")` |
| 表格行页码不准 | `page_verified=false`，说明 8 字短语没在 PDF 文本层命中 |
| 扫描件风险清单为空 | 检查 `uncovered_pages`；可能是 MinerU 没覆盖该页 |

## 不要做的事

- ❌ 不要提交 `data/`、`profiles/`、`.env`、`data/team.sqlite3`（含真实招标文件与凭据）
- ❌ 不要为了「看起来更好」而放宽引用校验
- ❌ 不要用 `except Exception: pass` 吞掉错误
- ❌ 不要在规则可用时让模型去猜金额、日期、证照名

## 扩展点

| 想加什么 | 改哪里 |
|---|---|
| 新的问答题工具 | `tender_agent.py` 的 `TOOLS` + `DocumentAgent.call_tool` |
| 新的抽取字段 | `package_review.py` 的 `read_package_facts`，同步补 `test_packages.py` |
| 新的风险条款来源 | `risk_review.py` 的 `collect_candidates` |
| 新的导出格式 | `checklist_export.py`，路由在 `web_app.py` |