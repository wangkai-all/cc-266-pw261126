/* 用途视图脱敏策略引擎 —— 前端逻辑（原生 JS，无第三方依赖）。
   前端只负责构造结构化查询；所有规则强制都在后端执行器完成。 */
"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, body) => {
  const opt = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let catalog = null;
const queryState = { filters: [], groupBy: [], aggregations: [], select: [] };

// ------------------------------------------------------------- 页签
document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  btn.classList.add("active");
  $(`tab-${btn.dataset.tab}`).classList.add("active");
  if (btn.dataset.tab === "policy") loadVersionList();
  if (btn.dataset.tab === "jobs") refreshJobs();
}));

// ------------------------------------------------------------- 全局状态
async function refreshStatus() {
  const health = await api("/api/health");
  $("global-status").textContent =
    `活动策略版本：v${health.active_version ?? "未发布"}`;
  catalog = await api("/api/purpose/catalog");
  fillPurposeTables();
}

function currentCatalog() {
  return catalog.purposes[$("q-purpose").value];
}
function currentTable() {
  return currentCatalog().tables[$("q-table").value];
}

function fillPurposeTables() {
  const purpose = $("q-purpose").value;
  const pconf = catalog.purposes[purpose];
  $("q-table").innerHTML = Object.keys(pconf.tables)
    .map((t) => `<option>${t}</option>`).join("");
  $("q-rule-hint").innerHTML =
    `当前用途 k=<b>${pconf.k}</b>，明细查询：<b>${pconf.allow_detail ? "允许" : "禁止"}</b>；` +
    `策略版本 v${catalog.version}。分组结果不足 k 人的小组将被整体抑制。`;
  $("q-mode").value = pconf.allow_detail ? "detail" : "group";
  toggleMode();
  fillColumnSelects();
}

function fillColumnSelects() {
  const cols = currentTable().columns;
  const opts = cols.map((c) => `<option>${c}</option>`).join("");
  $("f-col").innerHTML = opts;
  $("g-col").innerHTML = opts;
  $("a-col").innerHTML = opts;
  $("d-col").innerHTML = opts;
}

$("q-purpose").addEventListener("change", () => {
  resetQueryState();
  fillPurposeTables();
});
$("q-table").addEventListener("change", () => {
  resetQueryState();
  fillColumnSelects();
});
$("q-mode").addEventListener("change", toggleMode);
function toggleMode() {
  const detail = $("q-mode").value === "detail";
  $("detail-card").hidden = !detail;
  $("group-card").hidden = detail;
}
function resetQueryState() {
  queryState.filters = [];
  queryState.groupBy = [];
  queryState.aggregations = [];
  queryState.select = [];
  renderChips();
}

// ------------------------------------------------------------- 查询构造
$("f-add").onclick = () => {
  const op = $("f-op").value;
  const raw = $("f-value").value.trim();
  let value = raw;
  if (op === "in") value = raw.split(",").map((s) => s.trim()).filter(Boolean);
  else if (!isNaN(raw) && raw !== "") value = Number(raw);
  queryState.filters.push({ column: $("f-col").value, op, value });
  renderChips();
};
$("g-add").onclick = () => {
  const c = $("g-col").value;
  if (!queryState.groupBy.includes(c)) queryState.groupBy.push(c);
  renderChips();
};
$("a-add").onclick = () => {
  queryState.aggregations.push({
    func: $("a-func").value,
    column: $("a-col").value,
    alias: $("a-alias").value || `${$("a-func").value}_${$("a-col").value}`,
  });
  renderChips();
};
$("d-add").onclick = () => {
  const c = $("d-col").value;
  if (!queryState.select.includes(c)) queryState.select.push(c);
  renderChips();
};

function renderChips() {
  $("f-list").innerHTML = queryState.filters.map((f, i) =>
    `<li>${esc(f.column)} ${esc(f.op)} ${esc(JSON.stringify(f.value))}
     <button data-k="filters" data-i="${i}">×</button></li>`).join("");
  $("g-list").innerHTML = queryState.groupBy.map((c, i) =>
    `<li>${esc(c)}<button data-k="groupBy" data-i="${i}">×</button></li>`).join("");
  $("a-list").innerHTML = queryState.aggregations.map((a, i) =>
    `<li>${esc(a.func)}(${esc(a.column)}) AS ${esc(a.alias)}
     <button data-k="aggregations" data-i="${i}">×</button></li>`).join("");
  $("d-list").innerHTML = queryState.select.map((c, i) =>
    `<li>${esc(c)}<button data-k="select" data-i="${i}">×</button></li>`).join("");
  document.querySelectorAll(".chips button").forEach((b) =>
    b.addEventListener("click", () => {
      queryState[b.dataset.k].splice(Number(b.dataset.i), 1);
      renderChips();
    }));
}

