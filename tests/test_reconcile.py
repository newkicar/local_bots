"""票 04：重启核对——关机不丢任务、不盲目重试。

两阶段行为测试：阶段 A（应用 A 杀掉：TestClient 关闭 → dispatcher.stop() 打断在飞 Run），
阶段 B（应用 B 重启：同一数据目录）；输入 = 持久化状态（可手动改写持久化行模拟 kill -9），
判定全部经 HTTP API 外部行为，不测恢复逻辑实现细节。
"""
from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from app.task_manager import TaskManager, _now
from tests.fakes import FakeEngine
from tests.test_api_skeleton import _headers, _settings, _submit, _wait_terminal


def _restart_settings(settings: Settings) -> Settings:
    """阶段 B 的应用：同一数据目录（持久化状态为输入），独立的 Settings 实例。"""
    return Settings(
        ld_data_dir=settings.ld_data_dir,
        ld_workspace_dir=settings.ld_workspace_dir,
        ld_api_token="test-token",
        ld_max_concurrent_runs=2,
        _env_file=None,
    )


def _seed_running(settings: Settings, tid: str, rid: str) -> None:
    """模拟 kill -9：持久化行停留在 running（没有优雅 stop 的 interrupted 迁移）。"""
    async def _run() -> None:
        tm = TaskManager(settings.tasks_db_path)
        await tm.open()
        await tm.transition_run(rid, "running", started_at=_now())
        await tm.transition_task(tid, "running")
        await tm.close()

    asyncio.run(_run())


def _wait_running(c, *tids: str) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        statuses = {tid: c.get(f"/api/tasks/{tid}", headers=_headers()).json()["status"] for tid in tids}
        if all(s == "running" for s in statuses.values()):
            return
        time.sleep(0.1)
    raise AssertionError(f"未在 20s 内全部 running: {statuses}")


def test_restart_reconcile_safe_redrive_and_unreported_survive():
    """阶段 B：safe 核对 → 直接恢复执行；completed 未汇报可查可报；已知结果不重跑。"""
    settings = _settings("reconcile-safe", ld_max_concurrent_runs=2)
    engines_a = {
        "quick": FakeEngine(delay=0.01),
        "slow": FakeEngine(delay=30),
        "known": FakeEngine(delay=30),
    }
    with TestClient(create_app(settings, engines_a)) as c:
        t4 = _submit(c, "t4 quick", "quick")
        t2 = _submit(c, "t2 slow", "slow")
        t3 = _submit(c, "t3 known", "known")
        _wait_running(c, t2["task_id"], t3["task_id"])
        # 此刻 t4 已 completed（未汇报——从未 GET 过它）；t2/t3 在飞（30s 延迟，不会自然结束）
    # 关闭 = 杀掉服务：t2/t3 → interrupted（优雅停机）

    engines_b = {
        "quick": FakeEngine(),
        "slow": FakeEngine(delay=0.01, reconcile_safe=True),
        "known": FakeEngine(delay=0.01, reconcile_result="recovered known result"),
    }
    with TestClient(create_app(_restart_settings(settings), engines_b)) as c:
        # ① safe 核对 → redrive 恢复，resume_note 带 [核对] 来源标记
        t2r = _wait_terminal(c, t2["task_id"])
        assert t2r["status"] == "reported"
        assert "核对" in (t2r["result"] or ""), t2r["result"]
        assert t2r["runs"][0]["state"] == "completed"
        # ② 已知结果（完成但未汇报）→ 直接落 completed，不重跑，重启后仍可查可报
        r3 = c.get(f"/api/runs/{t3['runs'][0]['run_id']}", headers=_headers()).json()
        assert r3["state"] == "completed" and r3["result"] == "recovered known result"
        t3r = c.get(f"/api/tasks/{t3['task_id']}", headers=_headers()).json()
        assert t3r["status"] == "reported" and t3r["result"] == "recovered known result"
        assert engines_b["known"].executed == []  # 不盲目重试
        # ③ 已完成未汇报 → 重启后仍可查、可汇报
        t4r = c.get(f"/api/tasks/{t4['task_id']}", headers=_headers()).json()
        assert t4r["status"] == "reported"
        assert (t4r["result"] or "").startswith("fake result"), t4r["result"]


