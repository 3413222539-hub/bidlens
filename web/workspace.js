const byId = (id) => document.getElementById(id);
let teamMode = false;
let teamUser;
let teamGroups = [];
const confirmed = new Set(JSON.parse(localStorage.getItem("tender-GT2611P032-reviewed") || "[]"));
let reviewItems = [];
let reviewFilter = "all";
let currentDocument = "sample";
let documentVersion = 0;
let rawRiskReport = null;
let packageReport = null;
let checklistRequest = 0;
let confirmationState = {};
let previousQuestion = "";
let riskPoll;
const documents = new Map();

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(label, action) {
  const element = node("button", label);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}

async function api(path, options) {
  const response = await fetch(path, options);
  // 非 JSON 响应（例如反向代理返回的 404 页面）不能让解析异常冒泡成 "Not Found"
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : "";
    const fallback = {
      401: "请先登录团队工作台",
      403: "没有访问权限",
      404: "没有找到请求的内容",
      409: "操作冲突，请刷新后重试",
      422: "提交的参数无效",
      500: "服务内部错误",
      502: "上游服务请求失败",
      503: "服务暂不可用",
    }[response.status] || "请求失败";
    const error = new Error(detail || fallback);
    error.status = response.status;
    throw error;
  }
  return data ?? {};
}

const jsonOptions = (method, body) => ({
  method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
});

function pdfPath(documentId = currentDocument) {
  return documentId === "sample" ? "/document.pdf" : `/api/documents/${encodeURIComponent(documentId)}/pdf`;
}

function showSource(source) {
  const notes = [];
  if (source.page_verified === false) notes.push("表格行页码待核对");
  if (source.origin === "pdf_text") notes.push("来自 PDF 文字层，未经过版面解析");
  byId("source-id").textContent =
    `${source.document_label || "PDF"} 第 ${source.page} 页 · ${source.id}${notes.length ? " · " + notes.join(" · ") : ""}`;
  byId("source-text").textContent = source.text;
  const url = `${pdfPath(source.document_id)}?view_page=${source.page}#page=${source.page}`;
  if (byId("pdf").getAttribute("src") !== url) byId("pdf").src = url;
  byId("pdf-link").href = url;
}

function sourceButtons(sources, label = "") {
  const group = node("div", "", "sources");
  for (const source of sources) {
    group.append(button(`${label}第 ${source.page} 页 · ${source.id}`, () => showSource(source)));
  }
  return group;
}

function updateReview() {
  let pendingCount = 0;
  let confirmedCount = 0;
  let visibleCount = 0;
  for (const [index, item] of reviewItems.entries()) {
    const isConfirmed = confirmed.has(item.number);
    const isPending = item.status === "needs_review" && !isConfirmed;
    if (isPending) pendingCount++;
    if (isConfirmed) confirmedCount++;
    const visible = reviewFilter === "all" || (reviewFilter === "pending" && isPending)
      || (reviewFilter === "confirmed" && isConfirmed);
    byId("clauses").children[index].hidden = !visible;
    if (visible) visibleCount++;
  }
  byId("pending").textContent = `${pendingCount} 项待复核`;
  byId("progress-label").textContent = `已人工确认 ${confirmedCount} / ${reviewItems.length} 项`;
  const progress = byId("review-progress");
  progress.max = reviewItems.length || 1;
  progress.value = confirmedCount;
  const empty = byId("review-empty");
  empty.hidden = visibleCount > 0;
  empty.textContent = {all: "暂无条款。", pending: "暂无待复核条款。", confirmed: "暂无已确认条款。"}[reviewFilter];
}

function renderClause(item) {
  const card = node("details", "", "clause");
  const summary = node("summary");
  const status = node("span", "", "badge");
  const statusText = () => confirmed.has(item.number) ? "已确认" : item.status === "needs_review" ? "待复核" : "已提取";
  status.textContent = statusText();
  const title = item.subitems.length ? "投标人串通投标" : item.condition || "需查看原文";
  summary.append(node("span", item.number, "number"), node("span", title, "clause-title"), status);
  const content = node("div", "", "details");
  if (item.subitems.length) {
    const list = node("ul");
    item.subitems.forEach((part) => list.append(node("li", part.text)));
    content.append(list);
  }
  content.append(sourceButtons(item.sources, "原文 · "));
  const actions = node("div", "", "actions");
  const supplements = node("div", "", "details");
  supplements.setAttribute("aria-live", "polite");
  if (item.cross_references.length) {
    const lookup = button(`补查第 ${item.cross_references.join("、")} 款`, async () => {
      lookup.disabled = true;
      supplements.textContent = "正在查找引用条款…";
      try {
        const groups = await api(`/api/review/${encodeURIComponent(item.number)}/references`);
        supplements.replaceChildren();
        for (const group of groups) {
          supplements.append(node("strong", `第 ${group.number} 款 ${group.title}`));
          if (!group.sources.length) supplements.append(node("p", "未找到对应依据，请继续人工复核。"));
          for (const source of group.sources) {
            supplements.append(node("p", source.text, "muted"), sourceButtons([source]));
          }
        }
        const first = groups.find((group) => group.sources.length)?.sources[0];
        if (first && currentDocument === "sample") showSource(first);
      } catch (error) {
        supplements.textContent = error.message;
      } finally {
        lookup.disabled = false;
      }
    });
    actions.append(lookup);
  }
  const toggle = button(confirmed.has(item.number) ? "撤销确认" : "确认已核对", () => {
    if (confirmed.has(item.number)) confirmed.delete(item.number);
    else confirmed.add(item.number);
    localStorage.setItem("tender-GT2611P032-reviewed", JSON.stringify([...confirmed]));
    toggle.textContent = confirmed.has(item.number) ? "撤销确认" : "确认已核对";
    status.textContent = statusText();
    updateReview();
    if (card.hidden) document.querySelector('[data-filter][aria-pressed="true"]').focus();
  });
  actions.append(toggle);
  content.append(actions, supplements);
  card.append(summary, content);
  card.addEventListener("toggle", () => { if (card.open && item.sources.length) showSource(item.sources[0]); });
  return card;
}

async function loadReview() {
  try {
    reviewItems = (await api("/api/review")).items;
    byId("clauses").replaceChildren(...reviewItems.map(renderClause));
    updateReview();
    byId("review-tools").hidden = false;
  } catch (error) {
    byId("clauses").textContent = error.message;
  }
}

const filters = document.querySelectorAll("[data-filter]");
filters.forEach((filter) => {
  filter.addEventListener("click", () => {
    reviewFilter = filter.dataset.filter;
    filters.forEach((item) => item.setAttribute("aria-pressed", String(item === filter)));
    updateReview();
  });
});

const tabs = document.querySelectorAll("[data-view]");
function showView(view) {
  byId("welcome").hidden = view !== "welcome";
  for (const id of ["review", "risks", "eligibility", "chat"]) byId(id).hidden = id !== view;
  tabs.forEach((item) => {
    item.setAttribute("aria-pressed", String(item.dataset.view === view));
  });
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    showView(tab.dataset.view);
    if (tab.dataset.view === "chat") byId("question").focus();
    if (tab.dataset.view === "risks" && !rawRiskReport && !byId("generate-risks").disabled) loadRisks();
    if (tab.dataset.view === "eligibility") loadEligibility();
  });
});

const applicabilityLabels = {applies: "适用", excluded: "不适用", pending: "待核对"};

function scopeName(report, scoped) {
  return scoped ? `package-${report.selected ?? "general"}` : "raw";
}

