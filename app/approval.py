"""审批门禁（票 03）：waiting-approval Run 的人工决策 + 超时自动拒绝。

纪律（安全基线）：
- 批准/拒绝/取消/超时全程输出 Side-Effect Ledger（run.side_effects 追加式、从不回滚）；
- 超时（timeout_min，LD_APPROVAL_TIMEOUT_MIN）未处理自动拒绝：
  Run/Task → rejected，计时自进入 waiting-approval 起算（wait_requested_at）；
- 并发安全：决策前先查 waiting-approval 态，写库走 TaskManager 状态机守卫（原子），
  输给并发决策（已批准/已取消）时按 InvalidTransition 上抛，路由映射 409。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from app.task_manager import InvalidTransition, TaskManager, _now

if TYPE_CHECKING:
    from app.dispatcher import Dispatcher
    from app.permissions import Blacklist

logger = logging.getLogger(__name__)


class ApprovalGate:
    """每 sweep_interval 扫一次 waiting-approval Run；超时未处理自动拒绝。

    approve/reject/list_pending 供 main.py 路由调用；sweep_interval 为公开属性
    （测试接缝：客户端内可改小以加速超时路径，sweeper 每轮重读）。
    """

    def __init__(
        self,
        tm: TaskManager,
        dispatcher: "Dispatcher",
        blacklist: "Blacklist",
        *,
        timeout_min: float = 30.0,
        sweep_interval: float = 15.0,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._tm = tm
        self._dispatcher = dispatcher
        self._blacklist = blacklist
        self._timeout_min = float(timeout_min)
        self.sweep_interval = float(sweep_interval)
        # 可注入时钟（ISO-8601 UTC 字符串，与 task_manager._now 一致）
        self._clock = clock or _now
        self._sweeper: asyncio.Task | None = None

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """必须在运行中的事件循环内调用（FastAPI lifespan）。幂等。"""
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.get_running_loop().create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(max(0.05, self.sweep_interval))
            try:
                await self.sweep_once()
            except Exception:  # noqa: BLE001 —— 清扫崩溃不得杀死门禁
                logger.exception("approval sweep error")

    # ---------- 决策 API（main.py 路由调用） ----------
    async def list_pending(self) -> list[dict[str, Any]]:
        """所有 waiting-approval Run 的待审视图（含 Side-Effect Ledger 与超时时刻）。"""
        out: list[dict[str, Any]] = []
        for run in await self._tm.waiting_runs():
            task = await self._tm.get_task(run["task_id"])
            wrapper = run["pending_op"] or {}
            po = wrapper.get("pending_op") or {}
            timeout_at: str | None = None
            if run["wait_requested_at"]:
                timeout_at = (
                    datetime.fromisoformat(run["wait_requested_at"])
                    + timedelta(minutes=self._timeout_min)
                ).isoformat()
            out.append({
                "run_id": run["run_id"],
                "task_id": run["task_id"],
                "attempt": run["attempt"],
                "kind": po.get("kind"),
                "path": po.get("path"),
                "detail": po.get("detail"),
                "wait_reason": run["wait_reason"],
                "wait_requested_at": run["wait_requested_at"],
                "timeout_at": timeout_at,
                "instruction": task.get("instruction") if task else None,
                "workspace": task.get("workspace") if task else None,
                "approved_paths": wrapper.get("approved_paths") or [],
                "pending_op": run["pending_op"],
                "side_effects": run["side_effects"],
            })
        return out

    async def approve(self, run_id: str) -> dict[str, Any]:
        """放行：批准路径落库 + 决策写库（resume 前，_drive 重读新鲜行），再恢复执行。"""
        run = await self._tm.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["state"] != "waiting-approval":
            raise InvalidTransition(f"Run {run_id} 状态 {run['state']}，不可批准")
        po = (run["pending_op"] or {}).get("pending_op") or {}
        op_path = po.get("path")
        await self._tm.resume_wait_for_approved_path(
            run_id, [op_path] if op_path else []
        )
        if not await self._tm.decide_run(run_id, "approved"):
            raise InvalidTransition(f"Run {run_id} 已不处于 waiting-approval（并发决策胜出）")
        await self._dispatcher.resume_run(
            run_id, f"[审批] 已放行 {op_path}（来源: {run.get('wait_reason')}）"
        )
        return {
            "run_id": run_id,
            "status": "resumed",
            "approved_paths": [op_path] if op_path else [],
            "side_effects": run["side_effects"],
        }

    async def reject(self, run_id: str, reason: str) -> dict[str, Any]:
        """拒绝：当前 Run/Task 即停（rejected），Ledger 随通知输出，副作用不回滚。"""
        run = await self._tm.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["state"] != "waiting-approval":
            raise InvalidTransition(f"Run {run_id} 状态 {run['state']}，不可拒绝")
        payload = {
            "run_id": run_id,
            "reject_reason": reason,
            "wait_reason": run.get("wait_reason"),
            "side_effects": run["side_effects"],
        }
        if not await self._tm.transition_run(
            run_id, "rejected", error=f"已拒绝：{reason}", finished_at=self._clock()
        ):
            raise InvalidTransition(f"Run {run_id} 已不处于 waiting-approval（并发决策胜出）")
        task_id = run["task_id"]
        if await self._tm.transition_task(task_id, "rejected", reject_reason=reason):
            await self._tm.add_notification(task_id, "task_rejected", payload)
        return {"run_id": run_id, "status": "rejected", **payload}

    # ---------- 超时清扫 ----------
    async def sweep_once(self) -> list[str]:
        """拒绝所有已超过 timeout_min 的 waiting-approval Run；返回已拒绝的 run_id 列表。"""
        now = datetime.fromisoformat(self._clock())
        rejected: list[str] = []
        for run in await self._tm.waiting_runs():
            ts = run.get("wait_requested_at")
            if ts is None:
                continue
            if now < datetime.fromisoformat(ts) + timedelta(minutes=self._timeout_min):
                continue
            try:
                await self.reject(run["run_id"], "approval_timeout")
            except (KeyError, InvalidTransition):
                continue  # 并发决策（批准/取消）已胜出
            rejected.append(run["run_id"])
        return rejected