def test_restart_reconcile_unsafe_waits_verification_then_rejects():
    """无法确认副作用（kill -9 残留 running）→ 转 waiting-approval（来源标记），人工拒绝后终态。"""
    settings = _settings("reconcile-unsafe", ld_max_concurrent_runs=1)
    with TestClient(create_app(settings, {"unsafe": FakeEngine(delay=30)})) as c:
        t1 = _submit(c, "t1 unsafe", "unsafe")
        _wait_running(c, t1["task_id"])
    _seed_running(settings, t1["task_id"], t1["runs"][0]["run_id"])  # 模拟 kill -9

    eng = FakeEngine(delay=0.01, reconcile_safe=False)
    with TestClient(create_app(_restart_settings(settings), {"unsafe": eng})) as c:
        t1r = c.get(f"/api/tasks/{t1['task_id']}", headers=_headers()).json()
        assert t1r["status"] == "waiting-approval"
        run1 = t1r["runs"][0]
        assert run1["state"] == "waiting-approval"
        assert run1["wait_reason"] == "restart_reconciliation"  # 重启核对来源标记
        assert (run1["pending_op"] or {})["pending_op"]["kind"] == "restart_check"
        assert eng.executed == []  # 决策前绝不重跑
        pending = c.get("/api/approvals", headers=_headers()).json()
        assert len(pending) == 1 and pending[0]["run_id"] == run1["run_id"]
        assert pending[0]["wait_reason"] == "restart_reconciliation"
        notifs = c.get("/api/notifications", params={"task_id": t1["task_id"]},
                       headers=_headers()).json()
        assert any(n["kind"] == "approval_requested"
                   and n["payload"]["wait_reason"] == "restart_reconciliation"
                   for n in notifs)
        r = c.post(f"/api/approvals/{run1['run_id']}/reject",
                   json={"reason": "人工核实后拒绝"}, headers=_headers())
        assert r.status_code == 200, r.text
        assert c.get("/api/approvals", headers=_headers()).json() == []
        assert eng.executed == []
        t1f = c.get(f"/api/tasks/{t1['task_id']}", headers=_headers()).json()
        assert t1f["status"] == "rejected" and t1f["reject_reason"] == "人工核实后拒绝"


def test_restart_reconcile_unsafe_approve_resumes_once():
    """无法确认副作用（优雅停机 interrupted 形态）→ 等待核实；批准后恰好执行一次。"""
    settings = _settings("reconcile-approve", ld_max_concurrent_runs=1)
    with TestClient(create_app(settings, {"unsafe": FakeEngine(delay=30)})) as c:
        t1 = _submit(c, "t1 unsafe", "unsafe")
        _wait_running(c, t1["task_id"])
    # 优雅停机：t1 → interrupted（不 seed，覆盖 interrupted 核对形态）

    eng = FakeEngine(delay=0.01, reconcile_safe=False)
    with TestClient(create_app(_restart_settings(settings), {"unsafe": eng})) as c:
        t1w = c.get(f"/api/tasks/{t1['task_id']}", headers=_headers()).json()
        assert t1w["status"] == "waiting-approval"
        rid = t1w["runs"][0]["run_id"]
        r = c.post(f"/api/approvals/{rid}/approve", headers=_headers())
        assert r.status_code == 200, r.text
        t1f = _wait_terminal(c, t1["task_id"])
        assert t1f["status"] == "reported"
        assert len(eng.executed) == 1  # 人工决策后恰好执行一次
        assert "restart_reconciliation" in (t1f["result"] or ""), t1f["result"]
        assert c.get("/api/approvals", headers=_headers()).json() == []