function renderRisks(report, scoped = false) {
  const scope = scopeName(report, scoped);
  const shared = currentDocument !== "sample";
  const storageKey = `tender-risks-reviewed-${currentDocument}-${scope}`;
  const checked = shared
    ? new Set(Object.keys(confirmationState[scope] || {}))
    : new Set(JSON.parse(localStorage.getItem(storageKey) || "[]"));
  report.items.filter((item) => item.review_blocked).forEach((item) => checked.delete(item.id));
  const status = byId("risks-status");
  const filter = byId("risk-filter").value;
  const keyword = (byId("risk-search").value || "").trim().toLowerCase();
  const matches = (item) => {
    if (scoped && filter === "revision" && (!item.change_revision || checked.has(item.id))) return false;
    if (scoped && !["all", "revision"].includes(filter) && item.applicability !== filter) return false;
    if (!keyword) return true;
    const haystack = `${item.title || ""}${item.condition || ""}${item.check || ""}${item.number || ""}`.toLowerCase();
    return haystack.includes(keyword);
  };
  const items = scoped
    ? report.items.filter(matches)
      .sort((a, b) => ["applies", "pending", "excluded"].indexOf(a.applicability) - ["applies", "pending", "excluded"].indexOf(b.applicability))
    : report.items.filter(matches);
  // 批量选择只作用于「当前可见结果」
  const visibleIds = items.filter((item) => !item.review_blocked).map((item) => item.id);
  const pending = new Set();
  const update = () => {
    const count = items.filter((item) => checked.has(item.id)).length;
    status.textContent = scoped ? `已确认 ${count} / ${items.length} 项 · ${Object.entries(applicabilityLabels).map(([value, label]) => `${label} ${report.items.filter((item) => item.applicability === value).length}`).join(" · ")}`
      : `已确认 ${count} / ${items.length} 项 · 检查了 ${report.candidate_count} 条候选`;
    if (keyword && items.length !== report.items.length) {
      status.textContent += ` · 搜索「${byId("risk-search").value.trim()}」命中 ${items.length} 条`;
    }
    if (scoped) {
      const revisions = items.filter((item) => item.change_revision && !checked.has(item.id)).length;
      if (revisions) status.textContent += ` · 变更后待复核 ${revisions} 项`;
      if (report.revision_warnings?.length) status.textContent += ` · ${report.revision_warnings.join("；")}`;
    }
    if (report.uncovered_pages) {
      const pages = report.uncovered_page_numbers;
      status.textContent += pages?.length
        ? ` · 未提取到文字的 PDF 页：${pages.join("、")}`
        : ` · 历史报告记录 ${report.uncovered_pages} 页未覆盖，重新生成可查看页码`;
    }
    if (report.candidate_count === 0 && !report.items.length) {
      status.textContent += " · 未发现废标条款，请确认导入的是招标文件而非合同或附件";
    }
    updateBulkButtons();
  };

  const cards = items.map((item) => {
    const card = node("details", "", "clause");
    const summary = node("summary");
    const badge = node("span", "", "badge");
    const checkbox = node("input", "", "risk-check");
    checkbox.type = "checkbox";
    checkbox.disabled = Boolean(item.review_blocked);
    checkbox.checked = pending.has(item.id);
    checkbox.setAttribute("aria-label", `选择「${item.title}」`);
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) pending.add(item.id);
      else pending.delete(item.id);
      updateBulkButtons();
    });
    summary.append(checkbox);
    const applicability = scoped ? node("span", applicabilityLabels[item.applicability], `badge scope-badge ${item.applicability}`) : null;
    const needsEvidence = item.references.some((ref) => !ref.sources.length)
      || item.sources.some((source) => source.page_verified === false);
    const statusText = () => checked.has(item.id) ? "已确认" : item.change_revision ? "变更后待复核" : item.review_blocked ? "待授权" : needsEvidence ? "待补证" : item.scope ? "核对适用范围" : "待核对";
    badge.textContent = statusText();
    summary.append(node("span", item.group, "number"), node("span", item.title, "clause-title"));
    if (applicability) summary.append(applicability);
    if (!scoped || checked.has(item.id) || needsEvidence || item.change_revision || item.review_blocked) summary.append(badge);
    const content = node("div", "", "details");
    content.append(node("strong", item.number ? `第 ${item.number} 款 · 触发条件` : "触发条件"), node("p", item.condition),
      node("strong", "检查材料或动作"), node("p", item.check), sourceButtons(item.sources, "原文 · "));
    if (item.change_revision) {
      const change = item.change_revision;
      if (change.field === "证照资格要求" && change.before) {
        content.append(node("strong", "变更前"), node("p", change.before));
      }
      content.append(sourceButtons(change.original_sources, "原招标文件 · "),
        sourceButtons(change.correction_sources, "澄清公告 · "));
      const previous = item.previous_confirmation;
      if (previous) {
        const at = previous.at ? new Date(previous.at).toLocaleString() : "时间未记录";
        content.append(node("p", `历史确认：${previous.by} · ${at}`, "muted"));
      }
    }
    if (item.review_blocked) content.append(node("p", item.review_blocked, "muted"));
    if (item.scope) content.append(node("p", item.scope, "muted"));
    if (item.scope_sources?.length) content.append(sourceButtons(item.scope_sources, "适用范围依据 · "));
    for (const branch of item.branches || []) {
      content.append(node("p", branch.text), sourceButtons([branch.source], "提交方式原文 · "));
    }
    if (item.checks?.length) {
      const notes = node("details", "", "related-risk");
      notes.append(node("summary", `合并的检查动作${item.merged_count ? `（来自 ${item.merged_count} 条原始风险）` : ""}`));
      const list = node("ul");
      item.checks.forEach((check) => list.append(node("li", check)));
      notes.append(list);
      content.append(notes);
    }
    for (const ref of item.references) {
      const related = node("details", "", "related-risk");
      related.append(node("summary", `引用第 ${ref.number} 款${ref.sources.length ? "" : " · 待补证"}`));
      if (ref.sources.length) related.append(sourceButtons(ref.sources));
      else related.append(node("p", "未能唯一定位被引用条款，请在 PDF 中核对。", "muted"));
      content.append(related);
    }
    const actions = node("div", "", "actions");
    const reviewer = node("span", "", "muted");
    const updateReviewer = () => {
      const record = shared && confirmationState[scope]?.[item.id];
      reviewer.textContent = record ? `${record.by} · ${record.at ? new Date(record.at).toLocaleString() : "历史记录"}` : "";
    };
    updateReviewer();
    const toggle = button(checked.has(item.id) ? "撤销确认" : "确认已核对", async () => {
      const next = !checked.has(item.id);
      if (next) checked.add(item.id);
      else checked.delete(item.id);
      toggle.disabled = true;
      try {
        if (shared) {
          confirmationState = await api(`/api/documents/${currentDocument}/confirmations`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({scope, item_id: item.id, confirmed: next}),
          });
        } else {
          localStorage.setItem(storageKey, JSON.stringify([...checked]));
        }
        updateReviewer();
        badge.textContent = statusText();
        if (scoped && checked.has(item.id) && !badge.isConnected) summary.append(badge);
        if (scoped && !checked.has(item.id) && !needsEvidence && !item.change_revision) badge.remove();
        toggle.textContent = checked.has(item.id) ? "撤销确认" : "确认已核对";
        if (scoped && filter === "revision") {
          renderRisks(report, true);
          return;
        }
        update();
      } catch (error) {
        if (next) checked.delete(item.id);
        else checked.add(item.id);
        status.textContent = error.message;
      } finally {
        toggle.disabled = false;
      }
    });
    toggle.disabled = Boolean(item.review_blocked);
    actions.append(toggle, reviewer);
    content.append(actions);
    card.append(summary, content);
    return card;
  });
  byId("risk-items").replaceChildren(...cards);
  update();
  wireBulkActions({ scope, shared, storageKey, checked, visibleIds, pending,
    refresh: () => renderRisks(report, scoped) });
  if (!items.length) {
    byId("risk-items").append(node("p",
      keyword ? `没有匹配「${byId("risk-search").value.trim()}」的条款。`
        : scoped ? "此状态下没有检查项。" : "本次扫描没有生成风险条目，请结合原件继续检查。",
      "muted"));
  }
}

/* ---------- 批量确认 ---------- */

let bulkHandler = null;

function wireBulkActions(context) {
  bulkHandler = context;
  byId("risk-select-visible").onclick = () => {
    const { visibleIds, pending } = bulkHandler;
    const allSelected = pending.size === visibleIds.length && visibleIds.length > 0;
    pending.clear();
    if (!allSelected) visibleIds.forEach((id) => pending.add(id));
    // 同步复选框视觉状态
    const boxes = [...document.querySelectorAll("#risk-items .risk-check")];
    boxes.forEach((box) => { box.checked = !box.disabled && !allSelected; });
    updateBulkButtons();
  };
  byId("risk-confirm-visible").onclick = () => applyBulk(true);
  byId("risk-undo-visible").onclick = () => applyBulk(false);
  updateBulkButtons();
}

