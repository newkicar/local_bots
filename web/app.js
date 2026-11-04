/* Local Dots 前端逻辑——无框架。一切经 /api，前端不直连引擎。 */
"use strict";

const TERMINAL = new Set(["reported", "failed", "rejected", "cancelled"]);
const ACTIVE = new Set(["queued", "running", "waiting-approval", "interrupted"]);
const REASON_LABEL = { blacklist: "黑名单拦截", restart_reconciliation: "重启核对" };
const H = (id) => document.getElementById(id);

const state = {
  token: localStorage.getItem("ld_token") || "",
  history: [],
  es: null,
  panel: "chat",
  busy: false,
  seenNotifs: new Set(),  // 任务通知卡去重（票 10，上限 200 FIFO 淘汰）
};

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
/* ---------- 零依赖 Markdown 渲染（票 10）：先 esc 再转换，链接 URL 白名单 ---------- */
function mdSafeUrl(u) {
  const s = String(u || "").trim();
  if (/^https?:\/\//i.test(s)) return s;
  if (s.toLowerCase().startsWith("mailto:")) return s;
  if (s === "#" || s.startsWith("/")) return s;
  return "#";
}
function mdInline(t) {
  let x = t;
  x = x.replace(/`([^`\n]+)`/g, (m, c) => "<code>" + c + "</code>");
  x = x.replace(/\*\*([^*\n]+)\*\*/g, (m, b) => "<b>" + b + "</b>");
  x = x.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, (m, pre, i) => pre + "<i>" + i + "</i>");
  x = x.replace(/\[([^\]\n]+)\]\(([^)\n]+)\)/g, (m, label, url) =>
    '<a href="' + mdSafeUrl(url) + '" target="_blank" rel="noopener">' + label + "</a>");
  return x;
}
function mdToHtml(src) {
  const lines = String(src == null ? "" : src).split("\n").map((l) => esc(l));
  let html = "";
  let para = [];
  let list = null;
  let fence = false;
  let code = "";
  const flushPara = () => { if (para.length) { html += "<p>" + para.join("<br>") + "</p>"; para = []; } };
  const flushList = () => {
    if (!list) return;
    html += "<" + list.type + ">" + list.items.map((it) => "<li>" + mdInline(it) + "</li>").join("") + "</" + list.type + ">";
    list = null;
  };
  const flushBoth = () => { flushPara(); flushList(); };
  for (const raw of lines) {
    if (/^```/.test(raw)) {
      if (fence) { html += '<pre class="md-pre"><code>' + code + "</code></pre>"; code = ""; fence = false; }
      else { flushBoth(); fence = true; }
      continue;
    }
    if (fence) { code += (code ? "\n" : "") + raw; continue; }
    if (!raw.trim()) { flushBoth(); continue; }
    const h = raw.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushBoth();
      const lvl = Math.min(h[1].length + 2, 6);
      html += "<h" + lvl + ' class="md-h">' + mdInline(h[2]) + "</h" + lvl + ">";
      continue;
    }
    const li = raw.match(/^(?:[-*\u2022]|\d+[.)])\s+(.*)$/);
    if (li) {
      flushPara();
      const type = /^\d/.test(raw) ? "ol" : "ul";
      if (!list || list.type !== type) { flushList(); list = { type, items: [] }; }
      list.items.push(mdInline(li[1]));
      continue;
    }
    flushList();
    para.push(raw);
  }
  if (fence && code) html += '<pre class="md-pre"><code>' + code + "</code></pre>";
  flushBoth();
  return html;
}


async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers);
  headers["Authorization"] = "Bearer " + state.token;
  if (opts.body != null) headers["Content-Type"] = "application/json";
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401) {
    showLogin("Token 失效，请重新输入");
    throw new Error("401");
  }
  if (!r.ok) throw new Error(r.status + " " + (await r.text().catch(() => "")));
  return r.status === 204 ? null : r.json();
}

/* ---------- 登录 ---------- */
function showLogin(msg) {
  if (msg) H("login-err").textContent = msg;
  H("login").classList.remove("hidden");
}
function hideLogin() { H("login").classList.add("hidden"); }

async function tryLogin(token) {
  state.token = token;
  localStorage.setItem("ld_token", token);
  await api("/api/health");
  hideLogin();
  connectSSE();
  refreshAll();
}

