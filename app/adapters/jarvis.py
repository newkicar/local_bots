"""Jarvis 引擎适配器（票 05）：唯一接口 = Jarvis 公开工厂 + .ainvoke()。

纪律（AGENTS.md / 票 05 评审必查）：
- 只调用 Jarvis 公开工厂（延迟到调用时导入），零导入内部模块；
  本模块加载不依赖 Jarvis 安装状态，测试可经 agent_factory 注入替身；
- checkpointer 由本项目注入：独立 SQLite 库（data/checkpoints.db，
  AsyncSqliteSaver + aiosqlite），无模块级单例；thread_id = task_id
  （Task/Thread/Run 三层分离，ensure_thread 建立后立即写库）；
- 引擎永不改变进程全局目录；workspace 显式传给工厂（任务级
  workspace），Jarvis 文件后端 / shell 工具据此继承。
- HITL：ainvoke 返回 __interrupt__ → 映射 RunSuspended（jarvis_interrupt），
  审批放行后以 ainvoke(Command(resume={"decisions": [...]}), config) 从断点续行
  （HITL 挂起中断只能凭 resume 决策放行；普通 None 输入仅原样重新挂起，
  会导致批准后反复触发 approval_requested）。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.adapters.base import EngineAdapter, EngineError, RunHooks, RunSuspended

AgentFactory = Callable[[str, AsyncSqliteSaver], Awaitable[Any]]


class JarvisAdapter(EngineAdapter):
    """Jarvis 引擎：create_jarvis(workspace, checkpointer) + ainvoke。

    - thread_id = task_id（ensure_thread 固定，Dispatcher 立刻写库）；
    - 每个 Run 独立开闭一条 aiosqlite 连接（短生命期，无常驻单例）；
    - 工作目录不存在即 EngineError（不静默建目录、不切进程目录）。
    """

    name = "jarvis"

    def __init__(
        self,
        checkpoints_db: str | Path,
        *,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self._checkpoints_db = Path(checkpoints_db)
        self._agent_factory: AgentFactory = agent_factory or self._default_agent_factory

    # ---------- 默认工厂（延迟导入，只走公开入口） ----------
    async def _default_agent_factory(self, workspace: str, checkpointer: AsyncSqliteSaver):
        from jarvis import create_jarvis  # 延迟导入：公开入口，零导入内部模块

        return create_jarvis(workspace, checkpointer)

    # ---------- EngineAdapter 实现 ----------
    async def ensure_thread(
        self, task: dict[str, Any], run: dict[str, Any], hooks: RunHooks
    ) -> str | None:
        thread = task.get("engine_thread_id") or task["task_id"]
        await hooks.thread_bound(thread)
        return thread

    async def execute(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        thread: str | None,
        hooks: RunHooks,
        *,
        resume_note: str | None = None,
    ) -> str:
        ws = task.get("workspace")
        if not ws or not os.path.isdir(ws):
            raise EngineError(f"jarvis workspace 不可用: {ws}")
        text = task.get("instruction") or ""
        if resume_note:
            text += f"\n[恢复备注] {resume_note}"

        conn = await aiosqlite.connect(str(self._checkpoints_db), timeout=30.0)
        try:
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            agent = await self._agent_factory(ws, saver)
            config = {"configurable": {"thread_id": thread or task["task_id"]}}
            if await self._has_pending_interrupt(saver, config):
                # 审批放行后的断点续行：HITL 挂起中断必须携带 resume 决策放行；
                # None 输入不会放行，仅原样重新挂起（审批循环）。
                payload: Any = await self._resume_payload(saver, config, run.get("decision"))
            else:
                payload = {"messages": [{"role": "user", "content": text}]}
            state = await agent.ainvoke(payload, config)
            interrupts = state.get("__interrupt__") or []
            if interrupts:
                raise RunSuspended(
                    {
                        "kind": "jarvis_interrupt",
                        "path": ws,
                        "detail": str(interrupts[0].value)[:300],
                        "actions": self._interrupt_actions(interrupts[0].value),
                    },
                    "jarvis_interrupt",
                )
            return self._final_reply(state)
        except EngineError:
            raise
        except RunSuspended:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise EngineError(f"jarvis 执行失败: {exc}") from exc
        finally:
            await conn.close()

    # ---------- 检查点探查（票 04 重启核对复用） ----------
    @staticmethod
    async def _has_pending_interrupt(
        checkpointer: AsyncSqliteSaver, config: dict[str, Any]
    ) -> bool:
        """pending_writes 含 __interrupt__ 通道写入 → 线程停在 HITL 审批点。"""
        tup = await checkpointer.aget_tuple(config)
        if tup is None:
            return False
        return any(
            w[1] == "__interrupt__" for w in (getattr(tup, "pending_writes", None) or [])
        )

    async def _resume_payload(
        self, checkpointer: AsyncSqliteSaver, config: dict[str, Any], decision: str | None
    ) -> Any:
        """构造 HITL 挂起中断的恢复输入（Command(resume=...)）。

        LangGraph HITL 协议：每个挂起的工具调用需要一条决策
        （{"decisions": [{"type": "approve"}, ...]}），决策条数取自检查点
        中 pending 中断值的 action_requests。仅当 run 已明确批准
        （decision='approved'，ApprovalGate.approve 写入）才放行；
        未批准的防御路径返回 None —— 引擎将原样重新挂起并再次请求审批
        （保守：绝不无批准自动放行）。
        """
        if decision != "approved":
            return None
        tup = await checkpointer.aget_tuple(config)
        writes = (getattr(tup, "pending_writes", None) or []) if tup is not None else []
        n_actions = 0
        for w in writes:
            if len(w) < 3 or w[1] != "__interrupt__":
                continue
            items = w[2] if isinstance(w[2], (list, tuple)) else [w[2]]
            for item in items:
                value = getattr(item, "value", item) or {}
                if isinstance(value, dict):
                    n_actions += len(value.get("action_requests") or [])
        return Command(resume={"decisions": [{"type": "approve"}] * max(1, n_actions)})

    @staticmethod
    def _interrupt_actions(value: Any, *, cap: int = 20, arg_cap: int = 150) -> list[dict[str, str]]:
        """从 HITL 中断值提取结构化动作 [{"tool", "arg"}]，供代审规则判断只读性。

        容忍任意脏数据：非 dict 值（如旧数据的纯字符串）→ []；非 dict 元素、
        无 name / name 非 str 的元素跳过；args 非 dict 按空；arg 取值顺序
        command → file_path → path → pattern → ""。最多 cap 条，arg 截断 arg_cap。
        """
        if not isinstance(value, dict):
            return []
        requests = value.get("action_requests")
        if not isinstance(requests, (list, tuple)):
            return []
        out: list[dict[str, str]] = []
        for item in list(requests)[:cap]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            args = item.get("args")
            args = args if isinstance(args, dict) else {}
            arg = ""
            for key in ("command", "file_path", "path", "pattern"):
                v = args.get(key)
                if v is not None:
                    arg = str(v)
                    break
            out.append({"tool": name, "arg": arg[:arg_cap]})
        return out

    async def reconcile_running(
        self, task: dict[str, Any], run: dict[str, Any], thread: str | None
    ) -> dict[str, Any]:
        """重启核对：读该 Thread 的检查点判断可否安全恢复。

        无检查点 / 停在 HITL 审批点 / 存在未完成超步 → 不安全（转等待核实）；
        仅当检查点干净（无 pending writes）才判 safe，消息齐全时附带已知结果。
        """
        config = {"configurable": {"thread_id": thread or task["task_id"]}}
        conn = await aiosqlite.connect(str(self._checkpoints_db), timeout=30.0)
        try:
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            tup = await saver.aget_tuple(config)
        finally:
            await conn.close()
        return self._reconcile_from_tuple(tup)

    @staticmethod
    def _reconcile_from_tuple(tup: Any) -> dict[str, Any]:
        if tup is None:
            return {
                "safe": False,
                "result": None,
                "detail": "无检查点，运行状态未知；不盲目重试（转等待核实）",
            }
        pending = getattr(tup, "pending_writes", None) or []
        if any(w[1] == "__interrupt__" for w in pending):
            return {
                "safe": False,
                "result": None,
                "detail": "线程停留在 HITL 审批点（挂起未续行）；需人工核实后再放行",
            }
        if pending:
            return {
                "safe": False,
                "result": None,
                "detail": "存在未完成超步（可能已产生副作用）；不盲目重试",
            }
        msgs = ((tup.checkpoint or {}).get("channel_values") or {}).get("messages")
        detail = "检查点干净完整，可安全恢复"
        return {
            "safe": True,
            "result": JarvisAdapter._final_reply({"messages": msgs}) if msgs else None,
            "detail": detail + ("（结果已知）" if msgs else ""),
        }

    # ---------- 结果提取 ----------
    @staticmethod
    def _final_reply(state: Any) -> str:
        """取最后一条非空 AI 消息（dict 或对象，content 可为 block 列表）。"""
        msgs = state.get("messages") or []

        def _content(m: Any) -> str:
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            if isinstance(c, list):
                return "".join(
                    (b.get("text") or "") if isinstance(b, dict) else str(b) for b in c
                )
            return str(c) if c is not None else ""

        def _kind(m: Any) -> str | None:
            return m.get("role") if isinstance(m, dict) else getattr(m, "type", None)

        for m in reversed(msgs):
            c = _content(m).strip()
            if c and _kind(m) in ("ai", "assistant"):
                return c
        for m in reversed(msgs):
            c = _content(m).strip()
            if c:
                return c
        return "（jarvis 未产出最终回复）"