function updateBulkButtons() {
  if (!bulkHandler) return;
  const { visibleIds, pending } = bulkHandler;
  const total = visibleIds.length;
  const selected = pending.size;
  const select = byId("risk-select-visible");
  const confirmBtn = byId("risk-confirm-visible");
  const undoBtn = byId("risk-undo-visible");
  select.disabled = total === 0;
  select.textContent = selected === total && total > 0 ? "取消全选" : "全选当前结果";
  confirmBtn.disabled = selected === 0;
  undoBtn.disabled = selected === 0;
  confirmBtn.textContent = selected ? `确认所选（${selected}）` : "确认所选";
  undoBtn.textContent = selected ? `撤销所选（${selected}）` : "撤销所选";
}

async function applyBulk(confirmed) {
  if (!bulkHandler) return;
  const { scope, shared, storageKey, checked, pending, refresh } = bulkHandler;
  const ids = [...pending];
  if (!ids.length) return;
  const confirmBtn = byId("risk-confirm-visible");
  const undoBtn = byId("risk-undo-visible");
  confirmBtn.disabled = true;
  undoBtn.disabled = true;
  const snapshot = new Set(checked);
  try {
    if (shared) {
      confirmationState = await api(`/api/documents/${currentDocument}/confirmations/batch`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({scope, item_ids: ids, confirmed}),
      });
    } else {
      ids.forEach((id) => (confirmed ? checked.add(id) : checked.delete(id)));
      localStorage.setItem(storageKey, JSON.stringify([...checked]));
    }
    pending.clear();
    refresh();
  } catch (error) {
    checked.clear();
    snapshot.forEach((id) => checked.add(id));
    byId("risks-status").textContent = error.message;
    confirmBtn.disabled = false;
    undoBtn.disabled = false;
  }
}

function setRiskView(scoped) {
  byId("raw-risks").setAttribute("aria-pressed", String(!scoped));
  byId("package-risks").setAttribute("aria-pressed", String(scoped));
  byId("package-controls").hidden = !scoped;
}

let searchTimer = 0;
function scheduleSearchRender() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const clear = byId("risk-search-clear");
    clear.hidden = !byId("risk-search").value;
    if (packageReport && byId("package-risks").getAttribute("aria-pressed") === "true") {
      renderRisks(packageReport, true);
    } else if (rawRiskReport) {
      renderRisks(rawRiskReport);
    }
  }, 160);
}

byId("risk-search").addEventListener("input", scheduleSearchRender);
byId("risk-search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    byId("risk-search").value = "";
    scheduleSearchRender();
  }
});
byId("risk-search-clear").addEventListener("click", () => {
  byId("risk-search").value = "";
  byId("risk-search-clear").hidden = true;
  byId("risk-search").focus();
  scheduleSearchRender();
});

function checklistQuery(selected) {
  const query = new URLSearchParams();
  if (selected) query.set("package", selected);
  const correction = byId("correction-document").value;
  if (!teamMode && correction) query.set("correction_id", correction);
  return query;
}

async function loadChecklist(selected = "") {
  const request = ++checklistRequest;
  const select = byId("risk-package");
  const status = byId("risks-status");
  status.textContent = "正在按原文核对标包范围…";
  select.disabled = true;
  byId("risk-items").replaceChildren();
  try {
    const report = await api(`/api/documents/${currentDocument}/checklist?${checklistQuery(selected)}`);
    if (request !== checklistRequest) return;
    packageReport = report;
    select.replaceChildren(new Option(report.packages.length ? "请选择标包" : "项目通用", ""),
      ...report.packages.map((number) => new Option(`第 ${number} 标包`, number)));
    select.value = selected;
    byId("export-pdf").disabled = !selected && report.packages.length > 0;
    renderRisks(report, true);
  } catch (error) {
    if (request === checklistRequest) {
      status.textContent = error.message;
      byId("export-pdf").disabled = true;
    }
  } finally {
    if (request === checklistRequest) select.disabled = false;
  }
}

byId("raw-risks").addEventListener("click", () => {
  checklistRequest++;
  setRiskView(false);
  renderRisks(rawRiskReport);
});
byId("package-risks").addEventListener("click", () => {
  setRiskView(true);
  loadChecklist(packageReport?.selected ?? "");
});
byId("risk-package").addEventListener("change", (event) => {
  byId("source-text").textContent = "已切换标包，请选择当前清单的引用核对原文。";
  byId("source-id").textContent = "等待核对原文";
  loadChecklist(event.target.value);
});
byId("risk-filter").addEventListener("change", () => {
  if (packageReport && !byId("risk-package").disabled) renderRisks(packageReport, true);
});
byId("export-pdf").addEventListener("click", () => {
  const selected = byId("risk-package").value;
  if (!selected && packageReport?.packages.length) {
    byId("risks-status").textContent = "请先选择标包再导出。";
    return;
  }
  byId("risks-status").textContent = "已打开打印视图，可在打印窗口选择「另存为 PDF」。";
  window.open(`/api/documents/${currentDocument}/checklist/print?${checklistQuery(selected)}&print=1`, "_blank", "noopener");
});

async function loadRisks(generate = false, force = false) {
  clearTimeout(riskPoll);
  const version = documentVersion;
  const trigger = byId("generate-risks");
  const status = byId("risks-status");
  const badge = byId("risk-tab-status");
  let waiting = false;
  trigger.disabled = true;
  trigger.hidden = false;
  status.textContent = generate
    ? (force ? "正在重新分析全部条款…" : "正在定位审查条款并生成清单…")
    : "正在读取清单…";
  await confirmationsReady;
  try {
    const path = `/api/documents/${currentDocument}/risks${force ? "?force=true" : ""}`;
    const report = await api(path, generate ? {method: "POST"} : undefined);
    if (version !== documentVersion) return;
    if (report.items) {
      rawRiskReport = report;
      byId("risk-views").hidden = false;
      byId("risk-tools").hidden = false;
      badge.textContent = "已就绪";
      badge.hidden = false;
      trigger.hidden = false;
      trigger.disabled = false;
      trigger.textContent = "重新生成清单";
      if (byId("package-risks").getAttribute("aria-pressed") === "true") return;
      renderRisks(report);
    } else if (report.risk_state === "running") {
      waiting = true;
      badge.textContent = "生成中";
      badge.hidden = false;
      trigger.hidden = true;
      status.textContent = "正在后台生成清单。你可以继续查看原文或提问。";
      riskPoll = setTimeout(() => loadRisks(), 2000);
    } else {
      badge.textContent = "可重试";
      badge.hidden = report.risk_state !== "failed";
      trigger.textContent = badge.hidden ? "生成风险清单" : "重试生成";
      status.textContent = report.risk_error || "尚未生成清单。";
    }
  } catch (error) {
    if (version === documentVersion) status.textContent = error.message;
  } finally {
    if (version === documentVersion) trigger.disabled = waiting;
  }
}

byId("generate-risks").addEventListener("click", () => {
  const regenerate = rawRiskReport !== null;
  if (regenerate && !window.confirm("重新生成会用最新规则重跑全部条款，覆盖现有清单和标包结论。继续吗？")) return;
  loadRisks(true, regenerate);
});

byId("chat-starters").addEventListener("click", (event) => {
  const starter = event.target.closest("[data-question]");
  if (!starter) return;
  byId("question").value = starter.dataset.question;
  byId("question").focus();
});

function updateCorrectionOptions() {
  const select = byId("correction-document");
  const saved = localStorage.getItem(`tender-correction-${currentDocument}`);
  select.replaceChildren(new Option("选择已导入的 PDF", ""));
  for (const doc of documents.values()) {
    if (doc.id !== currentDocument && doc.id !== "sample") select.add(new Option(doc.name, doc.id));
  }
  select.value = !teamMode && [...select.options].some((option) => option.value === saved) ? saved : "";
  select.disabled = teamMode;
  byId("compare-documents").disabled = !select.value;
}

