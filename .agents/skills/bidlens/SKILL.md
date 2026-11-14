---
name: bidlens
description: 使用本机 BidLens 查询已导入的招标 PDF，按标包提问、查看风险自查清单或核对澄清变更，返回可点回原文的引用。
---

# BidLens 招标条款复核

调用已运行的 BidLens 服务。默认地址为 `http://127.0.0.1:8780`，用户也可能使用本机 8765 端口。先读 `GET /api/session` 确认服务与登录状态，再用 `GET /api/documents` 确定文件 ID。文件名相近时保留区分信息；用户未指定且无法唯一识别时才询问。服务未运行或要求登录时，说明入口和当前阻塞。

- 问条款：向 `POST /api/ask` 发送 JSON `{"document_id":"…","question":"…"}`。保留用户指定的标包、条件和子问。明确追问同一对象时可传 `previous_question`。
- 看风险：`GET /api/documents/{id}/risks` 查看原清单，`GET /api/documents/{id}/checklist?package=2` 查看指定标包；项目通用清单省略 package。区分适用性与人工确认。
- 核对公告：`GET /api/documents/{id}/revisions` 查已保存的公告时间线。比对一份已导入但尚未关联的公告时，可在问答请求中传 `correction_id`，让用户的问题明确指出要比较的字段。已保存的时间线优先。

按服务返回的结论和范围回答。优先保留 `answer_parts` 的分组及每组引用。原文链接用同一服务的 `/api/documents/{source.document_id 或当前文档 ID}/pdf#page={source.page}`；页码是 PDF 阅读器物理页。变更的前后依据分别使用各自的文件 ID。页码未经确认时标注待核对。遇到冲突，列出两处依据和差异；遇到 `needs_review` 或部分回答，明确哪些子问缺证，工具候选只作为待核对材料。

常规问答和检查使用现有文档。导入新 PDF 会调用解析服务，只有用户要求导入或明确要求分析该新文件时才执行；先确认用户指的文件，并用 `POST /api/imports/upload` 的 multipart `file` 字段上传，按返回的任务 ID 查询 `GET /api/imports/{id}`。修改确认状态或公告关联需要对应的用户操作指示。团队模式沿用服务鉴权；访问受限时让用户通过服务正常登录。
