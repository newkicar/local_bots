'''票 09/10：适配器能力面。

base.EngineAdapter.capabilities() 基类安全默认（全 False，FakeEngine 继承不动）；
Jarvis 如实上报能力面；显式非默认 jarvis profile → 显式 EngineError
（绝不静默回退 default）；默认路径行为不变（现有测试锁死）。
'''
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from app.adapters.base import EngineAdapter, EngineError
from app.adapters.codex import CodexAdapter
from app.adapters.jarvis import JarvisAdapter
from tests.fakes import FakeEngine

BASE = Path(__file__).resolve().parent
TMP = BASE / "tmp_workspace" / "caps09"


# execute 单测用最小 hooks（execute 路径不回调 hooks）
class _Hooks:
    async def thread_bound(self, tid: str) -> None:
        ...

    async def side_effect(self, kind: str, detail: str, path: str | None = None) -> None:
        ...

    async def request_operation(self, kind: str, path: str, detail: str = "") -> None:
        ...


def _ws() -> str:
    os.makedirs(TMP / "ws", exist_ok=True)
    return str(TMP / "ws")


def _run() -> dict[str, Any]:
    return {"run_id": "run-1", "attempt": 1, "decision": None, "pending_op": None}


class _NoCallAgent:
    # 非默认 profile 绝不得执行引擎（静默回退 default 即失败）
    async def ainvoke(self, payload: Any, config: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("非默认 profile 绝不得执行引擎（静默回退 default 即失败）")


def _adapter() -> JarvisAdapter:
    async def factory(ws: str, ckpt: Any) -> _NoCallAgent:
        return _NoCallAgent()

    return JarvisAdapter(TMP / "chk.db", agent_factory=factory)


# ---------- 基类安全默认：FakeEngine 不动，能力面全 False ----------
def test_base_capabilities_default_all_false():
    caps = FakeEngine().capabilities()
    assert caps == {"agent_discovery": False, "direct_profile_invocation": False}
    assert hasattr(EngineAdapter, "capabilities")


# ---------- jarvis 如实上报能力面 ----------
def test_jarvis_capabilities_honest():
    caps = _adapter().capabilities()
    assert caps["agent_discovery"] is False
    assert caps["direct_profile_invocation"] is False
    assert caps["non_default_profile_policy"] == "explicit_failure"


# ---------- 显式非默认 jarvis profile → 显式 EngineError（不触达引擎） ----------
def test_jarvis_non_default_profile_explicit_failure():
    a = _adapter()
    task = {"task_id": "t1", "instruction": "x", "workspace": _ws(),
            "engine_profile_id": "custom-profile"}
    try:
        asyncio.run(a.execute(task, _run(), "t1", _Hooks()))
    except EngineError as e:
        msg = str(e)
        assert "custom-profile" in msg and "不静默回退" in msg
    else:
        raise AssertionError("非默认 profile 必须显式 EngineError")


# ---------- 默认路径行为不变：无 profile / profile=default 均放行 ----------
def test_jarvis_default_profile_path_unchanged():
    class _OkAgent:
        def __init__(self) -> None:
            self.called = 0

        async def ainvoke(self, payload: Any, config: dict[str, Any]) -> dict[str, Any]:
            self.called += 1
            return {"messages": [{"role": "ai", "content": "done"}]}

    agent = _OkAgent()

    async def factory(ws: str, ckpt: Any) -> _OkAgent:
        return agent

    a = JarvisAdapter(TMP / "chk.db", agent_factory=factory)
    task = {"task_id": "t2", "instruction": "x", "workspace": _ws(),
            "engine_profile_id": "default"}
    assert asyncio.run(a.execute(task, _run(), "t2", _Hooks())) == "done"
    task2 = {"task_id": "t3", "instruction": "x", "workspace": _ws()}
    assert asyncio.run(a.execute(task2, _run(), "t3", _Hooks())) == "done"
    assert agent.called == 2


# ---------- codex 如实上报能力面（票 10） ----------
def test_codex_capabilities_honest():
    caps = CodexAdapter().capabilities()
    assert caps["agent_discovery"] is True
    assert caps["direct_profile_invocation"] == "default_only"
    assert caps["invocation_modes"]["default"] == "direct"
    assert caps["invocation_modes"]["worker"] == "delegated_only"
    assert caps["invocation_modes"]["explorer"] == "delegated_only"
    assert caps["invocation_modes"]["custom"] == "delegated_only"
    assert caps["non_default_profile_policy"] == "explicit_failure"


# ---------- delegated_only 顶层调用 → 显式 EngineError（CLI 绝不启动） ----------
def test_codex_delegated_only_top_level_explicit_failure():
    spawned: list = []

    async def spawn(cmd: list, cwd: str):
        spawned.append(cmd)
        raise AssertionError("delegated_only profile 绝不得启动 CLI")

    a = CodexAdapter(cli="codex", spawn=spawn)
    task = {"task_id": "t9", "instruction": "x", "workspace": _ws(),
            "engine_profile_id": "worker"}
    try:
        asyncio.run(a.execute(task, _run(), None, _Hooks()))
    except EngineError as e:
        msg = str(e)
        assert "worker" in msg and "显式拒绝" in msg
    else:
        raise AssertionError("delegated_only 顶层调用必须显式 EngineError")
    assert not spawned, "CLI 子进程不得启动（不发明无效旗标）"