byId("correction-document").addEventListener("change", async (event) => {
  const select = event.target;
  if (!teamMode) {
    localStorage.setItem(`tender-correction-${currentDocument}`, select.value);
    byId("compare-documents").disabled = !select.value;
    refreshChecklistAfterCorrection();
    updateDemoEntry();
    return;
  }
  const version = documentVersion;
  const documentId = currentDocument;
  const target = select.value;
  select.disabled = true;
  byId("compare-documents").disabled = true;
  try {
    const saved = await api(`/api/documents/${documentId}/correction`, jsonOptions("PUT", {correction_id: target}));
    if (version === documentVersion) {
      select.value = saved.correction_id;
      refreshChecklistAfterCorrection();
    }
  } catch (error) {
    if (version === documentVersion) {
      select.value = "";
      byId("chat-status").textContent = error.message;
    }
  } finally {
    if (version === documentVersion) {
      select.disabled = false;
      byId("compare-documents").disabled = !select.value;
    }
  }
});

function refreshChecklistAfterCorrection() {
  if (byId("package-risks").getAttribute("aria-pressed") === "true") {
    loadChecklist(byId("risk-package").value);
  }
}

async function loadSharing(docId) {
  const version = documentVersion;
  const select = byId("correction-document");
  byId("team-sharing").hidden = true;
  if (!teamMode || docId === "sample") return;
  const url = `/api/documents/${docId}`;
  const [linked, grants, groupShares] = await Promise.allSettled([
    api(url + "/correction"), api(url + "/shares"), api(url + "/groups"),
  ]);
  if (version !== documentVersion) return;
  if (linked.status === "fulfilled") select.value = linked.value.correction_id;
  byId("compare-documents").disabled = !select.value;
  if (grants.status === "fulfilled") {
    select.disabled = false;
    byId("team-sharing").hidden = false;
    const members = byId("team-share-members");
    members.replaceChildren(...grants.value.members.map((name) =>
      button(`撤销 ${name} 的授权`, () => changeShare(docId, name, false))));
    const target = byId("group-target");
    target.replaceChildren(new Option("请选择群组", ""), ...teamGroups.map((group) => new Option(group.title, group.id)));
    byId("group-share").hidden = !teamGroups.length;
    byId("document-groups").replaceChildren(...(groupShares.value || []).map((group) =>
      button(`停止共享到 ${group.title}`, () => changeGroupShare(docId, group.id, false))));
  }
}

async function changeGroupShare(docId, groupId, grant) {
  const version = documentVersion;
  try {
    await api(`/api/documents/${docId}/groups`, jsonOptions("PUT", {group_id: groupId, grant}));
    if (version !== documentVersion) return;
    byId("team-share-status").textContent = grant ? "已共享到群组" : "已停止群组共享";
    await loadSharing(docId);
  } catch (error) {
    if (version === documentVersion) byId("team-share-status").textContent = error.message;
  }
}

byId("group-share").addEventListener("submit", (event) => {
  event.preventDefault();
  const groupId = Number(byId("group-target").value);
  if (groupId) changeGroupShare(currentDocument, groupId, true);
});

function renderGroups() {
  const list = byId("group-list");
  list.replaceChildren(...teamGroups.map((group) => {
    const card = node("div", "", "group-card");
    const head = node("div", "", "group-card-head");
    head.append(node("strong", group.title));
    if (group.creator === teamUser.name) head.append(node("span", "创建者", "group-owner-tag"));
    card.append(head);

    const meta = node("div", "", "group-card-meta");
    meta.append(node("span", `邀请码 ${group.invite || "仅创建者可见"}`));
    card.append(meta);

    const actions = node("div", "", "group-card-actions");
    if (group.invite) {
      actions.append(button("复制邀请码", async (event) => {
        try {
          await navigator.clipboard.writeText(group.invite);
          event.currentTarget.textContent = "已复制";
          setTimeout(() => { event.currentTarget.textContent = "复制邀请码"; }, 1600);
        } catch (error) {
          byId("group-status").textContent = "复制失败，请手动选中邀请码。";
        }
      }));
    }
    if (group.creator !== teamUser.name) {
      actions.append(button("退出群组", () => updateGroups(`/api/groups/${group.id}`, {method: "DELETE"})));
    }
    if (actions.childElementCount) card.append(actions);
    return card;
  }));
  byId("group-empty").hidden = teamGroups.length > 0;
  byId("group-count").textContent = String(teamGroups.length);
}

async function updateGroups(url, options, form) {
  try {
    teamGroups = await api(url, options);
    if (form) form.reset();
    renderGroups();
    await loadDocuments();
    byId("group-status").textContent = "群组已更新";
  } catch (error) {
    byId("group-status").textContent = error.message;
  }
}

for (const [id, url] of [["group-create", "/api/groups"], ["group-join", "/api/groups/join"]]) {
  byId(id).addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    updateGroups(url, jsonOptions("POST", Object.fromEntries(new FormData(form))), form);
  });
}

async function changeShare(docId, name, grant) {
  const version = documentVersion;
  try {
    await api(`/api/documents/${docId}/shares`, jsonOptions("PUT", {name, grant}));
    if (version === documentVersion) {
      byId("team-share-status").textContent = grant ? "已授权" : "已撤销";
      await loadSharing(docId);
    }
  } catch (error) {
    if (version === documentVersion) byId("team-share-status").textContent = error.message;
  }
}

byId("team-share-form").addEventListener("submit", (event) => {
  event.preventDefault();
  changeShare(currentDocument, new FormData(event.currentTarget).get("name").trim(), true);
});
byId("compare-documents").addEventListener("click", () => {
  if (byId("ask-form").querySelector("button").disabled) return;
  byId("question").value = "最高限价、投标截止时间和证照资格要求有什么变化？";
  byId("follow-up").checked = false;
  byId("ask-form").requestSubmit();
});

const agentToolLabels = {
  search: "检索原文",
  read_pages: "精读页码",
  package_facts: "读取标包事实",
  checklist: "读取风险清单",
  outline: "读取文档结构",
};

const AGENT_AVATAR_SVG = `
<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M6.2 3.6h7.4l4.2 4.2v12.6a1.6 1.6 0 0 1-1.6 1.6H6.2a1.6 1.6 0 0 1-1.6-1.6V5.2a1.6 1.6 0 0 1 1.6-1.6z"
        fill="currentColor" opacity=".26"/>
  <path d="M6.2 3.6h7.4l4.2 4.2v12.6a1.6 1.6 0 0 1-1.6 1.6H6.2a1.6 1.6 0 0 1-1.6-1.6V5.2a1.6 1.6 0 0 1 1.6-1.6z"
        stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
  <path d="M13.4 3.8v4.2h4.2" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
  <circle cx="10.1" cy="13.1" r="2.9" stroke="currentColor" stroke-width="1.5"/>
  <path d="M12.3 15.3l2.4 2.4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
</svg>`;

function agentAvatar() {
  const avatar = node("span", "", "chat-avatar agent");
  avatar.setAttribute("aria-hidden", "true");
  avatar.innerHTML = AGENT_AVATAR_SVG;
  return avatar;
}

function userAvatar(name) {
  const avatar = node("span", "", "chat-avatar user");
  avatar.setAttribute("aria-hidden", "true");
  const initial = (name || "我").trim().charAt(0).toUpperCase();
  avatar.textContent = initial;
  return avatar;
}

function resetMessages() {
  const container = byId("messages");
  const row = node("div", "", "chat-row chat-row-agent chat-empty");
  const bubble = node("div", "", "chat-bubble agent-bubble");
  bubble.append(node("p", "你好，我是这份文件的审阅助手。可以直接问我资格要求、限价、保证金、截止时间或评分办法，我会自己翻原文找依据。"));
  row.append(agentAvatar(), bubble);
  container.replaceChildren(row);
}

function renderUserMessage(question) {
  const row = node("div", "", "chat-row chat-row-user");
  const bubble = node("div", "", "chat-bubble user-bubble");
  bubble.append(node("p", question));
  row.append(bubble, userAvatar(teamUser?.name || "我"));
  return row;
}

function renderThinking() {
  const row = node("div", "", "chat-row chat-row-agent chat-thinking");
  const bubble = node("div", "", "chat-bubble agent-bubble");
  const dots = node("span", "", "thinking-dots");
  dots.append(node("i"), node("i"), node("i"));
  bubble.append(node("span", "助手正在查阅文件", "thinking-label"), dots);
  row.append(agentAvatar(), bubble);
  return row;
}

