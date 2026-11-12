# 十份招标文件评测

这批文件用于验证「按标包自查」与「风险清单」的抽取质量，并作为回归基线。

## 文件说明

| 文件 | 说明 |
|---|---|
| `ten_document_gold.json` | 人工从原件核对的检查点：公开来源 URL、PDF sha256、导入编号、保证金/限价金额与页码、风险条款页码 |
| `ten_document_baseline.json` | 修复前的评测结果 |
| `ten_document_retest.json` | 修复后的评测结果 |
| `ten_document_split_retest.json` | 表格行拆解 + 页码反查上线后的结果 |
| `document_compare_pdf_acceptance.json` | 澄清公告比对的验收用例 |

`gold` 里的 `document_id` 是本地导入目录名（32 位 hex），**其他人无法直接复用**：仓库不包含这些 PDF（版权与保密原因），需要自己下载后重新导入，编号会不同。

## 复现步骤

1. 从 `gold` 的 `url` 字段下载对应 PDF
2. 校验 `sha256` 一致（若不一致说明官方替换过文件，页码可能对不上）
3. 导入后，用新生成的 `document_id` 更新 `gold` 里的对应字段
4. 生成风险清单（`POST /api/documents/{id}/risks`）
5. 运行评测

```powershell
.\.venv\Scripts\python.exe eval/check_benchmark.py eval/ten_document_gold.json <your_report.json>
```

## 指标含义

| 字段 | 含义 |
|---|---|
| `correct` | 保证金或限价条目的状态与金额是否符合人工标注 |
| `page_hit` | 在 `correct` 基础上，要求至少一个引用页码命中标注页 |
| `candidate_pages` | 模型调用前，候选证据是否已包含标注短语（衡量检索召回） |
| `model_source_pages` | 从已保存的风险报告中读取的引用；空值表示该方法没有可评估输出 |

`candidate_pages` 只看候选召回与页码，**不评判模型摘要的语义完整性**。`model_source_pages` **不评判整份清单的误报率**。

## 注意

这批文件参与了问题修复，因此修复后的指标属于**同批回归**，不代表泛化能力。评估泛化需要另选未参与修复过程的文档。


## 当前 Agent 问答基线

`agent_cases.json` 选出旧版问答的 20 个问题和 4 个公告比对验收题，并新增 6 个针对当前 Agent 的问题，覆盖评分追问、保证金辨别、证据不足和页面未提取。脚本直接运行与网页相同的 `/api/ask` 处理函数，跳过 HTTP、鉴权和浏览器渲染，使用本地导入目录及 `.env` 中的真实模型。

```powershell
.\.venv\Scripts\python.exe eval/run_agent_benchmark.py --workers 3 --output eval/agent_retest.json
.\.venv\Scripts\python.exe eval/run_agent_benchmark.py --case team-score-followup --output eval/agent_followup_retest.json
```

`agent_baseline.json` 是优化前的实跑记录。报告随用例完成而更新，包含实际答案、状态、引用页、工具序列和处理耗时。退出码 1 表示存在未通过、缺样本或请求失败的用例，具体原因见报告。复测时用新文件保存结果，比较延迟时沿用相同的并发数及运行环境。

- `answer_anchors` 检查答案是否出现预期事实锚点。旧评分题只给了「分」这个宽松锚点，因此只检查作答状态和引用页。完整性需要对照原文人工抽核。
- `citation_page_hit` 检查至少一个引用是否命中预期 PDF 页码。比对题额外检查旧值、新值、标包范围和两侧来源页。页码命中无法单独证明回答的每句话都得到支持。
- `coverage_warning` 检查 Agent 是否准确报告未提取到文字的页面。未提取页可能是空白页，不能从提示本身推断丢了有效内容。
- `latency` 分别统计规则路径与模型 Agent 路径的 P50、P95，包含冷启动、索引和模型调用。新版报告额外提供索引、准备和 Agent 总耗时，以及逐轮模型和工具耗时。小样本的 P95 只描述这一轮运行。
- 样本来自现有回归资料。重新导入后需同步更新清单中的前缀和被复用用例中的文件编号。这套基线可检测回归，泛化效果需要用独立的新文件验证。


本轮优化前后记录见 `agent_baseline.json` 和 `agent_retest.json`。30 道固定题通过数从 26 升到 30，模型路径 P50 从 38.061 秒降到 16.222 秒，P95 从 145.966 秒降到 50.652 秒。两轮都配置 3 并发，基线的一道未提取页用例为单独补跑。

日期锚点统一接受 `2025年07月11日` 和 `2025-07-11` 等价形式。两份报告使用同一规则核对，基线判定保持不变。206 页样本的评分题通过了页码检查，但回答主要覆盖第 1、2 包，第 3 包只有总分，第 4 包没有列出。此题仍可用于检查评分证据是否命中，若要验证按标包的完整性，需要补齐逐包评分项金标。