H("login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  H("login-err").textContent = "";
  const t = H("login-token").value.trim();
  if (!t) return;
  tryLogin(t).catch(() => showLogin("连接失败：检查服务是否运行（127.0.0.1:8000）与 Token"));
});
H("btn-logout").addEventListener("click", () => {
  localStorage.removeItem("ld_token");
  location.reload();
});

/* ---------- SSE 状态推送 ---------- */
function connectSSE() {
  if (state.es) state.es.close();
  const es = new EventSource("/api/events?token=" + encodeURIComponent(state.token));
  es.onopen = () => setConn(true);
  es.onerror = () => setConn(false); // 浏览器自动重连
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.type === "task_state") {
      refreshApprovals();
      refreshBadges();
      if (state.panel === "tasks" || state.panel === "results") refreshCurrent();
    }
    if (ev.type === "notification") chatTaskNotice(ev);
  };
  state.es = es;
}
function setConn(ok) {
  const c = H("conn");
  c.textContent = ok ? "● 在线（SSE）" : "● 离线";
  c.classList.toggle("ok", ok);
  c.classList.toggle("bad", !ok);
}
/* ---------- 聊天面板任务通知卡（SSE notification，票 10）：终态/挂起主动反馈，按 notification_id 去重 ---------- */
const NOTICE_KINDS = {
  task_completed: ["任务完成", "ok"],
  task_failed: ["任务失败", "bad"],
  task_rejected: ["任务被拒绝", "bad"],
  task_cancelled: ["任务已取消", "bad"],
  approval_requested: ["等待审批", "warn"],
};
function chatTaskNotice(ev) {
  const key = ev.notification_id || ev.task_id + ":" + ev.kind;
  if (state.seenNotifs.has(key)) return;
  if (state.seenNotifs.size >= 200) state.seenNotifs.delete(state.seenNotifs.values().next().value);
  state.seenNotifs.add(key);
  const meta = NOTICE_KINDS[ev.kind] || [ev.kind, "warn"];
  const p = ev.payload || {};
  let body;
  if (ev.kind === "task_completed") body = '<div class="tn-body md">' + mdToHtml(p.result) + "</div>";
  else if (ev.kind === "task_failed") body = '<div class="tn-body err">' + esc(p.error || "无错误明细") + "</div>";
  else if (ev.kind === "task_rejected") body = '<div class="tn-body err">' + esc(p.reject_reason || "无拒绝原因") + "</div>";
  else if (ev.kind === "task_cancelled") body = "<div class='tn-body'>Side-Effect Ledger " + (p.ledger || []).length + " 条（只追加不回滚）</div>";
  else if (ev.kind === "approval_requested") body = "<div class='tn-body'>" + esc(p.run_id || "") + "（原因 " + esc(p.wait_reason || "—") + "）</div>";
  else body = '<div class="tn-body">' + esc(JSON.stringify(p)) + "</div>";
  const isApproval = ev.kind === "approval_requested";
  const card = document.createElement("div");
  card.className = "msg assistant task-notice " + meta[1];
  card.innerHTML =
    '<div class="tn-head"><span class="status ' + meta[1] + '">' + meta[0] + '</span> ' +
    '<span class="muted">' + esc(ev.task_id || "") + "</span></div>" +
    body +
    '<div class="tn-actions"><button class="ghost">' + (isApproval ? "去审批" : "查看结果") + "</button></div>";
  card.querySelector("button").addEventListener("click", async () => {
    if (isApproval) { switchPanel("approvals"); refreshApprovals(); return; }
    switchPanel("results");
    await refreshResults();
    const el = H("res-" + ev.task_id);
    if (el) el.scrollIntoView({ block: "start", behavior: "smooth" });
    else openTask(ev.task_id);
  });
  H("chat-msgs").appendChild(card);
  card.scrollIntoView({ block: "end" });
}


/* ---------- 面板切换 ---------- */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => switchPanel(btn.dataset.panel));
});
function switchPanel(name) {
  state.panel = name;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.panel === name));
  ["chat", "tasks", "approvals", "results"].forEach((p) =>
    H("panel-" + p).classList.toggle("hidden", p !== name)
  );
  if (name === "tasks") refreshTasks();
  if (name === "approvals") refreshApprovals();
  if (name === "results") refreshResults();
}
function refreshCurrent() {
  if (state.panel === "tasks") refreshTasks();
  else if (state.panel === "approvals") refreshApprovals();
  else if (state.panel === "results") refreshResults();
}
function refreshAll() {
  refreshTasks(); refreshApprovals(); refreshResults(); refreshBadges();
}

