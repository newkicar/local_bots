"""架构评审 B1 验收：Task 提交序列收拢进 SubmissionService（假 router 驱动，不 import 任何真实引擎模块）。

覆盖：路由错误透传、引擎省略缺省、显式引擎归一、agent 提交快照与 engine_profile 回填、
project_id 覆盖/继承、默认 workspace 分配、显式 workspace 透传、routing_decision 回填、
create_task 失败包装、router 缺省纯引擎提交、router 缺省拒绝 agent/project 提交。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.agents.router import RoutingError
from app.settings import Settings
from app.submission import SubmissionService, TaskSubmissionError
from app.task_manager import TaskManager

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")

_DEFAULT_DECISION: dict[str, Any] = {
    "selected_agent_id": None,
    "engine": "jarvis",
    "engine_profile_id": None,
    "routing_mode": "explicit",
    "reason": "fake decision",
    "alternatives": [],
    "user_explicit_choice": False,
}

_AGENT_ROW: dict[str, Any] = {
    "agent_id": "jarvis:default",
    "engine": "jarvis",
    "engine_profile_id": "EP1",
    "profile_version": "v1",
    "profile_hash": "h1",
    "metadata": {"project_id": "P2"},
}

_AGENT_DECISION: dict[str, Any] = {
    **_DEFAULT_DECISION,
    "selected_agent_id": "jarvis:default",
    "engine": "codex",
    "engine_profile_id": "EP1",
}


class FakeRouter:
    """记录 resolve/agent_row 调用；decision / agent_row_value / error 按用例注入。"""

    def __init__(self) -> None:
        self.resolve_args: dict[str, Any] = {}
        self.decision: dict[str, Any] = dict(_DEFAULT_DECISION)
        self.agent_row_value: dict[str, Any] | None = None
        self.error: Exception | None = None

    async def resolve(
        self,
        *,
        agent_id: str | None = None,
        engine: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        self.resolve_args = {"agent_id": agent_id, "engine": engine, "project_id": project_id}
        if self.error is not None:
            raise self.error
        return self.decision

    async def agent_row(self, agent_id: str | None) -> dict[str, Any] | None:
        if agent_id is None:
            return None
        return self.agent_row_value


class _FailingTM(TaskManager):
    """create_task 必失败：验证提交序列统一包成 TaskSubmissionError。"""

    async def create_task(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("db down")


def _settings(name: str) -> Settings:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(d, "data"))
    return Settings(
        ld_data_dir=os.path.join(d, "data"),
        ld_workspace_dir=os.path.join(d, "workspace"),
        ld_api_token="test-token",
        _env_file=None,
    )


def _agent_fake() -> FakeRouter:
    fake = FakeRouter()
    fake.decision = _AGENT_DECISION
    fake.agent_row_value = _AGENT_ROW
    return fake


async def test_b1_01_路由错误原样透传() -> None:
    settings = _settings("b1_01_")
    fake = FakeRouter()
    fake.error = RoutingError("boom", ["a:b"])
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        with pytest.raises(RoutingError) as exc_info:
            await svc.submit("i")
        assert "boom" in exc_info.value.full_detail
    finally:
        await tm.close()


async def test_b1_02_引擎省略走缺省jarvis() -> None:
    settings = _settings("b1_02_")
    fake = FakeRouter()
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        task, engine = await svc.submit("i")
        assert fake.resolve_args["engine"] is None
        assert task["engine"] == "jarvis"
        assert engine == "jarvis"
    finally:
        await tm.close()


async def test_b1_03_显式引擎归一后传router() -> None:
    settings = _settings("b1_03_")
    fake = FakeRouter()
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        await svc.submit("i", engine="  CoDeX  ")
        assert fake.resolve_args["engine"] == "codex"
    finally:
        await tm.close()


async def test_b1_04_agent提交回填引擎与快照() -> None:
    settings = _settings("b1_04_")
    fake = _agent_fake()
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        task, engine = await svc.submit("i", agent_id="jarvis:default")
        assert engine == "codex"
        assert task["engine"] == "codex"
        assert task["agent_id"] == "jarvis:default"
        assert task["engine_profile_id"] == "EP1"
        assert task["agent_snapshot"]["agent_id"] == "jarvis:default"
    finally:
        await tm.close()


async def test_b1_05_显式project_id压agent元数据() -> None:
    settings = _settings("b1_05_")
    fake = _agent_fake()
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        task, _ = await svc.submit("i", agent_id="jarvis:default", project_id="P1")
        assert task["project_id"] == "P1"
    finally:
        await tm.close()


async def test_b1_06_project_id继承agent元数据() -> None:
    settings = _settings("b1_06_")
    fake = _agent_fake()
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        task, _ = await svc.submit("i", agent_id="jarvis:default")
        assert task["project_id"] == "P2"
    finally:
        await tm.close()


async def test_b1_07_默认workspace分配且建目录() -> None:
    settings = _settings("b1_07_")
    fake = FakeRouter()
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        task, _ = await svc.submit("i")
        expected = Path(str(settings.ld_workspace_dir)) / task["task_id"]
        assert task["workspace"] == str(expected)
        assert expected.is_dir()
    finally:
        await tm.close()


async def test_b1_08_显式workspace原样透传() -> None:
    settings = _settings("b1_08_")
    fake = FakeRouter()
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        task, _ = await svc.submit("i", workspace=r"Z:\no_such_dir_b1")
        assert task["workspace"] == r"Z:\no_such_dir_b1"
    finally:
        await tm.close()


async def test_b1_09_routing_decision回填任务() -> None:
    settings = _settings("b1_09_")
    fake = FakeRouter()
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        task, _ = await svc.submit("i", engine="jarvis")
        assert task["routing_decision"]["reason"] == "fake decision"
    finally:
        await tm.close()


async def test_b1_10_create_task失败包提交异常() -> None:
    settings = _settings("b1_10_")
    fake = FakeRouter()
    tm = _FailingTM(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, fake, settings)
        with pytest.raises(TaskSubmissionError) as exc_info:
            await svc.submit("i")
        assert "任务创建失败" in exc_info.value.detail
    finally:
        await tm.close()


async def test_b1_11_router缺省纯引擎提交() -> None:
    settings = _settings("b1_11_")
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, None, settings)
        task, engine = await svc.submit("i", engine="  CoDeX  ")
        assert engine == "codex"
        assert task["engine"] == "codex"
        assert task["agent_id"] is None
        assert task["agent_snapshot"] is None
        assert task["workspace"] is not None
        assert os.path.isdir(task["workspace"])
    finally:
        await tm.close()


async def test_b1_12_router缺省拒绝agent提交() -> None:
    settings = _settings("b1_12_")
    tm = TaskManager(settings.tasks_db_path)
    await tm.open()
    try:
        svc = SubmissionService(tm, None, settings)
        with pytest.raises(TaskSubmissionError) as exc_info:
            await svc.submit("i", agent_id="jarvis:default")
        assert "agent 路由未接线" in exc_info.value.detail
        with pytest.raises(TaskSubmissionError):
            await svc.submit("i", project_id="P1")
    finally:
        await tm.close()
