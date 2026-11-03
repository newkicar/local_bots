"""测试替身引擎（票 01 纪律：不 import 任何真实引擎模块——只依赖 adapters.base）。"""
from __future__ import annotations

import asyncio
from typing import Any

from app.adapters.base import EngineAdapter, EngineError, RunSuspended


class FakeEngine(EngineAdapter):
    """假引擎：确定性延迟 + 可注入行为（失败/挂起/核对），驱动状态机验收。"""

    name = "fake"

    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail: bool = False,
        suspend: bool = False,
        reconcile_safe: bool = False,
        reconcile_result: str | None = None,
    ) -> None:
        self._delay = delay
        self._fail = fail
        self._suspend = suspend
        self._reconcile_safe = reconcile_safe
        self._reconcile_result = reconcile_result
        self.executed: list[tuple[str, str]] = []
        self.in_flight = 0
        self.high_water = 0
        self._lock = asyncio.Lock()

    async def ensure_thread(self, task: dict[str, Any], run: dict[str, Any], hooks) -> str | None:
        thread = task.get("engine_thread_id") or f"thread-{task['task_id']}"
        await hooks.thread_bound(thread)
        return thread

    async def execute(self, task, run, thread, hooks, *, resume_note=None) -> str:
        if self._suspend:
            raise RunSuspended(
                {"kind": "write", "path": r"C:\secret", "detail": "fake-blacklist"},
                "fake-blacklist",
            )
        if self._fail:
            raise EngineError("fake engine boom")
        async with self._lock:
            self.in_flight += 1
            self.high_water = max(self.high_water, self.in_flight)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            self.executed.append((task["task_id"], run["run_id"]))
        finally:
            async with self._lock:
                self.in_flight -= 1
        return f"fake result [{task['task_id']} attempt={run['attempt']} note={resume_note} thread={thread}"

    async def reconcile_running(self, task, run, thread) -> dict[str, Any]:
        if self._reconcile_result is not None:
            return {"safe": self._reconcile_safe, "result": self._reconcile_result,
                    "detail": "fake-reconcile-known"}
        return {"safe": self._reconcile_safe, "result": None, "detail": "fake-reconcile"}