/* ---------- 聊天面板 ---------- */
function addMsg(role, text, cls) {
  const m = document.createElement("div");
  m.className = "msg " + role + (cls ? " " + cls : "");
  m.textContent = text;
  H("chat-msgs").appendChild(m);
  m.scrollIntoView({ block: "end" });
  return m;
}
function addTaskChips(holder, taskIds) {
  if (!taskIds.length) return;
  const chips = document.createElement("div");
  chips.className = "chips";
  for (const id of taskIds) {
    const c = document.createElement("span");
    c.className = "chip";
    c.textContent = id;
    c.addEventListener("click", () => { switchPanel("tasks"); openTask(id); });
    chips.appendChild(c);
  }
  holder.appendChild(chips);
}
H("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.busy) return;
  const input = H("chat-input");
  const msg = input.value.trim();
  if (!msg) return;
  input.value = "";
  state.busy = true;
  addMsg("user", msg);
  state.history.push({ role: "user", content: msg });
  const pend = addMsg("assistant", "秘书处理中…", "pending");
  try {
    const r = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: msg, history: state.history.slice(0, -1) }),
    });
    pend.remove();
    const reply = r.reply || "（无文字回复）";
    const m = addMsg("assistant", reply);
    addTaskChips(m, r.task_ids || []);
    state.history.push({ role: "assistant", content: reply });
    if (state.history.length > 16) state.history.splice(0, state.history.length - 16);
    refreshTasks(); refreshBadges();
  } catch (err) {
    pend.remove();
    addMsg("assistant", "出错了：" + err.message);
  } finally {
    state.busy = false;
    H("chat-input").focus();
  }
});

