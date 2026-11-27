"""启动分步限时护栏（防僵尸）：挂起的启动步骤不得拖死整个 lifespan。

背景（根因链）：uvicorn 先绑端口后跑 lifespan，lifespan 任一环节挂起 →
端口 LISTENING 但所有请求永久挂起；开机自启的 netstat-only 守卫看到
LISTENING 会跳过重启 → 僵尸状态永久持续到人工干预。

两类行为：
- agent sync provider 挂起 → 该步限时跳过（记日志继续启动），health 可达；
- 重启核对挂起 → 限时超期直接崩（崩溃→launcher 重启；DB 状态是持久化
  输入可重入），绝不变成"端口听着但全挂"的僵尸。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.agents.discovery import DiscoveryResult
from app.main import create_app
from tests.fakes import FakeEngine
from tests.test_api_skeleton import _headers, _settings, _submit
from tests.test_reconcile import _restart_settings, _seed_running, _wait_running

_MAX_ENTRY_S = 3.0  # 修后 lifespan 进入耗时上限（两处限时均被 monkeypatch 到 0.5s）


class _HangProvider:
    """永远挂起的发现 provider：驱动 agent sync 步的限时（修前红灯源）。"""

    engine = "hang-engine"

    async def discover(self, scope: str) -> DiscoveryResult:
        await asyncio.sleep(30)
        return DiscoveryResult(engine=self.engine, status="ok")


def test_agent_sync_hang_bounded_not_blocking(monkeypatch):
    """provider 挂起 → agent sync 限时内跳过（不阻断启动），lifespan 进入且 health 可达。"""
    monkeypatch.setattr(app_main, "AGENT_SYNC_TIMEOUT_S", 0.5)
    settings = _settings("lifespan-sync-hang")
    t0 = time.perf_counter()
    with TestClient(
        create_app(settings, {"fake": FakeEngine()}, agent_sync_providers=[_HangProvider()])
    ) as c:
        entry_s = time.perf_counter() - t0
        assert c.get("/api/health", headers=_headers()).status_code == 200
    # 修前：lifespan 等 provider 睡满 30s；修后：限时 0.5s 跳过 → 进入 <3s
    assert entry_s < _MAX_ENTRY_S


class _HangReconcileEngine(FakeEngine):
    """reconcile 永远挂起：模拟引擎侧核对卡死（驱动 reconcile 步的限时）。"""

    async def reconcile_running(self, task, run, thread) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {"safe": False, "result": None, "detail": "hang"}


def test_reconcile_hang_times_out_crash(monkeypatch):
    """reconcile 挂起 → 启动限时超期崩溃（不僵尸化）。"""
    settings = _settings("lifespan-reconcile-hang")
    with TestClient(create_app(settings, {"hang": FakeEngine(delay=30)})) as c:
        t = _submit(c, "hang reconcile", "hang")
        _wait_running(c, t["task_id"])
    # 阶段 A 关闭 = 杀掉服务；再模拟 kill -9 残留（running 行停在库里）
    _seed_running(settings, t["task_id"], t["runs"][0]["run_id"])

    monkeypatch.setattr(app_main, "RECONCILE_TIMEOUT_S", 0.5)
    t0 = time.perf_counter()
    with pytest.raises(TimeoutError), TestClient(
        create_app(_restart_settings(settings), {"hang": _HangReconcileEngine()})
    ):
        pass
    # 修前：lifespan 卡满 30s（或更久）才继续；修后：限时 0.5s 崩 → <3s
    assert time.perf_counter() - t0 < _MAX_ENTRY_S