function buildQuery() {
  const q = { table: $("q-table").value, limit: 200,
              filters: queryState.filters };
  if ($("q-mode").value === "group") {
    q.group_by = queryState.groupBy;
    q.aggregations = queryState.aggregations;
  } else {
    q.select = queryState.select.length ? queryState.select : ["*"];
  }
  return q;
}

function renderTable(target, rows) {
  if (!rows.length) {
    target.innerHTML = "<tr><td>无记录（可能被规则抑制）</td></tr>";
    return;
  }
  const cols = Object.keys(rows[0]);
  target.innerHTML = `<thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}
    </tr></thead><tbody>${rows.map((r) =>
    `<tr>${cols.map((c) => `<td title="${esc(r[c])}">${esc(r[c])}</td>`).join("")}</tr>`
  ).join("")}</tbody>`;
}

async function runQuery() {
  $("q-error").hidden = true;
  try {
    const res = await api("/api/query", {
      purpose: $("q-purpose").value, query: buildQuery() });
    renderTable($("q-result"), res.rows);
    const extra = res.suppressed_groups
      ? `，被 k 匿名抑制小组 <b>${res.suppressed_groups}</b> 个` : "";
    $("q-meta").innerHTML =
      `返回 ${res.rows.length} 行 · 策略 v${res.policy_version} · ` +
      `缓存${res.cache_hit ? "命中" : "未命中"}${extra}`;
    $("q-sql").textContent = res.sql;
  } catch (err) {
    $("q-error").hidden = false;
    $("q-error").textContent = err.message;
    $("q-result").innerHTML = "";
    $("q-sql").textContent = "";
  }
}
$("q-run").onclick = runQuery;

$("q-export").onclick = async () => {
  try {
    const res = await api("/api/jobs", {
      purpose: $("q-purpose").value, query: buildQuery() });
    alert(`导出任务已提交：${res.job_id}\n到「导出任务与审计」页签查看`);
  } catch (err) {
    alert(err.message);
  }
};

// ------------------------------------------------------------- 策略管理
async function loadVersionList(selectVersion) {
  const data = await api("/api/policies");
  $("p-version").innerHTML = data.versions.map((v) =>
    `<option value="${v.version}" ${v.status === "active" ? "selected" : ""}>
      v${v.version} · ${v.status}</option>`).join("");
  $("p-versions").innerHTML = `<thead><tr>
    <th>版本</th><th>状态</th><th>备注</th><th>创建</th><th>发布时间</th>
    </tr></thead><tbody>${data.versions.map((v) => `<tr>
    <td>v${v.version}</td>
    <td><span class="badge ${v.status}">${v.status}</span></td>
    <td>${esc(v.note || "")}</td>
    <td>${new Date(v.created_at * 1000).toLocaleString()}</td>
    <td>${v.published_at ? new Date(v.published_at * 1000).toLocaleString() : "-"}</td>
    </tr>`).join("")}</tbody>`;
  if (selectVersion) $("p-version").value = String(selectVersion);
}

$("p-load").onclick = async () => {
  const v = Number($("p-version").value);
  const data = await api(`/api/policies/${v}`);
  $("p-editor").value = JSON.stringify(data.doc, null, 2);
  $("p-msg").textContent = `已载入 v${v}`;
};

$("p-upgrade").onclick = async () => {
  const data = await api("/api/policies/seed-upgrade", {});
  $("p-editor").value = JSON.stringify(data.draft, null, 2);
  await loadVersionList(data.draft.version);
  $("p-msg").textContent = "已生成收紧草稿，校验并预览后再发布";
};

function editorDoc() {
  try {
    return JSON.parse($("p-editor").value);
  } catch (err) {
    throw new Error(`JSON 语法错误: ${err.message}`);
  }
}

$("p-validate").onclick = async () => {
  try {
    // 存草稿即触发后端全量校验
    const doc = editorDoc();
    await api("/api/policies/draft", { doc });
    $("p-msg").textContent = `校验通过，草稿 v${doc.version} 已保存（未发布）`;
    await loadVersionList();
  } catch (err) { $("p-msg").textContent = "校验失败：" + err.message; }
};

