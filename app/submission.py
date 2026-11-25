"""架构评审 B1：Task 提交序列收拢为唯一接缝——main/secretary 双调用方共用。

统一提交序列：router.resolve → agent 快照 → create_task → 默认 workspace 分配
→ routing_decision 回填。返回 (task 行, 生效引擎)。

Q4 修订：main HTTP 面保持 workspace 透传（test_submit_fields_roundtrip 锁定），
isdir 护栏留在 secretary LLM 工具包装层，本模块不做存在性校验。
engine 显式指定时归一（strip + lower）后传 router；省略时不传，由 router 缺省引擎兜底。
router=None 降级路径（B1 修订 2，恢复旧 secretary 契约）：仅允许纯引擎提交
（无 agent/project/decision 回填）；显式 agent_id/project_id 抛 TaskSubmissionError。
"""
from __future__ import annotations

import json
from typing import Any

from app.agents.router import AgentRouter, freeze_snapshot
from app.settings import Settings
from app.task_manager import TaskManager


class TaskSubmissionError(Exception):
    """提交序列内部失败（create_task 等）：detail 直接给调用方呈现。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class SubmissionService:
    """Task 提交唯一接缝：引擎路由 → 快照 → 入库 → 默认 workspace → decision 回填。"""

    def __init__(
        self,
        tm: TaskManager,
        router: AgentRouter | None,
        settings: Settings,
        *,
        default_engine: str = "jarvis",
    ) -> None:
        self._tm = tm
        self._router = router
        self._settings = settings
        self._default_engine = default_engine

    async def submit(
        self,
        instruction: str,
        *,
        engine: str | None = None,
        workspace: str | None = None,
        parent_task_id: str | None = None,
        writes_authorized: bool = False,
        agent_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """提交一个 Task；RoutingError 原样透传（HTTP 422 / 工具 JSON 错误由调用方决定）。"""
        eng = (engine or self._default_engine).strip().lower()
        if self._router is None:
            # 降级路径：router 未接线时 agent/project 维度整体不可用，仅纯引擎提交
            if agent_id is not None or project_id is not None:
                raise TaskSubmissionError(
                    "agent 路由未接线（router 未接线），拒绝显式 agent/project 提交"
                )
            return await self._submit_plain(
                instruction,
                eng,
                workspace,
                parent_task_id,
                writes_authorized,
            )
        # 显式指定 → 归一后交叉校验；省略 → 不传，router 应用其缺省引擎
        cross_check = eng if engine is not None else None
        decision = await self._router.resolve(agent_id=agent_id, engine=cross_check, project_id=project_id)
        selected = decision["selected_agent_id"]
        agent_row = await self._router.agent_row(selected) if selected else None
        task_project_id = (
            project_id or ((agent_row.get("metadata") or {}).get("project_id") if agent_row else None)
        )
        try:
            task = await self._tm.create_task(
                instruction=instruction,
                engine=decision["engine"] or eng,
                workspace=workspace,
                parent_task_id=parent_task_id,
                writes_authorized=writes_authorized,
                agent_id=selected,
                project_id=task_project_id,
                engine_profile_id=decision["engine_profile_id"],
                engine_profile_version=agent_row.get("profile_version") if agent_row else None,
                engine_profile_hash=agent_row.get("profile_hash") if agent_row else None,
                agent_snapshot=freeze_snapshot(agent_row) if agent_row else None,
            )
        except Exception as exc:
            raise TaskSubmissionError(f"任务创建失败: {exc}") from exc
        if task["workspace"] is None:
            ws = self._settings.ld_workspace_dir / task["task_id"]
            ws.mkdir(parents=True, exist_ok=True)
            await self._tm.set_task_workspace(task["task_id"], str(ws))
        await self._tm.transition_task(
            task["task_id"], "queued", from_states={"queued"},
            routing_decision_json=json.dumps(decision, ensure_ascii=False),
        )
        task = await self._tm.get_task(task["task_id"])
        assert task is not None
        return task, task["engine"]

    async def _submit_plain(
        self,
        instruction: str,
        eng: str,
        workspace: str | None,
        parent_task_id: str | None,
        writes_authorized: bool,
    ) -> tuple[dict[str, Any], str]:
        """router=None 降级路径：纯引擎提交，无 agent 快照与 decision 回填。"""
        try:
            task = await self._tm.create_task(
                instruction=instruction,
                engine=eng,
                workspace=workspace,
                parent_task_id=parent_task_id,
                writes_authorized=writes_authorized,
            )
        except Exception as exc:
            raise TaskSubmissionError(f"任务创建失败: {exc}") from exc
        if task["workspace"] is None:
            ws = self._settings.ld_workspace_dir / task["task_id"]
            ws.mkdir(parents=True, exist_ok=True)
            await self._tm.set_task_workspace(task["task_id"], str(ws))
        task = await self._tm.get_task(task["task_id"])
        assert task is not None
        return task, task["engine"]