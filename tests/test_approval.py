"""票 03：审批门禁与 Side-Effect Ledger（approve/reject/timeout/cancel/数据驱动黑名单）。"""
from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from app.adapters.base import EngineAdapter, RunSuspended
from app.adapters.codex import CodexAdapter
from app.main import create_app
from app.permissions import Blacklist
from tests.test_api_skeleton import _headers, _settings, _wait_terminal
from tests.test_codex_adapter import FakeSpawn, _codex_events, _wait_status


def _client(settings, spawn, instruction: str = "写公司目录"):
    """黑名单制住 workspace 根目录（粗粒度前缀）后建 app；提交任务即停在 waiting-approval。"""
    bl = settings.blacklist_path
    bl.parent.mkdir(parents=True, exist_ok=True)
    bl.write_text(json.dumps([str(settings.ld_workspace_dir)]), encoding="utf-8")
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    return create_app(settings, {"codex": adapter})


def _wait_waiting(c, tid: str):
    t = _wait_status(c, tid, "waiting-approval")
    run = t["runs"][0]
    assert run["state"] == "waiting-approval"
    return t, run


def _submit(c, instruction: str = "写公司目录", engine: str = "codex") -> str:
    r = c.post("/api/tasks", json={"instruction": instruction, "engine": engine},
               headers=_headers())
    assert r.status_code == 201, r.text
    return r.json()["task_id"]


def test_approve_resumes_with_workspace_write():
    settings = _settings("approval-approve")
    spawn = FakeSpawn(events=_codex_events())
    app = _client(settings, spawn)
    with TestClient(app) as c:
        tid = _submit(c)
        t, run = _wait_waiting(c, tid)
        rid = run["run_id"]
        assert spawn.calls == []  # 未审批前子进程未启动

        approvals = c.get("/api/approvals", headers=_headers()).json()
        assert len(approvals) == 1
        a = approvals[0]
        assert a["run_id"] == rid and a["task_id"] == tid
        assert a["kind"] == "task_workspace"
        assert a["path"] == t["workspace"]
        assert a["wait_reason"] == "blacklist"
        assert a["side_effects"] == []
        assert a["timeout_at"] is not None

        r2 = c.post(f"/api/approvals/{rid}/approve", headers=_headers())
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["status"] == "resumed"
        assert body["approved_paths"] == [t["workspace"]]

        fin = _wait_terminal(c, tid)
        assert fin["status"] == "reported", fin["status"]
        assert len(spawn.calls) == 1
        cmd = spawn.calls[0]["cmd"]
        assert cmd[cmd.index("-s") + 1] == "workspace-write"  # 批准后才放开写

        run2 = c.get(f"/api/runs/{rid}", headers=_headers()).json()
        assert run2["decision"] == "approved"
        kinds = [e["kind"] for e in run2["side_effects"]]
        assert "shell" in kinds  # Side-Effect Ledger 如实记录已发生副作用


def test_reject_stops_and_keeps_ledger():
    settings = _settings("approval-reject")
    spawn = FakeSpawn(events=_codex_events())
    app = _client(settings, spawn)
    with TestClient(app) as c:
        tid = _submit(c)
        t, run = _wait_waiting(c, tid)
        rid = run["run_id"]

        r2 = c.post(f"/api/approvals/{rid}/reject", json={"reason": "不让写"},
                    headers=_headers())
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["status"] == "rejected"
        assert body["reject_reason"] == "不让写"
        assert body["side_effects"] == []

        fin = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
        assert fin["status"] == "rejected"
        assert fin["reject_reason"] == "不让写"
        assert fin["runs"][0]["state"] == "rejected"
        assert spawn.calls == []  # 拒绝后当前步即停，后续不再执行

        notifs = c.get("/api/notifications", params={"task_id": tid},
                       headers=_headers()).json()
        kinds = [n["kind"] for n in notifs]
        assert "approval_requested" in kinds and "task_rejected" in kinds
        rej = next(n for n in notifs if n["kind"] == "task_rejected")
        assert rej["payload"]["reject_reason"] == "不让写"
        assert rej["payload"]["wait_reason"] == "blacklist"
        assert "side_effects" in rej["payload"]


def test_timeout_auto_rejects():
    settings = _settings("approval-timeout", ld_approval_timeout_min=0.05)  # 3s：足够观察到 waiting-approval
    spawn = FakeSpawn(events=_codex_events())
    app = _client(settings, spawn)
    app.state.gate.sweep_interval = 0.05  # 测试接缝：加速清扫（默认 15s）
    with TestClient(app) as c:
        tid = _submit(c)
        _wait_waiting(c, tid)
        time.sleep(0.3)  # 等至少一轮 sweep_once 落 rejected
        t = _wait_terminal(c, tid, want=("rejected",), timeout=10.0)
        assert t["status"] == "rejected"
        assert t["reject_reason"] == "approval_timeout"
        run = t["runs"][0]
        assert run["state"] == "rejected"
        assert "approval_timeout" in (run["error"] or "")
        assert spawn.calls == []  # 超时未处理 = 默认拒绝，全程未执行


def test_cancel_waiting_task_stops_with_ledger():
    settings = _settings("approval-cancel")
    spawn = FakeSpawn(events=_codex_events())
    app = _client(settings, spawn)
    with TestClient(app) as c:
        tid = _submit(c)
        t, run = _wait_waiting(c, tid)
        rid = run["run_id"]

        rc = c.post(f"/api/tasks/{tid}/cancel", headers=_headers())
        assert rc.status_code == 200, rc.text
        body = rc.json()
        assert body["status"] == "cancelled"
        assert len(body["ledger"]) == 1
        assert body["ledger"][0]["run_id"] == rid
        assert body["ledger"][0]["state"] == "cancelled"
        assert "side_effects" in body["ledger"][0]

        fin = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
        assert fin["status"] == "cancelled"
        assert spawn.calls == []  # 取消语义与拒绝一致：即停 + Ledger


