# 贡献指南

欢迎 Issue、Bug 修复、功能扩展和文档改进。

## 开发环境

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
source .venv/bin/activate         # macOS / Linux
pip install -r requirements-dev.txt
cp .env.example .env              # 填入 MinerU Token 与模型 Key
```

## 改动前必读

**修改抽取或检索逻辑后，必须跑回归测试**：

```bash
pytest tests/test_packages.py -q   # 约 3 分钟，纯本地
pytest tests/test_qa.py -q         # 约 3 分钟，需模型 API
```

`tests/baseline.json` 是 17 份文档的全量快照。如果改动**有意**改变输出：

1. 删除 `tests/baseline.json`
2. 重跑测试生成新基线
3. **人工逐条确认差异合理**，再提交

## 代码约定

### 后端

- PEP 8，类型标注尽量补全
- 不引入 ORM，SQLite 用标准库 `sqlite3`
- 文件写入一律「临时文件 + `replace()`」原子替换
- 缓存 key 必须带文件指纹（`stamp()` 返回 `mtime_ns + size`）

### 前端

- 原生 JS，无构建步骤
- **禁止用 `innerHTML` 拼接用户数据**，一律 `textContent`（XSS 防护）
- 样式集中在 `web/style.css`，使用 CSS 变量，不写行内样式

### 提交信息

遵循 Conventional Commits：

```
feat: 新增 XXX 能力
fix: 修复 XXX 章节匹配失败
docs: 补充 XXX 说明
refactor: 重构 XXX
test: 补充 XXX 用例
chore: 升级依赖
```

## 不要做的事

- ❌ 提交 `data/`、`profiles/`、`.env`、`data/team.sqlite3`（含真实招标文件与凭据）
- ❌ 为「看起来更好」而放宽引用校验
- ❌ 用 `except Exception: pass` 吞掉错误
- ❌ 在规则可用时让模型去猜金额、日期、证照名

## 扩展点

| 想加什么 | 改哪里 |
|---|---|
| 新的问答工具 | `tender_agent.py` 的 `TOOLS` + `DocumentAgent.call_tool` |
| 新的抽取字段 | `package_review.py` 的 `read_package_facts`，同步补 `test_packages.py` |
| 新的风险条款来源 | `risk_review.py` 的 `collect_candidates` |
| 新的导出格式 | `checklist_export.py`，路由在 `web_app.py` |

## 报告问题

请附上：

1. 复现步骤
2. 期望行为与实际行为
3. 若是抽取错误，附上原文片段（**请脱敏**，不要贴完整招标文件）
4. 相关日志（`uvicorn` 输出里有 `agent_tool` / `model_request` 记录）