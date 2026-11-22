"""重启核对（票 04）：以持久化状态为输入，恢复或转等待核实，绝不盲目重试。

设计（方案文档·重启/恢复）：
- queued / waiting-approval 任务不动（Dispatcher pump / ApprovalGate 已负责）；
- running / interrupted 任务逐一核对引擎 Thread 的实际执行状态：
  * verdict 有已知结果（完成但未汇报）→ 直接落 completed，结果重启后仍可查可报；
  * verdict safe → dispatcher.redrive 恢复执行（resume_note 带 [核对] 来源标记）；
  * 无法确认外部副作用 → suspend_run 转 waiting-approval（wait_reason=restart_reconciliation），
    等待人工核实 approve/reject，绝不盲目重试；
- 引擎不可用 → 视同无法确认，一律转等待核实。
"""
from __future__ import annotations

import logging
from typing import Any

from app.task_manager import TaskManager, _now

logger = logging.getLogger(__name__)


async def reconcile_on_startup(
    tm: TaskManager, engines: dict[str, Any], dispatcher: Any
) -> list[dict[str, Any]]:
    """lifespan 启动时调用（tm.open() 后、dispatcher.start() 前）。返回处理报告。"""
    report: list[dict[str, Any]] = []
    for task in await tm.incomplete_tasks():
        if task["status"] in ("queued", "waiting-approval"):
            continue  # pump / gate 已负责
        active_runs = [r for r in task["runs"] if r["state"] in ("running", "interrupted")]
        if not active_runs:
            continue
        run = active_runs[-1]  # 只核对最近一个 running/interrupted Run
        engine = engines.get(task["engine"])
        thread = task.get("engine_thread_id")
        if engine is None:
            verdict = {"safe": False, "result": None, "detail": f"引擎不可用：{task['engine']}"}
        else:
            verdict = await engine.reconcile_running(task, run, thread)
        rid, tid = run["run_id"], task["task_id"]
        if verdict.get("result") is not None:
            # 已知结果（完成但未汇报）：直接落 completed，不重跑
            await tm.transition_run(rid, "completed", result=verdict["result"], finished_at=_now())
            if await tm.transition_task(tid, "completed", result=verdict["result"]):
                await tm.add_notification(tid, "task_completed", {"run_id": rid, "result": verdict["result"]})
            action = "completed"
        elif verdict.get("safe"):
            await dispatcher.redrive(
                rid, f"[核对] 重启核对确认可安全恢复（{verdict.get('detail') or 'safe'}）"
            )
            action = "resumed"
        else:
            # 无法确认外部副作用 → 转等待核实，绝不盲目重试
            ws = task.get("workspace") or ""
            pending_op = {"kind": "restart_check", "path": ws or None,
                         "detail": verdict.get("detail") or "无法确认副作用"}
            await tm.suspend_run(rid, wait_reason="restart_reconciliation",
                                 pending_op=pending_op, approved_paths=[])
            if await tm.transition_task(tid, "waiting-approval"):
                await tm.add_notification(tid, "approval_requested", {
                    "run_id": rid, "wait_reason": "restart_reconciliation",
                    "pending_op": pending_op, "approved_paths": [],
                })
            action = "waiting-verification"
        logger.info("重启核对 task=%s run=%s → %s", tid, rid, action)
        report.append({"task_id": tid, "run_id": rid, "action": action,
                      "detail": verdict.get("detail")})
    return report