def test_blacklist_data_driven_no_code_change():
    settings = _settings("approval-bl-data")
    bl = Blacklist(settings.blacklist_path)
    assert bl.entries() == []
    assert bl.add_entry(r"D:\corp-data") is True
    assert bl.entries() == [r"D:\corp-data"]
    assert bl.matches(r"D:\corp-data\x\y") is True
    assert bl.add_entry(r"D:\corp-data\sub") is False  # 边界已被覆盖，不重复追加
    assert bl.add_entry(r"C:\Windows") is True
    assert bl.entries() == [r"D:\corp-data", r"C:\Windows"]
    assert bl.matches(r"C:\Windows\System32") is True


# 代审（auto_safe / yolo）：自动放行与手动批准走同一条 gate.approve 路径（票 03 延伸）

class JarvisLikeAdapter(EngineAdapter):
    """假 jarvis 引擎：首次 execute 携结构化动作挂起，恢复后直接返回（不 import 真实 Jarvis）。"""
    name = "jarvislike"

    def __init__(self, actions: list | None = None) -> None:
        self.actions = actions
        self.notes: list = []

    async def ensure_thread(self, task, run, hooks):
        thread = task["task_id"]
        await hooks.thread_bound(thread)
        return thread

    async def execute(self, task, run, thread, hooks, *, resume_note=None) -> str:
        if resume_note is None and self.actions is not None:
            raise RunSuspended(
                {"kind": "jarvis_interrupt", "path": task["workspace"],
                 "detail": "jarvis HITL 审批点", "actions": self.actions},
                "jarvis_interrupt",
            )
        self.notes.append(resume_note)
        return "after-auto"


def _jarvis_client(settings, adapter):
    return create_app(settings, {"jarvislike": adapter})


def test_yolo_auto_approves_without_manual_call():
    """yolo：无需手动审批，sweep 自动放行（含危险动作，风险自负）。"""
    settings = _settings("auto-yolo", ld_approval_mode="yolo")
    adapter = JarvisLikeAdapter(actions=[{"tool": "write_file", "arg": "C:\\secret"}])
    app = _jarvis_client(settings, adapter)
    app.state.gate.sweep_interval = 0.05
    with TestClient(app) as c:
        tid = _submit(c, "yolo 任务", engine="jarvislike")
        fin = _wait_terminal(c, tid)
        assert fin["status"] == "reported", fin["status"]
        assert len(adapter.notes) == 1 and adapter.notes[0].startswith("[代审]")
        assert "auto:yolo" in adapter.notes[0]
        rid = fin["runs"][0]["run_id"]
        run2 = c.get(f"/api/runs/{rid}", headers=_headers()).json()
        assert run2["decision"] == "approved"


def test_auto_safe_releases_readonly_commands_only():
    """auto_safe：全部动作均为只读 execute 命令 → sweep 自动放行。"""
    settings = _settings("auto-safe-ro", ld_approval_mode="auto_safe")
    adapter = JarvisLikeAdapter(actions=[{"tool": "execute", "arg": "git status"}])
    app = _jarvis_client(settings, adapter)
    app.state.gate.sweep_interval = 0.05
    with TestClient(app) as c:
        tid = _submit(c, "auto_safe 任务", engine="jarvislike")
        fin = _wait_terminal(c, tid)
        assert fin["status"] == "reported", fin["status"]
        assert len(adapter.notes) == 1 and "auto:auto_safe" in adapter.notes[0]


def test_auto_safe_keeps_file_write_manual():
    """auto_safe：文件写动作不自动放行，须等手动（代审不得越权）。"""
    settings = _settings("auto-safe-write", ld_approval_mode="auto_safe")
    adapter = JarvisLikeAdapter(actions=[{"tool": "write_file", "arg": "C:\\secret"}])
    app = _jarvis_client(settings, adapter)
    app.state.gate.sweep_interval = 0.05
    with TestClient(app) as c:
        tid = _submit(c, "写文件任务", engine="jarvislike")
        t, run = _wait_waiting(c, tid)
        rid = run["run_id"]
        time.sleep(0.5)  # 多轮 sweep：write_file 不得被自动放行
        t2 = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
        assert t2["status"] == "waiting-approval", t2["status"]
        assert adapter.notes == []
        r = c.post(f"/api/approvals/{rid}/approve", headers=_headers())
        assert r.status_code == 200, r.text
        fin = _wait_terminal(c, tid)
        assert fin["status"] == "reported", fin["status"]
        assert len(adapter.notes) == 1 and adapter.notes[0].startswith("[审批]")


def test_approval_mode_api_switch_and_guard():
    """运行期模式切换 API：GET 当前 + POST 切换；非法值 400；未认证 401。"""
    settings = _settings("auto-mode-api")
    app = _jarvis_client(settings, JarvisLikeAdapter())
    with TestClient(app) as c:
        body = c.get("/api/approval-mode", headers=_headers()).json()
        assert body["mode"] == "manual"
        assert body["modes"] == ["manual", "auto_safe", "yolo"]
        r = c.post("/api/approval-mode", json={"mode": "yolo"}, headers=_headers())
        assert r.status_code == 200, r.text
        assert app.state.auto_approver.current_mode() == "yolo"
        assert c.post("/api/approval-mode", json={"mode": "bogus"},
                      headers=_headers()).status_code == 400
        assert c.get("/api/approval-mode").status_code == 401
