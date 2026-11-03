"""引擎适配器接口（EngineAdapter 抽象 + 执行钩子 + 异常类型）。

纪律（AGENTS.md）：
- Dispatcher 只依赖本文件的抽象；具体引擎在 codex.py / jarvis.py；
- jarvis.py 只允许 import Jarvis 公开工厂，codex.py 只起 CLI 子进程；
- 引擎永不 os.chdir；工作目录一律显式参数（task["workspace"]）。
"""
from __future__ import annotations

import abc
from typing import Any


class EngineError(Exception):
    """引擎自身故障（CLI 缺席、非零退出、内部错误等）；Run 落 failed，不影响其他 Run。"""


class RunSuspended(Exception):
    """Run 挂起等待审批（Blacklist 命中 / 重启核对无法确认副作用）。

    - pending_op：被挂起的具体操作（kind/path/detail），写 runs.pending_op；
    - approved_paths：已批准的路径（批准恢复后放行，不再二次挂起）；
    - wait_reason：挂起来源（blacklist / restart_reconciliation / ...）。
    """

    def __init__(self, pending_op: dict[str, Any] | None, wait_reason: str,
                 approved_paths: list[str] | None = None) -> None:
        self.pending_op = pending_op
        self.wait_reason = wait_reason
        self.approved_paths = list(approved_paths or [])
        super().__init__(f"run suspended: {wait_reason} {pending_op}")


class RunHooks:
    """Dispatcher 注入适配器的单次 Run 回调（副作用台账 / 审批钩子 / Thread 绑定）。"""

    def __init__(self, task: dict[str, Any], run: dict[str, Any], dispatcher: Any) -> None:
        self.task = task
        self.run = run
        self._dispatcher = dispatcher

    async def thread_bound(self, thread_id: str) -> None:
        """引擎建立 Thread 后立即上报（Dispatcher 立刻写库，不等任务结束——票 01 验收项）。"""
        await self._dispatcher.thread_bound(self.task["task_id"], thread_id)

    async def side_effect(self, kind: str, detail: str, path: str | None = None) -> None:
        """副作用台账（追加式，从不静默回滚）：写了什么文件、跑了什么命令/应用。"""
        await self._dispatcher.record_side_effect(self.run["run_id"], kind, detail, path)

    async def request_operation(self, kind: str, path: str, detail: str = "") -> None:
        """敏感操作预审批（票 03 落地 Blacklist 判定）；未授权时抛 RunSuspended 停当前步。"""
        await self._dispatcher.request_operation(self, kind, path, detail)


class EngineAdapter(abc.ABC):
    """引擎唯一接口。task/run 为 TaskManager 返回的 dict（snake_case）。"""

    name: str = "abstract"

    @abc.abstractmethod
    async def ensure_thread(self, task: dict[str, Any], run: dict[str, Any],
                            hooks: RunHooks) -> str | None:
        """返回确定的 thread id；若 Thread 由引擎在执行时才产生（如 Codex 生成
        session id），返回 None，执行中经 hooks.thread_bound(...) 上报。"""

    @abc.abstractmethod
    async def execute(self, task: dict[str, Any], run: dict[str, Any], thread: str | None,
                      hooks: RunHooks, *, resume_note: str | None = None) -> str:
        """执行本次 Run（首次执行或带 resume_note 的恢复执行），返回结果文本。
        必须尊重 asyncio.CancelledError（取消/关停时终止底层进程）。"""

    async def reconcile_running(self, task: dict[str, Any], run: dict[str, Any],
                                thread: str | None) -> dict[str, Any]:
        """重启核对（票 04）：判断被中断的 Run 可否安全恢复。
        返回 {"safe": bool, "result": str|None（已知最终结果）, "detail": str}。
        默认无法确认 → 不安全（转 waiting-approval 请用户核实，绝不盲目重试）。"""
        return {"safe": False, "result": None, "detail": "adapter 未实现 reconcile"}