function renderPackageScores(packages) {
  const group = node("div", "", "agent-answer");
  for (const pack of packages) {
    const card = node("details", "", "related-risk");
    card.open = packages.length === 1 || !pack.complete;
    const label = pack.score_kind === "overall" ? "综合评分（含报价）" : "技术满分";
    const total = pack.max_score == null ? "" : ` · ${label} ${pack.max_score} 分`;
    card.append(node("summary", `第 ${pack.package} 包${total}${pack.complete ? "" : " · 待复核"}`));
    if (pack.shared_packages?.length > 1) {
      card.append(node("p", `第 ${pack.shared_packages.join("、")} 包共用这套评分标准。`, "muted"));
    }
    const items = node("ul");
    for (const item of pack.items) {
      const parts = item.parts.length > 1 ? `（${item.parts.join(" + ")}）` : "";
      const line = node("li", `${item.name}：${item.max_score} 分${parts}`);
      line.append(sourceButtons(item.sources));
      items.append(line);
    }
    if (pack.items.length) card.append(items);
    if (!pack.complete) card.append(node("p", "评分表范围、满分或评分项未能核齐，请核对原文件。", "muted"));
    group.append(card);
  }
  return group;
}

function renderAgentMessage(result, input, question, previous) {
  const row = node("div", "", "chat-row chat-row-agent");
  const bubble = node("div", "", "chat-bubble agent-bubble");

  if (result.status === "comparison") {
    renderComparison(bubble, result.changes);
    row.append(agentAvatar(), bubble);
    return row;
  }

  const head = node("div", "", "agent-bubble-head");
  head.append(node("strong", "招标助手", "agent-name"));
  if (result.steps?.length) {
    const toolCount = result.steps.filter((step) => step.action === "tool").length;
    head.append(node("span", `查阅 ${toolCount} 次`, "agent-meta"));
  }
  bubble.append(head);

  if (result.status === "answered") {
    if (result.steps?.length) bubble.append(renderAgentSteps(result.steps, result.partial));
    bubble.append(result.package_scores ? renderPackageScores(result.package_scores)
      : node("div", result.answer, "agent-answer"));
    if (result.conditions?.length) {
      const block = node("div", "", "agent-conditions");
      block.append(node("span", "适用条件", "agent-label"));
      const list = node("ul");
      for (const condition of result.conditions) list.append(node("li", condition));
      block.append(list);
      bubble.append(block);
    }
    if (!result.package_scores) {
      const citations = node("div", "", "agent-sources");
      citations.append(node("span", "原文依据", "agent-label"), sourceButtons(result.sources));
      bubble.append(citations);
    }
  } else {
    const invalid = result.status === "invalid_response";
    if (result.steps?.length) bubble.append(renderAgentSteps(result.steps));
    const note = node("div", "", "agent-note");
    note.append(node("span", invalid ? "回答生成失败" : "没有找到依据", "agent-label"));
    note.append(node("p", result.reason));
    if (result.searched) note.append(node("p", `已查范围：${result.searched}`, "muted"));
    bubble.append(note);
    if (result.tool_evidence?.length) {
      const box = node("div", "", "agent-sources");
      box.append(node("span", "助手查到的原文 · 供人工核对", "agent-label"));
      box.append(sourceButtons(result.tool_evidence));
      bubble.append(box);
    }
    if (invalid && input && !input.value) {
      input.value = question;
      byId("follow-up").checked = Boolean(previous);
    }
  }

  if (result.uncovered_pages?.length) {
    bubble.append(node("p",
      `以下 PDF 页未提取到文字，请打开原件核对：${result.uncovered_pages.join("、")}`, "muted"));
  }
  row.append(agentAvatar(), bubble);
  return row;
}

function renderAgentSteps(steps, partial = false) {
  const box = node("details", "", "agent-trace");
  const label = partial ? "助手的查证过程（结果不完整）" : "助手的查证过程";
  box.append(node("summary", `${label} · ${steps.filter(s => s.action === "tool").length} 次查阅`));
  const list = node("ol", "", "agent-steps");
  for (const step of steps) {
    const row = node("li", "", `agent-step ${step.action}`);
    const head = node("div", "", "agent-step-head");
    if (step.action === "tool") {
      head.append(node("span", agentToolLabels[step.tool] || step.tool, "agent-step-name"));
      if (step.args && Object.keys(step.args).length) {
        const args = Object.entries(step.args)
          .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(",") : value}`)
          .join(" · ");
        head.append(node("span", args, "agent-step-args"));
      }
      row.append(head);
      if (step.summary) row.append(node("p", step.summary, "agent-step-result"));
    } else if (step.action === "answer") {
      head.append(node("span", "给出结论", "agent-step-name"));
      row.append(head);
    } else if (step.action === "give_up") {
      head.append(node("span", "未能确认", "agent-step-name"));
      row.append(head);
    } else {
      head.append(node("span", "调整策略", "agent-step-name"));
      row.append(head);
    }
    if (step.thought) row.append(node("p", step.thought, "agent-step-thought"));
    if (step.warning) row.append(node("p", step.warning, "agent-step-warning"));
    list.append(row);
  }
  box.append(list);
  return box;
}

function renderComparison(answer, changes) {
  answer.append(node("p", "招标文件与澄清公告比对", "eyebrow"));
  if (!changes.length) {
    answer.append(node("p", "未检索到所问字段或标包的明确变更。可核对澄清公告原文，或换用含具体变更内容的 PDF。"));
  }
  for (const change of changes) {
    const card = node("section", "", "comparison-result");
    const scope = change.package === null ? "" : `第 ${change.package} 包 · `;
    card.append(node("strong", scope + change.field + (change.action ? ` · ${change.action}` : "")));
    if (change.action) {
      card.append(node("p", `变更前：${change.before || "未定位到对应旧条款"}`),
        node("p", `变更后：${change.after || "新条款待核实"}`));
    } else {
      card.append(node("p", `${change.before || "原值待核实"} → ${change.after || "新值待核实"}`));
    }
    if (change.reason) card.append(node("p", `待核对 · ${change.reason}`, "muted"));
    if (change.original_sources.length) card.append(sourceButtons(change.original_sources, "原招标文件 · "));
    if (change.correction_sources.length) card.append(sourceButtons(change.correction_sources, "澄清公告 · "));
    answer.append(card);
  }
}

// 输入框自动增高：内容变化时按滚动高度调整，最多 6 行
function autoGrowInput() {
  const input = byId("question");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 168)}px`;
}

byId("question").addEventListener("input", autoGrowInput);
byId("question").addEventListener("keydown", (event) => {
  // Enter 发送，Shift+Enter 换行；输入法组合期间不拦截
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  byId("ask-form").requestSubmit();
});

byId("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("question");
  const submit = event.currentTarget.querySelector("button");
  const question = input.value.trim();
  if (!question || submit.disabled) return;
  const version = documentVersion;
  const messages = byId("messages");
  const previous = byId("follow-up").checked ? previousQuestion : "";
  byId("chat-starters").hidden = true;
  messages.append(renderUserMessage(question));
  submit.disabled = true;
  input.value = "";
  autoGrowInput();
  byId("chat-status").textContent = "助手正在查阅文件…";
  const thinking = renderThinking();
  messages.append(thinking);
  messages.scrollTop = messages.scrollHeight;
  try {
    const result = await api("/api/ask", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question, document_id: currentDocument, previous_question: previous,
        correction_id: byId("correction-document").value}),
    });
    if (version !== documentVersion) return;
    thinking.remove();
    const answer = renderAgentMessage(result, input, question, previous);
    messages.append(answer);
    messages.scrollTop = messages.scrollHeight;
    if (result.status !== "invalid_response") {
      previousQuestion = question;
      byId("follow-up-option").hidden = false;
      byId("follow-up").checked = false;
    }
    byId("chat-status").textContent = "";
  } catch (error) {
    if (version === documentVersion) {
      thinking.remove();
      byId("chat-status").textContent = error.message;
      if (!input.value) input.value = question;
    }
  } finally {
    if (version === documentVersion) {
      submit.disabled = false;
      if (!byId("chat").hidden) input.focus();
    }
  }
});

