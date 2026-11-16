"""票 07 验收（API 面 + 静态前端）：四面板数据契约、静态资源免鉴权、SSE 查询参数鉴权、
审批面板 approve/reject 即时反映、关掉页面重进任务仍在。

纪律（同票 01）：SSE 无限流断言走真 uvicorn 子进程 + httpx 真网络流；
TestClient 的 portal 会死锁无限流，只用它做普通请求与静态资源断言。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.fakes import FakeEngine

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")
TERMINAL = ("reported", "failed", "cancelled", "rejected")
SSE_PORT = 18766


def _settings(name: str, **kw) -> Settings:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return Settings(
        ld_data_dir=os.path.join(d, "data"),
        ld_workspace_dir=os.path.join(d, "workspace"),
        ld_api_token="test-token",
        _env_file=None,
        **kw,
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _wait_terminal(c: TestClient, tid: str, want=TERMINAL, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    t: dict = {}
    while time.time() < deadline:
        t = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
        if t["status"] in want:
            return t
        time.sleep(0.1)
    raise AssertionError(f"task {tid} 未在 {timeout}s 内到达 {want}: {t['status']}")


def _start_uvicorn(settings: Settings, port: int) -> subprocess.Popen:
    """真子进程起 uvicorn（SSE 须真网络流；TestClient portal 死锁无限流，同票 01 纪律）。"""
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.fake_server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=os.getcwd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ,
             "LD_DATA_DIR": str(settings.ld_data_dir),
             "LD_WORKSPACE_DIR": str(settings.ld_workspace_dir),
             "LD_API_TOKEN": settings.ld_api_token,
             "LD_MAX_CONCURRENT_RUNS": str(settings.ld_max_concurrent_runs)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _wait_http(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/health", headers=_headers(), timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise AssertionError(f"uvicorn 未在 {timeout}s 内就绪")


def test_index_and_assets_served():
    """GET / 返回前端首页；四个面板与 app.js / style.css 齐备（静态免鉴权）。"""
    with TestClient(create_app(_settings("web07"))) as c:
        r = c.get("/")
        assert r.status_code == 200
        html = r.text
        for marker in ("id=\"panel-chat\"", "id=\"panel-tasks\"", "id=\"panel-approvals\"",
                       "id=\"panel-results\"", "id=\"login\"", "tabbar"):
            assert marker in html, marker
        js = c.get("/app.js")
        assert js.status_code == 200 and "EventSource" in js.text
        css = c.get("/style.css")
        assert css.status_code == 200 and "tabbar" in css.text


def test_static_open_but_api_and_sse_still_authed():
    """静态页免登录可取（不含密钥）；API 与 SSE 一律鉴权（SSE 走 ?token= 查询参数）。"""
    with TestClient(create_app(_settings("web07auth"))) as c:
        assert c.get("/").status_code == 200            # 静态免鉴权
        assert c.get("/api/tasks").status_code == 401   # API 仍鉴权
        assert c.get("/api/health", headers=_headers()).status_code == 200
        # SSE 鉴权失败在开流前即出（无流可死锁）；成功开流的断言见 test_sse_*（真回环）
        assert c.get("/api/events?token=wrong").status_code == 401


def test_sse_pushes_task_state_events():
    """提交任务后 SSE 推送 task_state 事件（前端实时刷新依据）；鉴权用 ?token=
    查询参数（浏览器 EventSource 发不了 Bearer header）；首帧为 retry 指令。"""
    settings = _settings("web07sse")
    proc = _start_uvicorn(settings, SSE_PORT)
    try:
        _wait_http(SSE_PORT)
        with httpx.Client(base_url=f"http://127.0.0.1:{SSE_PORT}", timeout=10) as hc:
            r = hc.post("/api/tasks", json={"instruction": "x", "engine": "fake"},
                       headers=_headers())
            assert r.status_code == 201
            tid = r.json()["task_id"]
            # EventSource 同款调用：?token= 查询参数；首帧须为 retry 指令（重连间隔），
            # 读到本任务 task_state 事件即走
            got = False
            with hc.stream("GET", "/api/events?token=test-token") as resp:
                assert resp.status_code == 200
                first = True
                for line in resp.iter_lines():
                    if first:
                        assert line.startswith("retry:"), line
                        first = False
                    if "task_state" in line and tid in line:
                        got = True
                        break
            assert got, "SSE 流未收到含 tid 的 task_state 事件"
            # 事件推送期间状态机照常推进到终态
            deadline = time.time() + 20
            t = {}
            while time.time() < deadline:
                t = hc.get(f"/api/tasks/{tid}", headers=_headers()).json()
                if t["status"] in TERMINAL:
                    break
                time.sleep(0.1)
            assert t["status"] == "reported"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_approvals_panel_contract_approve_and_reject():
    """审批面板：挂起 Run 列表契约；批准 → 任务继续到 reported；拒绝 → rejected + 原因落库。"""
    for name, action, want_status, reject_reason in (
        ("web07ap_ok", "approve", "reported", None),
        ("web07ap_rej", "reject", "rejected", "unsafe path"),
    ):
        fake = FakeEngine(suspend_times=1)
        settings = _settings(name, ld_approval_timeout_min=10.0)
        with TestClient(create_app(settings, {"fake": fake})) as c:
            t = c.post("/api/tasks", json={"instruction": "write secret", "engine": "fake"},
                       headers=_headers()).json()
            tid = t["task_id"]
            deadline = time.time() + 10
            approvals = []
            while time.time() < deadline:
                approvals = c.get("/api/approvals", headers=_headers()).json()
                if approvals:
                    break
                time.sleep(0.1)
            assert approvals, "挂起 Run 未出现在审批列表"
            a = approvals[0]
            # 前端契约：挂起原因（黑名单/重启核对）+ 超时时刻 + 指令 + 工作区 + Ledger
            assert a["run_id"].startswith("R") and a["task_id"] == tid
            assert a["kind"] == "write" and a["path"] == "C:\\secret"
            assert a["wait_reason"] == "fake-blacklist"
            assert a["timeout_at"] and a["instruction"] and a["workspace"]
            assert isinstance(a["side_effects"], list)
            if action == "approve":
                r = c.post(f"/api/approvals/{a['run_id']}/approve", headers=_headers())
                assert r.status_code == 200, r.text
            else:
                r = c.post(f"/api/approvals/{a['run_id']}/reject",
                           json={"reason": reject_reason}, headers=_headers())
                assert r.status_code == 200, r.text
            # 操作结果即时反映到任务状态
            assert _wait_terminal(c, tid, timeout=15)["status"] == want_status
            if action == "reject":
                t2 = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
                assert t2["reject_reason"] == reject_reason
            assert c.get("/api/approvals", headers=_headers()).json() == []  # 决策后列表清空


def test_cancel_and_results_contract_and_persistence():
    """取消返回 Ledger；结果面板契约（result/error/reject_reason + runs.side_effects）；
    关掉页面重进（新 TestClient = 重新打开浏览器）任务仍在。"""
    fake = FakeEngine(suspend=True)
    settings = _settings("web07persist", ld_approval_timeout_min=10.0)
    with TestClient(create_app(settings, {"fake": fake})) as c:
        t = c.post("/api/tasks", json={"instruction": "long job", "engine": "fake"},
                   headers=_headers()).json()
        tid = t["task_id"]
        c.post("/api/tasks", json={"instruction": "quick", "engine": "fake"},
               headers=_headers())
        time.sleep(0.3)  # 两任务均被假引擎挂起进 waiting-approval
        r = c.post(f"/api/tasks/{tid}/cancel", headers=_headers())
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled"
        assert isinstance(r.json()["ledger"], list) and r.json()["ledger"][0]["state"] == "cancelled"
        # 关页面重进：全新客户端读同一个 data 目录，任务不丢
        with TestClient(create_app(settings, {"fake": FakeEngine(suspend=True)})) as c2:
            tasks = c2.get("/api/tasks", headers=_headers()).json()
            assert tid in [x["task_id"] for x in tasks]
            t2 = c2.get(f"/api/tasks/{tid}", headers=_headers()).json()
            assert "runs" in t2 and all(isinstance(x["side_effects"], list) for x in t2["runs"])
            mine = next(x for x in tasks if x["task_id"] == tid)
            assert mine["status"] in TERMINAL
            # 结果面板契约：终态字段可读
            assert set(tasks[0].keys()) >= {"task_id", "status", "result", "error",
                                            "reject_reason", "runs"}


def test_frontend_no_secret_leak():
    """前端资产本身不含 API 密钥（密钥只存在于 .env / 浏览器 localStorage）。"""
    web_dir = Path(__file__).resolve().parent.parent / "web"
    for f in web_dir.iterdir():
        if f.is_file():
            text = f.read_bytes().decode("utf-8")
            assert "ld_api_token" not in text
            assert "local-dots-dev-token" not in text


def test_frontend_chat_notices_and_markdown_markers():
    """票 10：聊天面板任务主动反馈卡（SSE notification → 去重插卡）+ 零依赖 Markdown
    结果渲染（动态区一定线在浏览器跑）；此处做静态 marker 守门。"""
    with TestClient(create_app(_settings("web10"))) as c:
        js = c.get("/app.js")
        assert js.status_code == 200
        for marker in ("mdToHtml", "task-notice", "seenNotifs", "mdSafeUrl", "chatTaskNotice"):
            assert marker in js.text, marker
        css = c.get("/style.css")
        assert css.status_code == 200
        for marker in (".task-notice", "pre.md-pre", ".status.ok"):
            assert marker in css.text, marker


def test_frontend_selfheal_polling_markers():
    """票 07 回归：断线自愈兜底——SSE 重连补齐 / 周期轮询 / 亮屏唤醒；静态 marker 守门。"""
    with TestClient(create_app(_settings("web07poll"))) as c:
        js = c.get("/app.js")
        assert js.status_code == 200
        for marker in ("startPolling", "catchUp", "POLL_MS", "visibilitychange",
                       'window.addEventListener("focus"'):
            assert marker in js.text, marker


def test_frontend_auto_approval_mode_markers():
    """票 03 延伸：审批面板代审模式切换 UI（manual/auto_safe/yolo）+ 结果反馈；静态 marker 守门。"""
    with TestClient(create_app(_settings("web03auto"))) as c:
        html = c.get("/").text
        for marker in ('id="auto-mode-row"', 'data-mode="yolo"', 'id="auto-mode-note"'):
            assert marker in html, marker
        js = c.get("/app.js")
        assert js.status_code == 200
        for marker in ("refreshAutoMode", "auto-mode-btn", "/api/approval-mode"):
            assert marker in js.text, marker
        css = c.get("/style.css")
        assert css.status_code == 200
        for marker in (".auto-mode", ".auto-mode-note"):
            assert marker in css.text, marker


def test_frontend_scroll_arrow_markers():
    """悬浮回顶/回底控件：全面板共享一组浮动控件（贴 .wrap 滚动容器）+ 按可滚动量自动显隐；静态 marker 守门。"""
    with TestClient(create_app(_settings("web11arrow"))) as c:
        html = c.get("/").text
        for marker in ('id="to-top"', 'id="to-bottom"', 'id="scroll-arrows"'):
            assert marker in html, marker
        js = c.get("/app.js")
        assert js.status_code == 200
        for marker in ("bindScrollArrows", "scrollHeight", 'behavior: "smooth"'):
            assert marker in js.text, marker
        css = c.get("/style.css")
        assert css.status_code == 200
        for marker in (".scroll-arrows", ".arrow"):
            assert marker in css.text, marker


# ---------- DOM 桩回归：任务详情展开态不得被自动刷新全量重建收起（Node 跑真 web/app.js + 最小 DOM 桩，零框架纪律） ----------
# 桩的 _createdEls 逆向扫描 = 浏览器语义“被 innerHTML 清掉的节点查不到”；driver 模拟 10s 轮询 tick（同一 refreshTasks 全量重建路径）。
_DOM_STUB_JS = r"""function _escText(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function _makeEl(id) {
  const el = {
    id: id || "",
    _classes: new Set(),
    _text: "",
    _html: "",
    children: [],
    style: { cssText: "" },
    dataset: {},
    value: "",
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    addEventListener() {},
    querySelector() { return _makeEl(null); },
    querySelectorAll() { return []; },
    scrollIntoView() {},
    scrollTo() {},
    focus() {},
    remove() {},
    appendChild(c) { el.children.push(c); return c; },
  };
  el.classList = {
    add: (...ns) => ns.forEach((n) => el._classes.add(n)),
    remove: (...ns) => ns.forEach((n) => el._classes.delete(n)),
    contains: (n) => el._classes.has(n),
    toggle: (n, force) => {
      const want = force === undefined ? !el._classes.has(n) : !!force;
      if (want) el._classes.add(n); else el._classes.delete(n);
      return want;
    },
  };
  Object.defineProperty(el, "className", {
    get: () => Array.from(el._classes).join(" "),
    set: (v) => { el._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  Object.defineProperty(el, "textContent", {
    get: () => el._text,
    set: (v) => { el._text = String(v); el._html = ""; },
  });
  Object.defineProperty(el, "innerHTML", {
    get: () => (el._html !== "" ? el._html : _escText(el._text)),
    set: (v) => { el._html = String(v); el._text = ""; el.children = []; },
  });
  return el;
}
const _STATIC_IDS = new Set([
  "conn", "btn-logout", "wrap", "panel-chat", "chat-msgs", "chat-form", "chat-input",
  "panel-tasks", "btn-refresh-tasks", "task-list", "panel-approvals", "btn-refresh-approvals",
  "auto-mode-row", "auto-mode-note", "approval-list", "panel-results", "btn-refresh-results",
  "result-list", "badge-tasks", "badge-approvals", "badge-results", "scroll-arrows",
  "to-top", "to-bottom", "login", "login-form", "login-token", "login-err",
]);
const _createdEls = [];
const _staticEls = {};
function _lookup(id) {
  for (let i = _createdEls.length - 1; i >= 0; i--) { const e = _createdEls[i]; if (e.id === id) return e; }
  if (_STATIC_IDS.has(id)) return _staticEls[id] || (_staticEls[id] = _makeEl(id));
  return null;
}
globalThis.document = {
  hidden: false,
  getElementById: _lookup,
  createElement: (tag) => { const e = _makeEl(null); e.tagName = tag; _createdEls.push(e); return e; },
  querySelectorAll: () => [],
  addEventListener() {},
};
const _fetchCalls = [];
globalThis.fetch = async (path) => {
  _fetchCalls.push(String(path));
  if (String(path) === "/api/tasks") {
    return { ok: true, status: 200, json: async () => TASKS };
  }
  const m = String(path).match(/^\/api\/tasks\/([^/]+)$/);
  if (m) {
    const t = TASKS.find((x) => x.task_id === m[1]);
    if (t) return { ok: true, status: 200, json: async () => Object.assign({}, t, DETAIL_EXTRA) };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};
const _store = {};
globalThis.localStorage = {
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
  removeItem: (k) => { delete _store[k]; },
};
globalThis.window = { addEventListener() {}, location: { reload() {} } };
globalThis.EventSource = class { constructor() {} close() {} };
globalThis.alert = () => {};
const TASKS = [
  { task_id: "T1", status: "running", engine: "jarvis", instruction: "demo" },
  { task_id: "T2", status: "reported", engine: "codex", instruction: "done", result: "ok result" },
];
const DETAIL_EXTRA = {
  engine_thread_id: "th-test",
  writes_authorized: false,
  workspace: "D:\\ws",
  runs: [{ state: "reported", attempt: 1, origin: "chat", side_effects: [] }],
};"""

_DOM_DRIVER_JS = r"""(async () => {
  state.panel = "tasks";
  await refreshTasks();
  await toggleTaskDetail("T2", null);
  await toggleTaskDetail("T1", TASKS[0]);
  let d1 = document.getElementById("detail-T1");
  let d2 = document.getElementById("detail-T2");
  const openBefore = !!(d1 && d2) && !d1.classList.contains("hidden") && !d2.classList.contains("hidden");
  const dfBefore = _fetchCalls.filter((p) => p.indexOf("/api/tasks/") === 0).length;
  // 模拟 10s 轮询 tick / SSE task_state / catchUp：同一全量重建路径
  await refreshTasks();
  d1 = document.getElementById("detail-T1"); d2 = document.getElementById("detail-T2");
  const openAfter = !!(d1 && d2) && !d1.classList.contains("hidden") && !d2.classList.contains("hidden");
  const contentRestored = !!(d2 && d2.innerHTML.indexOf("fu-T2") >= 0);
  const dfAfter = _fetchCalls.filter((p) => p.indexOf("/api/tasks/") === 0).length;
  process.stdout.write(JSON.stringify({ openBefore, openAfter, contentRestored, detailFetchesAfterRefresh: dfAfter - dfBefore }));
  process.exit(openBefore && openAfter ? 0 : 1);
})().catch((e) => { process.stdout.write("DRIVER_FAIL " + String(e)); process.exit(2); });"""


def test_task_detail_expanded_state_survives_auto_refresh():
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable; cannot run DOM-stub regression")
    src = Path(__file__).resolve().parents[1].joinpath("web", "app.js").read_text(encoding="utf-8")
    d = os.path.join(BASE, "dom")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    with open(os.path.join(d, "driver.js"), "w", encoding="utf-8") as f:
        f.write(_DOM_STUB_JS + "\n" + src + "\n" + _DOM_DRIVER_JS)
    proc = subprocess.run([node, os.path.join(d, "driver.js")], capture_output=True, text=True, timeout=30)
    out = (proc.stdout or "").strip()
    assert proc.returncode == 0, "driver failed rc=%s out=%s err=%s" % (proc.returncode, out, proc.stderr)
    rep = json.loads(out)
    assert rep["openBefore"], "precondition not met: detail not open before refresh: " + out
    assert rep["openAfter"], "expanded detail collapsed after auto refresh (full re-render loses state): " + out
    assert rep["contentRestored"], "detail content not restored from list data: " + out
    assert rep["detailFetchesAfterRefresh"] == 0, "restore must reuse list data, not re-fetch details: " + out
_RACE_DRIVER_JS = r"""(async () => {
  state.panel = "tasks";
  await refreshTasks();
  const p = toggleTaskDetail("T2", null);  // expand in-flight (fetch pending)
  await refreshTasks();                     // auto refresh rebuilds DOM while in-flight
  await p;                                  // backfill must land on the new DOM node
  const d2 = document.getElementById("detail-T2");
  const open = !!(d2) && !d2.classList.contains("hidden") && d2.innerHTML.indexOf("fu-T2") >= 0;
  process.stdout.write(JSON.stringify({ raceOk: open }));
  process.exit(open ? 0 : 1);
})().catch((e) => { process.stdout.write("DRIVER_FAIL " + String(e)); process.exit(2); });"""


def test_task_detail_toggle_race_survives_rebuild():
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable; cannot run DOM-stub regression")
    src = Path(__file__).resolve().parents[1].joinpath("web", "app.js").read_text(encoding="utf-8")
    d = os.path.join(BASE, "dom-race")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    with open(os.path.join(d, "driver.js"), "w", encoding="utf-8") as f:
        f.write(_DOM_STUB_JS + "\n" + src + "\n" + _RACE_DRIVER_JS)
    proc = subprocess.run([node, os.path.join(d, "driver.js")], capture_output=True, text=True, timeout=30)
    out = (proc.stdout or "").strip()
    assert proc.returncode == 0, "driver failed rc=%s out=%s err=%s" % (proc.returncode, out, proc.stderr)
    rep = json.loads(out)
    assert rep["raceOk"], "detail did not open on rebuilt DOM node after in-flight toggle + refresh: " + out


def test_frontend_composer_sticky_and_static_no_cache():
    with TestClient(create_app(_settings("web12sticky"))) as c:
        js = c.get("/app.js")
        assert js.status_code == 200
        for marker in ("detailCache", "chatNearBottom", "openDetails"):
            assert marker in js.text, marker
        css = c.get("/style.css")
        assert css.status_code == 200
        assert re.search(r"\.chat-form\s*\{[^}]*?position:\s*sticky", css.text), ".chat-form must be sticky"
        for path in ("/style.css", "/app.js"):
            r = c.get(path)
            assert r.headers.get("cache-control", "").lower() == "no-cache", path


def test_chat_reply_follow_and_panel_width():
    """发送时贴底：AI 回话到达自动跟底（期间插入的任务通知卡不打断跟随）；对话区不再窄于常规面板。"""
    with TestClient(create_app(_settings("web13follow"))) as c:
        js = c.get("/app.js")
        assert js.status_code == 200
        for marker in ("const stick = chatNearBottom();", "if (stick) m.scrollIntoView", "if (stick) em.scrollIntoView"):
            assert marker in js.text, marker
        css = c.get("/style.css")
        assert css.status_code == 200
        panel_m = re.search(r"\.panel\s*\{[^}]*?max-width:\s*(\d+)px", css.text)
        chat_m = re.search(r"#panel-chat\s*\{[^}]*?max-width:\s*(\d+)px", css.text)
        chat_w = int(chat_m.group(1)) if chat_m else (int(panel_m.group(1)) if panel_m else 0)
        assert chat_w >= 800, "chat panel narrower than standard panel: %s" % chat_w