## 按标包核对技术评分

`package_scoring_gold.json` 记录 206 页样本原 PDF 第 41–50 页的四包技术满分、27 个评分项、组合分值和逐项引用页。验收覆盖全部标包、第 3 包、追问第 4 包及原文中不存在的包。组合项对照原文拆分核对，第 3 包的项目团队是 1、6、3 分，分别见第 47、48 页。

```powershell
.\.venv\Scripts\python.exe eval/run_agent_benchmark.py --suite package-scoring --output eval/package_scoring_results.json
```

这组验收通过与网页一致的问答处理函数，比较标包集合、逐项分值及逐项来源页。明确识别到分包评标标题、技术满分和对应评分表的文档可走规则路径。其他表格格式仍走通用问答，需另外准备样本验证。


### 跨文档评分验收

| 样本 | 评分表结构 | 核对页（PDF 页码） |
|---|---|---|
| 宝鸡文理学院 | 两包共用一张跨页综合评分表，评分因素包含子项 | 33–35 |
| 苏州科大图书采购 | 第 1–4 标段共用一套，第 5 标段另列，分值以四组章节标题公布 | 57–66 |
| 榆林中医医院 | 第 1–3 标段共用一张跨页表，分值写在评分项名称后 | 26–27 |
| 网络安全服务 | 两包各有技术评分表，沿用已有解析规则验证 | 39–47 |

```powershell
.\.venv\Scripts\python.exe eval/run_agent_benchmark.py --suite scoring-cross-document --output eval/package_scoring_cross_document_results.json
```

金标和结果文件分别为 `package_scoring_cross_document_gold.json`、`package_scoring_cross_document_results.json`。每份文件验收全包、单包与追问的范围、分值、来源页及共用范围。苏州样本额外验收从共用标准切换到独立的第 5 包。苏州样本本轮核对的是四类综合评分及其满分，细则请从引用回到原表。

新增规则依赖原文明确的标包范围，且可核对各综合评分项目之和为 100 分。缺少范围标题、非 100 分制或未识别的表格格式仍需要补充样本验证。


### 206 页样本的首问与并发

`agent_startup_206_live_before.json` 和 `agent_startup_206_live_after.json` 使用同一份 PDF 和同一组问题，测单人冷首问、热缓存追问和三请求冷并发。调用当前问答处理函数和真实模型，计时包含索引、准备和回答，排除了浏览器渲染。每轮前清理进程内派生缓存。

```powershell
.\.venv\Scripts\python.exe eval/benchmark_agent_startup.py --live --output eval/agent_startup_local.json
```

| 场景 | 优化前 | 优化后 |
|---|---:|---:|
| 单人冷首问 | 20.446 秒 | 14.599 秒 |
| 热缓存评分追问 | 4.352 秒 | 4.034 秒 |
| 三请求冷并发，整组耗时 | 58.469 秒 | 21.053 秒 |
| 三请求冷并发，PDF 读取 / 索引构建 / 标包事实构建 | 6 / 3 / 3 | 1 / 1 / 1 |

两轮均核对第 42 页的宣传推广方案满分与追问档次。这是单进程三线程的实测，跨进程部署各自维护缓存。


### 澄清公告触发复核

```powershell
.\.venv\Scripts\python.exe eval/check_revision_workflow.py --output eval/revision_workflow_results.json
```

该脚本在本地临时服务上验收安宁庄西路的截止时间变更和新乡医学院第 2 包的限价变更，使用真实导入 PDF 和内存中的旧确认记录。逐项核对新版本的确认状态、旧确认人和时间、双文档引用以及打印视图。

证照取消、原值不一致、标包隔离和缺少对应旧证照卡片的情形由 `tests/test_document_compare.py` 覆盖。其中曹县资格样例按公开公告构造证据夹具，保留第 2 包和未受影响标包的对照用例。[中国政府采购网更正公告](https://www.ccgp.gov.cn/cggg/dfgg/gzgg/202609/t20260907_27283124.htm)

目前每份原文件关联一份澄清公告。变更比较在现有证据范围内运行，存在冲突或无法核实原值时会保留原确认并提示核对。


无密钥演示的测试使用生成的 PDF 和临时目录，验证三种变更、两包隔离、引用、重新确认和重复运行后历史记录。GitHub Actions 配置只运行可在无密钥环境执行的测试。真实模型和真实文件的评测按上面的命令运行。

## 公开评测记录

公开记录保留用例、判定、分值、引用页码和耗时。模型原始回答、原文摘录和下载链接查询参数已移除。若官方 PDF 需要访问参数，请从原公告重新获取。