function registerDocument(doc) {
  const select = byId("documents");
  if (!documents.has(doc.id)) select.add(new Option(doc.name, doc.id));
  documents.set(doc.id, doc);
  select.hidden = false;
}

let confirmationsReady = Promise.resolve();

async function loadConfirmations() {
  confirmationsReady = (async () => {
    try {
      confirmationState = await api(`/api/documents/${currentDocument}/confirmations`);
    } catch (error) {
      confirmationState = {};
    }
  })();
  return confirmationsReady;
}

function demoDocuments() {
  const samples = [...documents.values()].filter((doc) => doc.demo === "tender-review-demo-v1");
  return {
    original: samples.find((doc) => doc.name === "演示 · 原招标文件.pdf"),
    notice: samples.find((doc) => doc.name === "演示 · 澄清公告.pdf"),
  };
}

function updateDemoEntry() {
  const {original, notice} = demoDocuments();
  const available = Boolean(!teamMode && original && notice);
  const active = available && currentDocument === original.id;
  byId("demo-entry").hidden = !available;
  byId("start-demo").hidden = active;
  byId("demo-checklist").hidden = !active;
  byId("demo-correction").hidden = !active;
  if (!available) return;
  const linked = active && byId("correction-document").value === notice.id;
  byId("demo-hint").textContent = !active
    ? "用两份示例 PDF 体验公告变更后的复核流程，无需上传文件或填写 API Key。"
    : linked
      ? "第 2 包的最高限价、截止时间和证照要求已变更。切换到第 1 包可核对原确认记录。"
      : "先查看第 2 包的历史确认，再关联澄清公告，看看哪些条目需要重审。";
  byId("demo-correction").textContent = linked ? "查看第 2 包待复核" : "关联演示公告并查看待复核";
}

async function openDemoChecklist(filter) {
  const version = documentVersion;
  await confirmationsReady;
  if (version !== documentVersion) return;
  showView("risks");
  setRiskView(true);
  byId("risk-filter").value = filter;
  await loadChecklist("2");
}

byId("start-demo").addEventListener("click", () => {
  const {original} = demoDocuments();
  if (!original || teamMode) return;
  openDocument(original);
  openDemoChecklist("all");
});

byId("demo-checklist").addEventListener("click", () => openDemoChecklist("all"));

byId("demo-correction").addEventListener("click", async () => {
  const {original, notice} = demoDocuments();
  if (teamMode || !original || !notice || currentDocument !== original.id) return;
  const select = byId("correction-document");
  select.value = notice.id;
  localStorage.setItem(`tender-correction-${original.id}`, notice.id);
  byId("compare-documents").disabled = false;
  updateDemoEntry();
  await openDemoChecklist("revision");
});

function showEmpty() {
  clearTimeout(riskPoll);
  documentVersion++;
  checklistRequest++;
  rawRiskReport = null;
  packageReport = null;
  confirmationState = {};
  currentDocument = "";
  byId("documents").value = "";
  document.querySelector(".workspace").classList.add("is-empty");
  byId("evidence-panel").hidden = true;
  byId("show-import").hidden = true;
  byId("tabs").hidden = true;
  byId("review-tab").hidden = true;
  byId("risks-tab").hidden = true;
  byId("eligibility-tab").hidden = true;
  byId("eligibility-summary").hidden = true;
  byId("eligibility-rows").replaceChildren();
  byId("eligibility-status").textContent = "";
  showProfiles(false);
  byId("import-panel").hidden = false;
  byId("show-import").setAttribute("aria-expanded", "true");
  byId("import-status").textContent = "";
  byId("risk-items").replaceChildren();
  byId("risks-status").textContent = "";
  byId("source-id").textContent = "尚未导入文件";
  byId("source-text").textContent = "导入 PDF 后，点击条款或问答引用会在这里显示原文。";
  byId("pdf").removeAttribute("src");
  byId("pdf").hidden = true;
  byId("pdf-link").hidden = true;
  showView("welcome");
  localStorage.removeItem("tender-current-document");
  updateDemoEntry();
}

function openDocument(doc) {
  clearTimeout(riskPoll);
  documentVersion++;
  checklistRequest++;
  rawRiskReport = null;
  packageReport = null;
  confirmationState = {};
  byId("risk-views").hidden = true;
  byId("risk-tools").hidden = true;
  byId("risk-search").value = "";
  byId("risk-search-clear").hidden = true;
  byId("risk-filter").value = "all";
  byId("export-pdf").disabled = true;
  setRiskView(false);
  currentDocument = doc.id;
  updateCorrectionOptions();
  byId("team-share-status").textContent = "";
  loadSharing(doc.id);
  document.querySelector(".workspace").classList.remove("is-empty");
  byId("evidence-panel").hidden = false;
  byId("show-import").hidden = false;
  byId("import-panel").hidden = true;
  byId("show-import").setAttribute("aria-expanded", "false");
  byId("document-label").textContent = "当前招标文件";
  const isSample = doc.id === "sample";
  if (!isSample) loadConfirmations();
  byId("tabs").hidden = false;
  byId("pdf").hidden = false;
  byId("pdf-link").hidden = false;
  byId("documents").value = doc.id;
  byId("document-title").textContent = doc.name;
  byId("document-meta").textContent = `${isSample ? "GT2611P032 · " : "已解析 · "}${doc.pages} 页`;
  byId("review-tab").hidden = !isSample;
  byId("risks-tab").hidden = isSample;
  byId("eligibility-tab").hidden = isSample;
  byId("eligibility-summary").hidden = true;
  byId("eligibility-rows").replaceChildren();
  byId("eligibility-status").textContent = "";
  byId("eligibility-package").replaceChildren();
  showProfiles(false);
  byId("generate-risks").hidden = false;
  byId("generate-risks").disabled = false;
  byId("generate-risks").textContent = "生成风险清单";
  byId("risk-tab-status").hidden = true;
  byId("risk-items").replaceChildren();
  byId("risks-status").textContent = "";
  showView("chat");
  previousQuestion = "";
  byId("follow-up").checked = false;
  byId("follow-up-option").hidden = true;
  byId("chat-starters").hidden = false;
  resetMessages();
  byId("chat-status").textContent = "";
  byId("question").value = "";
  byId("ask-form").querySelector("button").disabled = false;
  byId("source-id").textContent = "PDF 第 1 页";
  byId("source-text").textContent = "点击条款或问答引用后，这里会显示证据原文。";
  const url = `${pdfPath()}#page=1`;
  byId("pdf").src = url;
  byId("pdf-link").href = url;
  localStorage.setItem("tender-current-document", doc.id);
  updateDemoEntry();
  if (!isSample) loadRisks();
}

async function loadDocuments() {
  try {
    const list = await api("/api/documents");
    documents.clear();
    const select = byId("documents");
    select.replaceChildren(new Option("选择文档", ""));
    select.hidden = !list.length;
    list.forEach(registerDocument);
    const saved = documents.get(localStorage.getItem("tender-current-document"));
    if (saved) openDocument(saved);
    else showEmpty();
  } catch (error) {
    showEmpty();
    byId("import-status").textContent = error.message;
  }
}

byId("documents").addEventListener("change", (event) => {
  const doc = documents.get(event.target.value);
  if (doc) openDocument(doc);
});
byId("show-import").addEventListener("click", (event) => {
  const panel = byId("import-panel");
  panel.hidden = !panel.hidden;
  event.currentTarget.setAttribute("aria-expanded", String(!panel.hidden));
});