$("p-save").onclick = async () => {
  try {
    const doc = editorDoc();
    await api("/api/policies/draft", { doc });
    $("p-msg").textContent = "草稿已保存，不影响线上查询";
    await loadVersionList();
  } catch (err) { $("p-msg").textContent = err.message; }
};

$("p-preview").onclick = async () => {
  try {
    const report = await api("/api/query/preview-policy", { doc: editorDoc() });
    $("p-preview").innerHTML = `<p class="hint">草稿 v${report.draft_version}
      对比现行 v${report.current_version ?? "无"}（同一代表性场景）</p>
      <div class="preview-grid">${Object.entries(report.purposes).map(
      ([purpose, e]) => `<div class="preview-box"><h4>${esc(purpose)}</h4>
        ${renderSide("现行", e.current)}${renderSide("草稿", e.draft)}</div>`
    ).join("")}</div>`;
  } catch (err) { $("p-preview").textContent = err.message; }
};

function renderSide(label, s) {
  if (s.error) return `<p><b>${label}：</b><span class="error-box">${esc(s.error)}</span></p>`;
  return `<p><b>${label}：</b>k=${s.k}，返回 ${s.returned_rows} 行，
    抑制小组 ${s.suppressed_groups} 个，明细=${s.allow_detail ? "允许" : "禁止"}</p>`;
}

$("p-publish").onclick = async () => {
  try {
    const doc = editorDoc();
    if (!confirm(`确认发布 v${doc.version}？\n发布后所有用途立即按新规则出数，` +
        "旧缓存清空、历史导出撤回，操作不可旁路。")) return;
    const res = await api("/api/policies/publish", { doc });
    $("p-msg").textContent = `已发布，活动版本 v${res.active_version}`;
    await Promise.all([loadVersionList(), refreshStatus()]);
  } catch (err) { $("p-msg").textContent = "发布失败：" + err.message; }
};

// ------------------------------------------------------------- 任务与审计
async function refreshJobs() {
  const data = await api("/api/jobs");
  $("j-table").innerHTML = `<thead><tr>
    <th>任务</th><th>用途</th><th>状态</th><th>请求版本</th><th>执行版本</th>
    <th>缓存</th><th>行数</th><th>错误/说明</th><th>下载</th>
    </tr></thead><tbody>${data.jobs.map((j) => `<tr>
    <td title="${esc(j.id)}">${esc(j.id.slice(0, 8))}</td>
    <td>${esc(j.purpose)}</td>
    <td><span class="badge ${j.status}">${j.status}</span></td>
    <td>v${j.requested_version}</td>
    <td>${j.executed_version ? "v" + j.executed_version : "-"}</td>
    <td>${j.cache_hit === null ? "-" : j.cache_hit ? "命中" : "未命中"}</td>
    <td>${j.row_count ?? "-"}</td>
    <td title="${esc(j.error || "")}">${esc(j.error || "")}</td>
    <td>${j.status === "done"
      ? `<a class="dl" href="/api/jobs/${j.id}/download">CSV</a>` : "-"}</td>
    </tr>`).join("")}</tbody>`;
}
$("j-refresh").onclick = refreshJobs;

$("a-refresh").onclick = async () => {
  const data = await api("/api/audit");
  $("a-table").innerHTML = `<thead><tr>
    <th>时间</th><th>用途</th><th>动作</th><th>请求版本</th><th>执行版本</th>
    <th>缓存</th><th>行数</th><th>说明</th>
    </tr></thead><tbody>${data.audit.map((a) => `<tr>
    <td>${new Date(a.ts * 1000).toLocaleTimeString()}</td>
    <td>${esc(a.purpose)}</td><td>${esc(a.action)}</td>
    <td>${a.requested_version ? "v" + a.requested_version : "-"}</td>
    <td>${a.executed_version ? "v" + a.executed_version : "-"}</td>
    <td>${a.cache_hit === null ? "-" : a.cache_hit ? "命中" : "未命中"}</td>
    <td>${a.row_count ?? "-"}</td>
    <td title="${esc(a.detail || "")}">${esc(a.detail || "")}</td></tr>`).join("")}
    </tbody>`;
};

// 定时刷新任务状态
setInterval(() => {
  if ($("tab-jobs").classList.contains("active")) refreshJobs();
}, 2500);

refreshStatus().then(() => loadVersionList()).catch((err) => {
  $("global-status").textContent = "后端未就绪：" + err.message;
});
