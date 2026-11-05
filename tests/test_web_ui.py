"""票 07 验收（API 面 + 静态前端）：四面板数据契约、静态资源免鉴权、SSE 查询参数鉴权、
审批面板 approve/reject 即时反映、关掉页面重进任务仍在。

纪律（同票 01）：SSE 无限流断言走真 uvicorn 子进程 + httpx 真网络流；
TestClient 的 portal 会死锁无限流，只用它做普通请求与静态资源断言。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