async function monitorImport(start) {
  const controls = byId("import-panel").querySelectorAll("input, button");
  const status = byId("import-status");
  controls.forEach((control) => { control.disabled = true; });
  status.textContent = "正在提交导入任务…";
  try {
    let job = await start();
    sessionStorage.setItem("tender-import-job", job.id);
    let lastProgress = Date.now();
    const labels = {queued: "等待处理", downloading: "正在下载 PDF", parsing: "正在由 MinerU 解析"};
    while (labels[job.state]) {
      const progress = job.page_start ? ` · 第 ${job.page_start}–${job.page_end} 页 / 共 ${job.pages} 页` : "";
      status.textContent = `${labels[job.state]} · ${job.name}${progress}`;
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const next = await api(`/api/imports/${job.id}`, {signal: AbortSignal.timeout(30000)});
      if (next.state !== job.state || next.page_start !== job.page_start) lastProgress = Date.now();
      job = next;
      const maxIdle = job.state === "queued" ? 60000 : 10 * 60 * 1000;
      if (labels[job.state] && Date.now() - lastProgress >= maxIdle) {
        throw new Error("导入任务长时间没有更新，请检查服务状态后重新提交。");
      }
    }
    if (job.state !== "ready") throw new Error(job.error || "导入未完成");
    registerDocument(job);
    openDocument(job);
    byId("upload-form").reset();
    byId("url-form").reset();
    status.textContent = `解析完成 · ${job.pages} 页`;
  } catch (error) {
    status.textContent = error.name === "TimeoutError" ? "查询导入状态超时，请稍后重试。" : error.message;
  } finally {
    sessionStorage.removeItem("tender-import-job");
    controls.forEach((control) => { control.disabled = false; });
  }
}

byId("upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const file = byId("pdf-file").files[0];
  if (!file) return;
  if (file.size > 200000000) {
    byId("import-status").textContent = "PDF 超过 200 MB";
    return;
  }
  const body = new FormData();
  body.append("file", file);
  monitorImport(() => api("/api/imports/upload", {method: "POST", body}));
});

byId("url-form").addEventListener("submit", (event) => {
  event.preventDefault();
  monitorImport(() => api("/api/imports/url", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({url: byId("pdf-url").value.trim()}),
  }));
});

/* ---------- 公司档案（独立一级功能） ---------- */

let companies = [];
let editingCompany = "";

function profileEntries(list) {
  return (list || []).map((item) => {
    const name = item.type || item.name || "";
    return item.valid_until ? {type: name, name, valid_until: item.valid_until}
      : {type: name, name, valid_until: ""};
  });
}

function parseLines(text) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const [name, valid] = line.split("|");
    return {type: (name || "").trim(), name: (name || "").trim(), valid_until: (valid || "").trim()};
  }).filter((entry) => entry.name);
}

function formatLines(list) {
  return (list || []).map((item) => {
    const name = item.type || item.name || "";
    return item.valid_until ? `${name}|${item.valid_until}` : name;
  }).join("\n");
}

function setProfileCount() {
  const badge = byId("profile-count");
  badge.hidden = !companies.length;
  badge.textContent = companies.length;
}

function renderProfileList() {
  const container = byId("profile-cards");
  byId("profile-list-empty").hidden = companies.length > 0;
  container.replaceChildren(...companies.map((company) => {
    const card = node("button", "", "profile-card");
    card.type = "button";
    card.dataset.company = company.id;
    if (company.id === editingCompany) card.setAttribute("aria-current", "true");
    card.append(node("strong", company.name));
    const meta = node("span", "", "profile-card-meta");
    meta.textContent = `${company.qualifications} 项证照 · ${company.certifications} 项认证`;
    card.append(meta);
    const foot = node("span", "", "profile-card-foot");
    foot.textContent = company.updated_at ? `更新于 ${company.updated_at}` : "";
    card.append(foot);
    if (company.licence_names?.length) {
      const tags = node("span", "", "profile-tags");
      company.licence_names.slice(0, 3).forEach((name) => {
        const short = name.replace(/^中华人民共和国/, "").replace(/许可证$/, "");
        if (short) tags.append(node("i", short));
      });
      card.append(tags);
    }
    card.addEventListener("click", () => editCompany(company.id));
    return card;
  }));
}

async function loadCompanies() {
  try {
    companies = await api("/api/companies");
  } catch (error) {
    companies = [];
  }
  setProfileCount();
  renderProfileList();
  const saved = localStorage.getItem("tender-company");
  const known = companies.some((company) => company.id === saved);
  byId("company-select").replaceChildren(
    ...(companies.length
      ? companies.map((company) => new Option(company.name, company.id))
      : [new Option("还没有公司档案", "")]),
  );
  if (known) byId("company-select").value = saved;
  return companies;
}

function showProfiles(open) {
  byId("profiles-view").hidden = !open;
  document.body.classList.toggle("profiles-open", open);
  byId("open-profiles").setAttribute("aria-expanded", String(open));
}

async function editCompany(companyId) {
  editingCompany = companyId || "";
  byId("profile-editor-empty").hidden = Boolean(companyId);
  byId("profile-fields").hidden = !companyId;
  if (!companyId) {
    byId("profile-form-title").textContent = "新建档案";
    byId("profile-form-meta").textContent = "";
    byId("company-name").value = "";
    byId("company-code").value = "";
    byId("company-licences").value = "";
    byId("company-certs").value = "";
    byId("company-notes").value = "";
    byId("company-delete").hidden = true;
    byId("profile-form-title").textContent = "新建档案";
    byId("profile-fields").hidden = false;
    byId("profile-editor-empty").hidden = true;
    byId("company-status").textContent = "";
    renderProfileList();
    byId("company-name").focus();
    return;
  }
  try {
    const profile = await api(`/api/companies/${companyId}`);
    byId("company-name").value = profile.name || "";
    byId("company-code").value = profile.credit_code || "";
    byId("company-licences").value = formatLines(profile.qualifications);
    byId("company-certs").value = formatLines(profile.certifications);
    byId("company-notes").value = profile.notes || "";
    byId("profile-form-title").textContent = profile.name || "档案详情";
    byId("profile-form-meta").textContent = "修改后保存即可生效";
    byId("company-delete").hidden = false;
    byId("company-status").textContent = "";
    renderProfileList();
  } catch (error) {
    byId("company-status").textContent = error.message;
  }
}

byId("open-profiles").addEventListener("click", async () => {
  const open = byId("profiles-view").hidden;
  showProfiles(open);
  if (!open) return;
  await loadCompanies();
  if (!editingCompany && companies.length) await editCompany(companies[0].id);
  else if (!companies.length) await editCompany("");
});

byId("profiles-close").addEventListener("click", () => showProfiles(false));
byId("profile-new").addEventListener("click", () => editCompany(""));

byId("company-delete").addEventListener("click", async () => {
  if (!editingCompany) return;
  const target = companies.find((company) => company.id === editingCompany);
  if (!window.confirm(`确定删除「${target?.name || "这份档案"}」？此操作不可撤销。`)) return;
  try {
    await api(`/api/companies/${editingCompany}`, {method: "DELETE"});
    if (localStorage.getItem("tender-company") === editingCompany) {
      localStorage.removeItem("tender-company");
    }
    editingCompany = "";
    await loadCompanies();
    const next = companies[0]?.id || "";
    await editCompany(next);
    byId("profile-editor-empty").hidden = Boolean(next);
    if (!next) byId("profile-fields").hidden = true;
    if (currentDocument && currentDocument !== "sample") loadEligibility();
  } catch (error) {
    byId("company-status").textContent = error.message;
  }
});