/* ---------- 任务列表面板 ---------- */
async function refreshTasks() {
  try {
    const tasks = await api("/api/tasks");
    renderTasks(tasks);
  } catch { /* 401 已弹登录；网络错误保持旧列表 */ }
}
function renderTasks(tasks) {
  const box = H("task-list");
  box.innerHTML = "";
  if (!tasks.length) { box.innerHTML = '<div class="empty">暂无任务</div>'; return; }
  for (const t of tasks) box.appendChild(taskCard(t));
}
function taskCard(t) {
  const card = document.createElement("div");
  card.className = "card";
  card.id = "task-" + t.task_id;
  const head = document.createElement("div");
  head.style.cssText = "display:flex;gap:8px;align-items:center;cursor:pointer";
  head.innerHTML =
    '<span class="status ' + esc(t.status) + '">' + esc(t.status) + "</span>" +
    "<b>" + esc(t.task_id) + "</b><span class='muted'>" + esc(t.engine || "") + "</span>" +
    '<span class="muted" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
    esc(t.instruction || "") + "</span>";
  head.addEventListener("click", () => toggleTaskDetail(t.task_id, t));
  card.appendChild(head);
  const detail = document.createElement("div");
  detail.className = "detail hidden";
  detail.id = "detail-" + t.task_id;
  card.appendChild(detail);
  return card;
}
async function openTask(id) {
  await toggleTaskDetail(id, null);
  const el = H("task-" + id);
  if (el) el.scrollIntoView({ block: "start", behavior: "smooth" });
}
async function toggleTaskDetail(id, cached) {
  const detail = H("detail-" + id);
  if (!detail) return;
  const visible = !detail.classList.contains("hidden");
  detail.classList.add("hidden");
  if (visible) return;
  let t = cached;
  try { t = await api("/api/tasks/" + encodeURIComponent(id)); } catch { t = cached; }
  if (!t) return;
  detail.innerHTML = detailHtml(t);
  detail.classList.remove("hidden");
  bindTaskDetail(t, detail);
}
function detailHtml(t) {
  let runs = "";
  for (const r of t.runs || []) {
    runs +=
      '<div class="run-row"><span class="status ' + esc(r.state) + '">' + esc(r.state) +
      "</span> <span>第" + r.attempt + "次</span> <span class='muted'>来源 " + esc(r.origin) +
      "</span> " +
      (r.side_effects && r.side_effects.length ? "<span class='muted'>副作用 " + r.side_effects.length + " 条</span>" : "") +
      "</div>";
  }
  const notifs = (t.notifications || []).map(
    (n) => '<div class="kv">◆ ' + esc(n.kind) + "：" + esc(JSON.stringify(n.payload == null ? "" : n.payload)) + "</div>"
  ).join("");
  const followable = !ACTIVE.has(t.status);
  return (
    '<div class="kv"><b>引擎</b> ' + esc(t.engine) + "　<b>线程</b> " + esc(t.engine_thread_id || "—") +
    "　<b>可写</b> " + (t.writes_authorized ? "是" : "否") + "</div>" +
    '<div class="kv"><b>工作区</b> ' + esc(t.workspace || "—") + "</div>" +
    (t.result ? '<div class="kv"><b>结果</b></div><div class="md">' + mdToHtml(t.result) + "</div>" : "") +
    (t.error ? '<div class="kv" style="color:var(--bad)"><b>错误</b> ' + esc(t.error) + "</div>" : "") +
    (t.reject_reason ? '<div class="kv" style="color:var(--warn)"><b>拒绝原因</b> ' + esc(t.reject_reason) + "</div>" : "") +
    (runs ? '<div class="kv"><b>Run 历史</b></div>' + runs : "") +
    (notifs ? '<div class="kv"><b>通知</b></div>' + notifs : "") +
    '<div class="actions">' +
      (followable
        ? '<input id="fu-' + t.task_id + '" placeholder="在本任务内追问…"><button class="primary" id="fu-btn-' + t.task_id + '">追问</button>'
        : "<span class='muted'>任务进行中，不可追问（完成后即可在同一 Thread 内追问）</span>") +
      (ACTIVE.has(t.status) ? '<button class="danger" id="cx-' + t.task_id + '">取消任务</button>' : "") +
    "</div>"
  );
}
function bindTaskDetail(t, detail) {
  const fu = H("fu-" + t.task_id);
  if (fu) {
    H("fu-btn-" + t.task_id).addEventListener("click", async () => {
      const instr = fu.value.trim();
      if (!instr) return;
      fu.value = "";
      try {
        await api("/api/tasks/" + encodeURIComponent(t.task_id) + "/followup", {
          method: "POST", body: JSON.stringify({ instruction: instr }),
        });
        refreshTasks(); refreshBadges();
      } catch (err) { alert("追问失败：" + err.message); refreshTasks(); }
    });
  }
  const cx = H("cx-" + t.task_id);
  if (cx) {
    cx.addEventListener("click", async () => {
      try {
        const r = await api("/api/tasks/" + encodeURIComponent(t.task_id) + "/cancel", { method: "POST" });
        alert("已取消（Side-Effect Ledger " + (r.ledger || []).length + " 条，只追加不回滚）");
        refreshTasks(); refreshBadges();
      } catch (err) { alert("取消失败：" + err.message); }
    });
  }
}
H("btn-refresh-tasks").addEventListener("click", refreshTasks);

