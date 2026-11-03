"""Dispatcher：后台任务调度器（ADR-0003 可配并发上限 + 同 Thread 串行化）。

纪律：
- 只依赖 app.adapters.base 抽象；真实引擎（codex/jarvis）永不直接出现在这里；
- Run 执行期间尊重 CancelledError（取消/关停即终止底层进程）；
- 状态迁移全部走 TaskManager 的状态机守卫（原子），本类只做编排。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Awaitable, Callable

from app.adapters.base import EngineAdapter, EngineError, RunHooks, RunSuspended
from app.task_manager import (
    TASK_TERMINAL_STATES,
    InvalidTransition,
    TaskManager,
    _now,
)

logger = logging.getLogger(__name__)

# 审批策略钩子（票 03 接 Blacklist）：返回 RunSuspended 即挂起该 Run。
PolicyFn = Callable[[dict[str, Any], dict[str, Any]], Awaitable[RunSuspended | None]]


class Dispatcher:
    def __init__(
        self,
        tm: TaskManager,
        engines: dict[str, EngineAdapter],
        *,
        max_concurrent: int = 10,
        policy: PolicyFn | None = None,
    ) -> None:
        self._tm = tm
        self._engines = dict(engines)
        self._max = max(1, int(max_concurrent))
        self._policy = policy
        self._execs: dict[str, asyncio.Task] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._cancel_flags: set[str] = set()
        self._shutdown = False
        self._loop_task: asyncio.Task | None = None

    # ---------- 生命周期 ----------
    @property
    def active_count(self) -> int:
        return len(self._execs)

    @property
    def shutdown(self) -> bool:
        return self._shutdown

    def start(self) -> None:
        """必须跑在运行中的事件循环内（FastAPI lifespan 调起）。幂等。"""
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        """关停：取消 pump 与所有在飞 Run（CancelledError → interrupted/cancelled 落库）。"""
        self._shutdown = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        for t in list(self._execs.values()):
            t.cancel()
        with suppress(Exception):
            await asyncio.gather(*self._execs.values(), return_exceptions=True)
        self._execs.clear()

    async def _loop(self) -> None:
        while not self._shutdown:
            try:
                await self._pump()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— pump 崩溃不得杀死调度
                logger.exception("dispatcher pump error")
            await asyncio.sleep(0.2)

    async def _pump(self) -> None:
        exclude = await self._tm.active_task_ids()
        while len(self._execs) < self._max:
            run = await self._tm.next_queued_run(exclude)
            if run is None:
                break
            task = await self._tm.get_task(run["task_id"])
            if task is None:
                await self._tm.transition_run(run["run_id"], "cancelled", error="task missing")
                continue
            if task["status"] in TASK_TERMINAL_STATES:
                await self._tm.transition_run(
                    run["run_id"], "cancelled", error="task already terminal"
                )
                continue
            exclude.add(task["task_id"])  # 同 Thread 串行：本批不再取该任务的下一个 queued Run
            self._execs[run["run_id"]] = asyncio.get_running_loop().create_task(
                self._drive(run["run_id"])
            )

    def _lock_for(self, task_id: str) -> asyncio.Lock:
        if task_id not in self._task_locks:
            self._task_locks[task_id] = asyncio.Lock()
        return self._task_locks[task_id]

    # ---------- 单次驱动（首次执行 / 恢复执行） ----------
    async def _drive(self, run_id: str, resume_note: str | None = None) -> None:
        run: dict[str, Any] | None = None
        task: dict[str, Any] | None = None
        try:
            run = await self._tm.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            task = await self._tm.get_task(run["task_id"])
            if task is None:
                await self._tm.transition_run(run_id, "cancelled", error="task missing")
                return
            if task["status"] in TASK_TERMINAL_STATES:
                await self._tm.transition_run(run_id, "cancelled", error="task already terminal")
                return
            if self._policy is not None:
                verdict = await self._policy(task, run)
                if verdict is not None:
                    await self._suspend(run, task, verdict)
                    return
            async with self._lock_for(task["task_id"]):
                await self._execute_in_lock(run, task, resume_note)
        except asyncio.CancelledError:
            if run is not None and task is not None:
                await self._on_cancel(run, task)
        finally:
            self._execs.pop(run_id, None)

    async def _execute_in_lock(
        self, run: dict[str, Any], task: dict[str, Any], resume_note: str | None
    ) -> None:
        try:
            await self._do_execute(run, task, resume_note)
        except asyncio.CancelledError:
            await self._on_cancel(run, task)

    async def _do_execute(
        self, run: dict[str, Any], task: dict[str, Any], resume_note: str | None
    ) -> None:
        engine = self._engines.get(task["engine"])
        if engine is None:
            await self._fail(run, task, f"engine unavailable: {task['engine']}")
            return
        thread = task.get("engine_thread_id")
        hooks = RunHooks(task, run, self)
        try:
            if thread is None:
                bound = await engine.ensure_thread(task, run, hooks)
                if bound:
                    await self._tm.set_task_thread(task["task_id"], bound)
                    thread = bound
        except EngineError as exc:
            await self._fail(run, task, f"ensure_thread failed: {exc}")
            return
        except asyncio.CancelledError:
            await self._on_cancel(run, task)
            return
        except Exception as exc:  # noqa: BLE001
            await self._fail(run, task, f"engine crash: {exc}")
            return
        # resume 路径（resume_run）已把 run/task 预置为 running：状态机无自环可跳，须守卫
        if task["status"] != "running":
            if not await self._tm.transition_task(task["task_id"], "running"):
                await self._tm.transition_run(run["run_id"], "cancelled", error="task no longer active")
                return
        if run["state"] != "running":
            await self._tm.transition_run(run["run_id"], "running", started_at=_now())
        try:
            result = await engine.execute(task, run, thread, hooks, resume_note=resume_note)
        except RunSuspended as s:
            await self._suspend(run, task, s)
            return
        except EngineError as exc:
            await self._fail(run, task, str(exc))
            return
        except asyncio.CancelledError:
            await self._on_cancel(run, task)
            return
        except Exception as exc:  # noqa: BLE001
            await self._fail(run, task, f"engine crash: {exc}")
            return
        await self._finish(run, task, result)

    # ---------- 终局处理 ----------
    async def _finish(
        self, run: dict[str, Any], task: dict[str, Any], result: str
    ) -> None:
        await self._tm.transition_run(
            run["run_id"], "completed", result=result, finished_at=_now()
        )
        if await self._tm.transition_task(task["task_id"], "completed", result=result):
            await self._tm.add_notification(
                task["task_id"],
                "task_completed",
                {"run_id": run["run_id"], "result": result},
            )

    async def _fail(
        self, run: dict[str, Any], task: dict[str, Any], error: str
    ) -> None:
        logger.warning("run %s failed: %s", run["run_id"], error)
        if await self._tm.transition_run(run["run_id"], "failed", error=error, finished_at=_now()):
            if await self._tm.transition_task(task["task_id"], "failed", error=error):
                await self._tm.add_notification(
                    task["task_id"], "task_failed", {"run_id": run["run_id"], "error": error}
                )

    async def _suspend(
        self, run: dict[str, Any], task: dict[str, Any], s: RunSuspended
    ) -> None:
        await self._tm.suspend_run(
            run["run_id"],
            wait_reason=s.wait_reason,
            pending_op=s.pending_op,
            approved_paths=s.approved_paths,
        )
        await self._tm.transition_task(task["task_id"], "waiting-approval")
        await self._tm.add_notification(
            task["task_id"],
            "approval_requested",
            {
                "run_id": run["run_id"],
                "wait_reason": s.wait_reason,
                "pending_op": s.pending_op,
                "approved_paths": s.approved_paths,
            },
        )

    async def _on_cancel(self, run: dict[str, Any], task: dict[str, Any]) -> None:
        user_cancel = run["run_id"] in self._cancel_flags
        if user_cancel:
            self._cancel_flags.discard(run["run_id"])
            target, detail = "cancelled", "cancelled by user"
        else:
            target, detail = "interrupted", "service stopped"
        await self._tm.transition_run(run["run_id"], target, error=detail, finished_at=_now())
        await self._tm.transition_task(task["task_id"], target)

    # ---------- 用户 API（被 main.py 路由调用） ----------
    async def cancel_task(self, task_id: str) -> list[dict[str, Any]]:
        """取消整个任务（ADR-0006 只取消，不留半状态）；返回各 Run 结局台账。"""
        task = await self._tm.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] in TASK_TERMINAL_STATES:
            raise InvalidTransition(f"任务 {task_id} 已终态 {task['status']}，不可取消")
        for run in task["runs"]:
            rid = run["run_id"]
            if run["state"] == "running":
                self._cancel_flags.add(rid)
                drive = self._execs.get(rid)
                if drive is not None:
                    drive.cancel()
                    with suppress(Exception, asyncio.CancelledError):
                        await drive
            elif run["state"] in ("queued", "waiting-approval"):
                await self._tm.transition_run(
                    rid, "cancelled", error="cancelled by user", finished_at=_now()
                )
        with suppress(InvalidTransition):
            await self._tm.transition_task(task_id, "cancelled")
        task = await self._tm.get_task(task_id)
        assert task is not None
        ledger = [
            {"run_id": r["run_id"], "attempt": r["attempt"], "state": r["state"],
             "side_effects": r["side_effects"]}
            for r in task["runs"]
        ]
        await self._tm.add_notification(task_id, "task_cancelled", {"ledger": ledger})
        return ledger

    async def resume_run(self, run_id: str, resume_note: str | None = None) -> None:
        """审批恢复（票 03 决策 API 调用）：waiting-approval 的 Run 继续执行。"""
        run = await self._tm.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run["state"] != "waiting-approval":
            raise InvalidTransition(f"Run {run_id} 状态 {run['state']}，不可恢复")
        while not self._shutdown and len(self._execs) >= self._max:
            await asyncio.sleep(0.2)
        if self._shutdown:
            raise RuntimeError("dispatcher 已关停")
        await self._tm.transition_run(run_id, "running", started_at=_now())
        await self._tm.transition_task(run["task_id"], "running")
        self._execs[run_id] = asyncio.get_running_loop().create_task(
            self._drive(run_id, resume_note)
        )

    # ---------- RunHooks 实现（适配器回调） ----------
    async def thread_bound(self, task_id: str, thread_id: str) -> None:
        task = await self._tm.get_task(task_id)
        if task is None or task.get("engine_thread_id") == thread_id:
            return
        await self._tm.set_task_thread(task_id, thread_id)

    async def record_side_effect(
        self, run_id: str, kind: str, detail: str, path: str | None = None
    ) -> None:
        effect: dict[str, Any] = {"kind": kind, "detail": detail}
        if path is not None:
            effect["path"] = path
        await self._tm.append_side_effect(run_id, effect)

    async def request_operation(self, hooks: RunHooks, kind: str, path: str, detail: str = "") -> None:
        """敏感操作预审批：票 03 在此接 Blacklist 判定；命中则抛 RunSuspended。"""