byId("profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = byId("company-status");
  const submit = event.currentTarget.querySelector("button[type=submit]");
  const payload = {
    name: byId("company-name").value.trim(),
    credit_code: byId("company-code").value.trim(),
    qualifications: parseLines(byId("company-licences").value),
    certifications: parseLines(byId("company-certs").value),
    performance: [],
    notes: byId("company-notes").value.trim(),
  };
  if (!payload.name) {
    status.textContent = "请填写公司名称。";
    byId("company-name").focus();
    return;
  }
  submit.disabled = true;
  status.textContent = "正在保存…";
  try {
    const saved = editingCompany
      ? await api(`/api/companies/${editingCompany}`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      })
      : await api("/api/companies", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
    editingCompany = saved.id;
    localStorage.setItem("tender-company", saved.id);
    await loadCompanies();
    byId("profile-form-title").textContent = saved.name;
    byId("profile-form-meta").textContent = "已保存";
    byId("company-delete").hidden = false;
    status.textContent = "已保存。";
    if (currentDocument && currentDocument !== "sample") loadEligibility();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

/* ---------- 能否投标 ---------- */

const verdictLabels = {met: "满足", unmet: "不满足", unknown: "待确认"};
const verdictMarks = {met: "✓", unmet: "!", unknown: "?"};
let eligibilityRequest = 0;

function verdictCard(row) {
  const card = node("div", "", `elig-card ${row.status}`);
  const head = node("div", "", "elig-head");
  head.append(node("h3", row.title));
  head.append(node("span", verdictLabels[row.status], `elig-status ${row.status}`));
  card.append(head);
  card.append(node("p", row.reason, "elig-reason"));
  if (row.details?.length > 1) {
    const detail = node("div", "", "elig-detail");
    for (const item of row.details) {
      const line = node("div", "", item.status);
      line.append(node("i", verdictLabels[item.status]), node("span", item.reason));
      detail.append(line);
    }
    card.append(detail);
  }
  if (row.condition && row.reason && !row.reason.includes(row.condition)) {
    card.append(node("p", row.condition, "elig-cond"));
  }
  if (row.sources?.length) card.append(sourceButtons(row.sources, "原文 · "));
  return card;
}

function renderEligibility(data) {
  const summary = byId("eligibility-summary");
  summary.hidden = false;
  summary.className = `eligibility-summary ${data.verdict}`;
  const mark = node("div", verdictMarks[data.verdict] || "?", "verdict-mark");
  const text = node("div", "", "verdict-text");
  text.append(node("strong", data.headline),
    node("span", `第 ${data.package ?? "通用"} 标包 · 公司：${data.company.name || "未命名"}`));
  const counts = node("div", "", "verdict-counts");
  for (const [key, label] of Object.entries(verdictLabels)) {
    const value = data.counts[key] || 0;
    if (value) counts.append(node("span", `${label} ${value}`, `count-chip ${key}`));
  }
  summary.replaceChildren(mark, text, counts);

  const rows = byId("eligibility-rows");
  if (!data.rows.length) {
    rows.replaceChildren(node("p", "这个标包没有可自动比对的资质要求，请结合风险清单人工核对。", "muted"));
    return;
  }
  const order = {unmet: 0, unknown: 1, met: 2};
  rows.replaceChildren(...[...data.rows]
    .sort((a, b) => order[a.status] - order[b.status])
    .map(verdictCard));
}

async function loadEligibility() {
  const status = byId("eligibility-status");
  const noCompany = byId("company-select").value === "";
  if (currentDocument === "sample") {
    status.textContent = "样例文档不支持资质比对，请先导入自己的招标文件。";
    byId("eligibility-summary").hidden = true;
    byId("eligibility-rows").replaceChildren();
    return;
  }
  if (noCompany) {
    status.textContent = "还没有公司档案。点右上角「公司档案」新建一份，之后可反复用于所有招标文件。";
    byId("eligibility-summary").hidden = true;
    byId("eligibility-rows").replaceChildren();
    return;
  }
  const request = ++eligibilityRequest;
  const packageSelect = byId("eligibility-package");
  status.textContent = packageSelect.value ? "正在比对资质…" : "正在读取标包…";
  try {
    const listing = await api(`/api/documents/${currentDocument}/checklist`);
    if (request !== eligibilityRequest) return;
    const packages = listing.packages || [];
    const previous = packageSelect.value;
    packageSelect.replaceChildren(
      ...(packages.length
        ? packages.map((number) => new Option(`第 ${number} 标包`, number))
        : [new Option("项目通用（未划分标包）", "")]),
    );
    const stillThere = previous && [...packageSelect.options].some((option) => option.value === previous);
    if (stillThere) packageSelect.value = previous;
    const selected = packageSelect.value;
    const query = checklistQuery(selected);
    query.set("company", byId("company-select").value);
    status.textContent = "正在比对资质…";
    const data = await api(`/api/documents/${currentDocument}/eligibility?${query}`);
    if (request !== eligibilityRequest) return;
    renderEligibility(data);
    status.textContent = [...(data.revision_warnings || []), ...(data.revision_notes || [])].join(" ");
  } catch (error) {
    if (request === eligibilityRequest) {
      status.textContent = error.message;
      byId("eligibility-summary").hidden = true;
      byId("eligibility-rows").replaceChildren();
    }
  }
}

byId("company-select").addEventListener("change", (event) => {
  if (event.target.value) localStorage.setItem("tender-company", event.target.value);
  loadEligibility();
});
byId("eligibility-package").addEventListener("change", () => loadEligibility());
byId("company-manage").addEventListener("click", async () => {
  showProfiles(true);
  await loadCompanies();
  const selected = byId("company-select").value;
  await editCompany(selected || companies[0]?.id || "");
});

byId("team-login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  const status = byId("team-login-status");
  submit.disabled = true;
  status.textContent = "正在登录…";
  try {
    await api("/api/session/login", jsonOptions("POST", Object.fromEntries(new FormData(event.currentTarget))));
    location.reload();
  } catch (error) {
    status.textContent = error.message;
    submit.disabled = false;
  }
});

let registrationCaptcha = "";
async function refreshRegistrationCaptcha() {
  const refresh = byId("refresh-captcha");
  if (refresh.disabled) return;
  const submit = byId("submit-registration");
  const image = byId("registration-captcha");
  registrationCaptcha = "";
  submit.disabled = true;
  refresh.disabled = true;
  image.hidden = true;
  byId("team-register").elements.captcha_code.value = "";
  try {
    const result = await api("/api/session/captcha", {method: "POST"});
    registrationCaptcha = result.captcha_id;
    image.src = result.image;
    image.hidden = false;
    submit.disabled = false;
  } catch (error) {
    byId("registration-status").textContent = error.message;
  } finally {
    refresh.disabled = false;
  }
}

byId("show-registration").addEventListener("click", () => {
  byId("team-login").hidden = true;
  byId("team-register").hidden = false;
  byId("registration-status").textContent = "";
  byId("register-name").focus();
  refreshRegistrationCaptcha();
});
byId("refresh-captcha").addEventListener("click", () => {
  byId("registration-status").textContent = "";
  refreshRegistrationCaptcha();
});
byId("back-to-login").addEventListener("click", () => {
  byId("team-register").reset();
  byId("team-register").hidden = true;
  byId("team-login").hidden = false;
});
byId("team-register").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = byId("submit-registration");
  if (submit.disabled || !registrationCaptcha) return;
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form));
  submit.disabled = true;
  byId("refresh-captcha").disabled = true;
  try {
    await api("/api/session/register", jsonOptions("POST", {...payload, captcha_id: registrationCaptcha}));
    form.reset();
    form.hidden = true;
    const login = byId("team-login");
    login.hidden = false;
    login.elements.name.value = payload.name;
    byId("team-login-status").textContent = "注册成功，请登录";
    login.elements.password.focus();
  } catch (error) {
    byId("registration-status").textContent = error.message;
    byId("refresh-captcha").disabled = false;
    await refreshRegistrationCaptcha();
  } finally {
    byId("refresh-captcha").disabled = false;
  }
});

byId("team-logout").addEventListener("click", async () => {
  await api("/api/session/logout", {method: "POST"});
  location.reload();
});

async function startWorkspace() {
  const session = await api("/api/session").catch((error) => {
    if (error.status === 404) return {team: false};
    throw error;
  });
  teamMode = session.team;
  if (teamMode && !session.user) {
    byId("auth-view").hidden = false;
    byId("team-login").hidden = false;
    byId("team-register").hidden = true;
    byId("open-profiles").hidden = true;
    byId("team-logout").hidden = true;
    byId("workspace-identity").hidden = true;
    document.querySelector(".workspace").hidden = true;
    byId("login-name").focus();
    return;
  }
  byId("auth-view").hidden = true;
  byId("workspace-identity").hidden = false;
  document.querySelector(".workspace").hidden = false;
  if (teamMode) {
    teamUser = session.user;
    byId("workspace-identity").textContent = session.user.name;
    byId("team-logout").hidden = false;
    byId("team-groups").hidden = false;
    try {
      teamGroups = await api("/api/groups");
      renderGroups();
    } catch (error) {
      byId("group-status").textContent = error.message;
    }
  }
  if (!teamMode || session.user.admin) loadReview();
  loadCompanies();
  await loadDocuments();
  const jobId = sessionStorage.getItem("tender-import-job");
  if (jobId) monitorImport(() => api(`/api/imports/${jobId}`));
}

startWorkspace().catch((error) => { byId("import-status").textContent = error.message; });