/* ---------- 审批面板 ---------- */
async function refreshApprovals() {
  let items;
  try { items = await api("/api/approvals"); } catch { return; }
  const box = H("approval-list");
  box.innerHTML = "";
  setBadge("approvals", items.length);
  if (!items.length) { box.innerHTML = '<div class="empty">没有待审批的 Run</div>'; return; }
  for (const a of items) box.appendChild(approvalCard(a));
}
function srcLabel(reason) {
  return REASON_LABEL[reason] || "来源：" + (reason || "未知");
}
function approvalCard(a) {
  const card = document.createElement("div");
  card.className = "card approval";
  const po = (a.pending_op && a.pending_op.pending_op) || a.pending_op || {};
  card.innerHTML =
    '<h3>' + esc(a.run_id) + "（" + esc(a.task_id) + "）" +
    (a.timeout_at ? ' <span class="muted">超时于 ' + new Date(a.timeout_at).toLocaleString() + "</span>" : "") + "</h3>" +
    '<div class="op">' + esc(po.kind || "—") + " " + esc(po.path || "—") +
    (po.detail ? "（" + esc(po.detail) + "）" : "") + "</div>" +
    '<div class="kv"><b>挂起原因</b> ' + esc(a.wait_reason || "—") + "</div>" +
    '<div class="kv src">来源：' + esc(srcLabel(a.wait_reason)) + "</div>" +
    (a.instruction ? '<div class="kv"><b>指令</b> ' + esc(a.instruction) + "</div>" : "") +
    (a.workspace ? '<div class="kv"><b>工作区</b> ' + esc(a.workspace) + "</div>" : "") +
    (a.approved_paths && a.approved_paths.length ? '<div class="kv"><b>已放行路径</b> ' + esc(a.approved_paths.join(", ")) + "</div>" : "") +
    (a.side_effects && a.side_effects.length ? '<div class="kv"><b>Side-Effect Ledger（' + a.side_effects.length + " 条）</b></div><pre class='ledger'>" + esc(JSON.stringify(a.side_effects, null, 1)) + "</pre>" : "") +
    '<div class="actions"><button class="primary" data-act="approve">批准</button>' +
    '<input data-reason placeholder="拒绝原因（必填）"><button class="warn" data-act="reject">拒绝</button></div>';
  card.querySelector('[data-act="approve"]').addEventListener("click", async () => {
    try { await api("/api/approvals/" + encodeURIComponent(a.run_id) + "/approve", { method: "POST" }); refreshAll(); }
    catch (err) { alert("批准失败：" + err.message); refreshApprovals(); }
  });
  card.querySelector('[data-act="reject"]').addEventListener("click", async () => {
    const reason = card.querySelector("[data-reason]").value.trim() || "（未填写）";
    try { await api("/api/approvals/" + encodeURIComponent(a.run_id) + "/reject", { method: "POST", body: JSON.stringify({ reason }) }); refreshAll(); }
    catch (err) { alert("拒绝失败：" + err.message); refreshApprovals(); }
  });
  return card;
}
H("btn-refresh-approvals").addEventListener("click", refreshApprovals);

/* ---------- 结果面板 ---------- */
async function refreshResults() {
  let tasks;
  try { tasks = await api("/api/tasks"); } catch { return; }
  const done = tasks.filter((t) => TERMINAL.has(t.status));
  setBadge("results", done.length);
  const box = H("result-list");
  box.innerHTML = "";
  if (!done.length) { box.innerHTML = '<div class="empty">暂无已终结任务</div>'; return; }
  for (const t of done.slice(0, 30)) box.appendChild(resultCard(t));
}
function resultCard(t) {
  const card = document.createElement("div");
  card.className = "card";
  card.id = "res-" + t.task_id;  // 聊天面板通知卡跳转目标
  const ledger = (t.runs || []).flatMap((r) => r.side_effects || []);
  card.innerHTML =
    '<div style="display:flex;gap:8px;align-items:center;cursor:pointer" class="rc-head">' +
    '<span class="status ' + esc(t.status) + '">' + esc(t.status) + "</span>" +
    "<b>" + esc(t.task_id) + "</b>" +
    '<span class="muted" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
    esc(t.instruction || "") + "</span></div>" +
    (t.result ? '<div class="kv"><b>结果</b></div><div class="md">' + mdToHtml(t.result) + "</div>" : "") +
    (t.error ? '<div class="kv" style="color:var(--bad)"><b>错误</b> ' + esc(t.error) + "</div>" : "") +
    (t.reject_reason ? '<div class="kv" style="color:var(--warn)"><b>拒绝</b> ' + esc(t.reject_reason) + "</div>" : "") +
    '<div class="kv"><b>Side-Effect Ledger（' + ledger.length + " 条，只追加）</b></div>" +
    '<pre class="ledger">' + esc(ledger.length ? JSON.stringify(ledger, null, 1) : "[]") + "</pre>";
  card.querySelector(".rc-head").addEventListener("click", () => {});
  return card;
}
H("btn-refresh-results").addEventListener("click", refreshResults);

/* ---------- 角标 / 徽章 ---------- */
async function refreshBadges() {
  let tasks;
  try { tasks = await api("/api/tasks"); } catch { return; }
  setBadge("tasks", tasks.filter((t) => ACTIVE.has(t.status)).length);
  let ap = 0;
  try { ap = (await api("/api/approvals")).length; } catch {}
  setBadge("approvals", ap);
}
function setBadge(name, n) {
  const b = H("badge-" + name);
  b.textContent = String(n);
  b.classList.toggle("hidden", n === 0);
}

/* ---------- 启动 ---------- */
if (state.token) {
  tryLogin(state.token).catch(() => showLogin(""));
} else {
  showLogin("");
}