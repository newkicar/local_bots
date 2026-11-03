"""票 05 验收：Jarvis 适配器（纪律 + 生命周期 + HITL 审批门禁 + 重启核对）。

经 HTTP API 驱动；注入假 agent（不 import 真实 Jarvis 模块），
checkpointer 走真实 AsyncSqliteSaver + aiosqlite（独立 SQLite 库）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from app.adapters.jarvis import JarvisAdapter
from app.main import create_app
from tests.test_api_skeleton import BASE, _headers, _settings, _submit, _wait_terminal

ADAPTER_SRC = Path(__file__).resolve().parent.parent / "app" / "adapters" / "jarvis.py"


def _wait_status(c: TestClient, tid: str, want: tuple[str, ...], timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    t: dict[str, Any] = {}
    while time.time() < deadline:
        t = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
        if t["status"] in want:
            return t
        time.sleep(0.05)
    raise AssertionError(f"task {tid} 未在 {timeout}s 内到达 {want}: {t.get('status')}")


# ---------- 纪律（评审必查）：只走公开入口 + 永不切全局目录 ----------
def test_discipline_public_entry_only_no_chdir():
    src = ADAPTER_SRC.read_text(encoding="utf-8")
    for ln in src.splitlines():
        if "import jarvis" in ln or "from jarvis" in ln:
            assert ln[:1] in (" ", "\t"), f"jarvis 导入必须延迟（缩进行）: {ln!r}"
    assert "src.agent" not in src, "禁止 import Jarvis 内部模块 src.agent"
    assert "from src." not in src, "禁止 import Jarvis 内部模块 from src.*"
    assert "os.chdir" not in src, "引擎永不 os.chdir"


# ---------- 假 agent / 工厂 ----------
class FakeJarvisAgent:
    """记录 ainvoke 调用签名；按开关返回正常完成 / HITL 中断 / 审批后续行状态。"""

    def __init__(self, interrupt: bool = False) -> None:
        self.interrupt = interrupt
        self.seen: list[tuple[Any, Any]] = []

    async def ainvoke(self, payload: Any, config: dict[str, Any]) -> dict[str, Any]:
        self.seen.append((payload, config))
        if self.interrupt:
            if len(self.seen) == 1:
                return {"messages": [], "__interrupt__": [SimpleNamespace(value="jarvis HITL 审批点")]}
            return {"messages": [{"role": "ai", "content": "after-approval"}]}
        if len(self.seen) == 1:
            return {
                "messages": [
                    {"role": "user", "content": "整理项目 B"},
                    {"role": "ai", "content": "done"},
                ]
            }
        user_text = payload["messages"][0]["content"]
        return {
            "messages": [
                {"role": "user", "content": user_text},
                {"role": "ai", "content": f"followup done: {user_text}"},
            ],
        }


def _make_factory(agent: FakeJarvisAgent) -> tuple:
    calls: list[tuple[str, str]] = []

    async def factory(ws: str, ckpt: Any) -> FakeJarvisAgent:
        calls.append((ws, type(ckpt).__name__))
        return agent

    return factory, calls


# ---------- 生命周期：缺省引擎 + Thread 绑定 + followup 沿用原 Thread ----------
def test_lifecycle_default_engine_thread_binding_and_followup():
    agent = FakeJarvisAgent()
    factory, calls = _make_factory(agent)
    settings = _settings("jarvis-lifecycle", ld_max_concurrent_runs=1)
    adapter = JarvisAdapter(settings.checkpoints_db_path, agent_factory=factory)
    with TestClient(create_app(settings, {"jarvis": adapter})) as c:
        t = _submit(c, instruction="整理项目 B", engine=None)  # 缺省引擎（ADR-0001）
        tid = t["task_id"]
        assert t["engine"] == "jarvis"
        assert t["workspace"], "未显式指定时自动分配任务级 workspace"
        t2 = _wait_terminal(c, tid, want=("reported",))
        assert t2["engine_thread_id"] == tid, "thread_id = task_id，建立后立即写库"
        assert t2["result"] == "done"
        # checkpointer 由本项目注入：独立 SQLite 库真实落盘
        assert os.path.isfile(settings.checkpoints_db_path)
        assert calls and calls[0][1] == "AsyncSqliteSaver"
        assert os.path.isdir(calls[0][0]) and os.path.normpath(calls[0][0]) == os.path.normpath(t2["workspace"])
        # 工厂收到任务级 workspace（显式传参，非进程目录）
        # followup：沿用原 Thread，指令进入 messages[0]
        r = c.post(f"/api/tasks/{tid}/followup", json={"instruction": "再补一条备注 X"}, headers=_headers())
        assert r.status_code == 200, r.text
        t3 = _wait_terminal(c, tid, want=("reported",))
        assert [x["attempt"] for x in t3["runs"]] == [1, 2]
        assert t3["engine_thread_id"] == tid, "追问沿用原 Thread"
        assert agent.seen[1][0] is not None, "无挂起断点时正常重发新指令"
        assert "再补一条备注 X" in agent.seen[1][0]["messages"][0]["content"]
        assert agent.seen[1][1]["configurable"]["thread_id"] == tid
        assert t3["result"].startswith("followup done:")


# ---------- HITL 中断映射审批门禁：挂起 → 审批放行 → ainvoke(None) 续行 ----------
def test_hitl_interrupt_maps_to_approval_gate():
    agent = FakeJarvisAgent(interrupt=True)
    factory, _calls = _make_factory(agent)
    settings = _settings("jarvis-hitl", ld_max_concurrent_runs=1)
    adapter = JarvisAdapter(settings.checkpoints_db_path, agent_factory=factory)

    async def _always_pending(_checkpointer: Any, _config: dict[str, Any]) -> bool:
        return True

    adapter._has_pending_interrupt = _always_pending  # 实例级接缝：模拟停在 HITL 审批点
    with TestClient(create_app(settings, {"jarvis": adapter})) as c:
        t = _submit(c, instruction="敏感操作（需审批）", engine=None)
        tid = t["task_id"]
        rid = t["runs"][0]["run_id"]
        # Run 停在 HITL 审批点 → 任务转 waiting-approval（映射票 03 门禁）
        _wait_status(c, tid, ("waiting-approval",))
        approvals = c.get("/api/approvals", headers=_headers()).json()
        row = next(a for a in approvals if a["run_id"] == rid)
        assert row["kind"] == "jarvis_interrupt"
        assert row["wait_reason"] == "jarvis_interrupt"
        assert row["path"], "挂起操作携带 workspace 路径"
        # 审批放行 → 恢复执行：resume 路径以 payload=None 调用 ainvoke（断点续行）
        r = c.post(f"/api/approvals/{rid}/approve", headers=_headers())
        assert r.status_code == 200, r.text
        t2 = _wait_terminal(c, tid, want=("reported",))
        assert agent.seen[1][0] is None, "审批恢复必须以 ainvoke(None, config) 续行"
        assert agent.seen[1][1]["configurable"]["thread_id"] == tid
        assert t2["result"] == "after-approval"
        assert t2["engine_thread_id"] == tid


# ---------- 重启核对：无检查点 / HITL 挂起 / 未完成超步 / 干净可恢复 ----------
async def test_reconcile_running_no_checkpoint_is_unsafe():
    settings = _settings("jarvis-reconcile")
    (settings.ld_data_dir / "data").mkdir(parents=True, exist_ok=True)
    adapter = JarvisAdapter(settings.checkpoints_db_path)
    task = {"task_id": "Trec", "instruction": "x", "workspace": str(settings.ld_workspace_dir / "Trec")}
    os.makedirs(task["workspace"], exist_ok=True)
    v = await adapter.reconcile_running(task, {"run_id": "R1", "attempt": 1}, "Trec")
    assert v["safe"] is False and v["result"] is None
    assert "无检查点" in v["detail"] and "不盲目重试" in v["detail"]


def test_reconcile_from_tuple_branches():
    settings = _settings("jarvis-reconcile")
    adapter = JarvisAdapter(settings.checkpoints_db_path)  # 纯函数分支，不开连接
    # 停在 HITL 审批点 → 不安全
    tup = SimpleNamespace(pending_writes=[("t1", "__interrupt__", {"id": "i1"})], checkpoint={})
    v = adapter._reconcile_from_tuple(tup)
    assert v["safe"] is False and "HITL 审批点" in v["detail"]
    # 存在未完成超步 → 不安全（可能已产生副作用）
    tup2 = SimpleNamespace(pending_writes=[("t1", "tasks", {"x": 1})], checkpoint={})
    v2 = adapter._reconcile_from_tuple(tup2)
    assert v2["safe"] is False and "未完成超步" in v2["detail"]
    # 干净 + 结果已知 → 安全且直接给出结果（完成但未汇报）
    tup3 = SimpleNamespace(
        pending_writes=[],
        checkpoint={"channel_values": {"messages": [
            {"role": "user", "content": "go"},
            {"role": "ai", "content": "final answer"},
        ]}},
    )
    v3 = adapter._reconcile_from_tuple(tup3)
    assert v3["safe"] is True and v3["result"] == "final answer"
    # 干净但无消息 → 安全（可恢复执行，结果未知）
    tup4 = SimpleNamespace(pending_writes=[], checkpoint={"channel_values": {}})
    v4 = adapter._reconcile_from_tuple(tup4)
    assert v4["safe"] is True and v4["result"] is None
    # 无检查点 → 不安全（转等待核实）
    assert adapter._reconcile_from_tuple(None)["safe"] is False