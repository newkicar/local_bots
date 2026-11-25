"""票 08 验收：tasks 表 Agent 身份 + 快照列（迁移 / 全链路 / 创建冻结 / SubmitTask 校验）。

锚点模式沿用 tests/test_task_manager.py（_tm 直驱 TM）与 tests/test_agent_api.py
（_settings/TestClient，假引擎）；自建 tests/tmp_workspace，禁 tmp_path。
"""
from __future__ import annotations

import os
import shutil

import aiosqlite
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from app.task_manager import AGENT_TASK_COLUMNS, TaskManager
from tests.fakes import FakeEngine

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")

_OLD_TASKS = """
CREATE TABLE IF NOT EXISTS tasks(
    task_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    engine TEXT NOT NULL DEFAULT \'jarvis\',
    engine_thread_id TEXT,
    workspace TEXT,
    status TEXT NOT NULL DEFAULT \'queued\',
    parent_task_id TEXT,
    instruction TEXT,
    result TEXT,
    error TEXT,
    reject_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _settings(name: str, **kw) -> Settings:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return Settings(
        ld_data_dir=os.path.join(d, "data"),
        ld_workspace_dir=os.path.join(d, "workspace"),
        ld_api_token="test-token",
        ld_agent_sync_on_startup=False,
        _env_file=None,
        **kw,
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _dir(name: str) -> str:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


async def _tm(name: str, **kw) -> TaskManager:
    tm = TaskManager(os.path.join(_dir(name), "unit_tm.db"), **kw)
    await tm.open()
    return tm


async def _old_db(path: str) -> None:
    """模拟票 08 之前的旧 tasks.db（无 writes_authorized、无 6 个 agent 列）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_OLD_TASKS)
        await db.execute(
            "INSERT INTO tasks(task_id, seq, status, instruction, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("Told00000001", 1, "reported", "旧任务", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )
        await db.commit()


async def test_old_db_upgrade_adds_agent_columns():
    path = os.path.join(_dir("p08_old"), "legacy.db")
    await _old_db(path)
    tm = TaskManager(path)
    await tm.open()
    async with tm.db.execute("PRAGMA table_info(tasks)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    for col in AGENT_TASK_COLUMNS:
        assert col in cols, col
    assert "writes_authorized" in cols  # 票 02 迁移同时补齐
    old = await tm.get_task("Told00000001")
    assert old is not None
    assert old["agent_id"] is None and old["project_id"] is None
    assert old["agent_snapshot"] is None and old["engine_profile_id"] is None
    assert old["writes_authorized"] is False
    # 旧行可继续走状态机（reported → queued followup 路径不被新列破坏）
    assert (await tm.incomplete_tasks()) == []
    await tm.close()


async def test_create_task_old_path_defaults_none():
    tm = await _tm("p08_default")
    task = await tm.create_task(instruction="x")
    for key in ("agent_id", "project_id", "engine_profile_id", "engine_profile_version",
                "engine_profile_hash", "agent_snapshot"):
        assert task[key] is None, key
    assert task["engine"] == "jarvis"
    await tm.close()


async def test_create_task_freezes_snapshot_fields():
    tm = await _tm("p08_snap")
    snapshot = {"agent_id": "codex:alpha", "engine": "codex", "engine_profile_id": "alpha",
                "engine_profile_version": "v1", "engine_profile_hash": "h1", "name": "Alpha"}
    task = await tm.create_task(
        instruction="用 alpha 跑", engine="codex",
        agent_id="codex:alpha", project_id="Pabc123",
        engine_profile_id="alpha", engine_profile_version="v1",
        engine_profile_hash="h1", agent_snapshot=snapshot,
    )
    assert task["agent_id"] == "codex:alpha"
    assert task["agent_snapshot"] == snapshot  # JSON round-trip
    assert task["project_id"] == "Pabc123"
    assert task["engine_profile_hash"] == "h1"
    assert (await tm.get_task(task["task_id"]))["agent_snapshot"]["engine_profile_version"] == "v1"
    listed = await tm.list_tasks()
    assert listed[0]["agent_id"] == "codex:alpha"
    incomplete = await tm.incomplete_tasks()  # queued 任务在列
    assert len(incomplete) == 1 and incomplete[0]["agent_snapshot"] == snapshot
    await tm.close()


def test_submit_task_agent_validation():
    settings = _settings("p08_api")
    with TestClient(create_app(settings, {"fake": FakeEngine(), "codex": FakeEngine(), "jarvis": FakeEngine()})) as c:
        cat = c.app.state.catalog

        # 未知 agent → 422
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "codex:ghost"},
                   headers=_headers())
        assert r.status_code == 422 and "agent not found" in r.json()["detail"], r.text

        # 待审 agent → 拒绝创建
        c.portal.call(cat.upsert_agent, {
            "agent_id": "codex:beta", "engine": "codex", "engine_profile_id": "beta",
            "name": "Beta", "source_scope": "user", "source_id": "beta",
            "metadata": {}, "profile_hash": "hB"})
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "codex:beta"},
                   headers=_headers())
        assert r.status_code == 422 and "pending_review" in r.json()["detail"], r.text

        # override 禁用 → effective_status=disabled → 422
        r = c.post("/api/agents/codex:worker/disable", headers=_headers())
        assert r.status_code == 200
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "codex:worker"},
                   headers=_headers())
        assert r.status_code == 422 and "disabled" in r.json()["detail"], r.text
        assert c.post("/api/agents/codex:worker/enable", headers=_headers()).status_code == 200

        # 引擎不匹配 → 422
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "jarvis:default",
                                       "engine": "codex"}, headers=_headers())
        assert r.status_code == 422 and "不匹配" in r.json()["detail"], r.text

        # 合法：engine 缺省 → 取 agent 的 engine；快照冻结落库
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "codex:default"},
                   headers=_headers())
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["engine"] == "codex"
        assert body["agent_id"] == "codex:default"
        assert body["agent_snapshot"]["engine_profile_hash"] == "builtin"
        assert body["agent_snapshot"]["invocation_mode"] == "direct"
        assert body["project_id"] is None

        # 合法：显式 engine 与 agent 引擎一致
        r = c.post("/api/tasks", json={"instruction": "y", "agent_id": "codex:default",
                                       "engine": "codex"}, headers=_headers())
        assert r.status_code == 201 and r.json()["engine"] == "codex", r.text


def test_submit_task_project_agent_binding():
    settings = _settings("p08_proj")
    with TestClient(create_app(settings, {"fake": FakeEngine(), "codex": FakeEngine(), "jarvis": FakeEngine()})) as c:
        cat = c.app.state.catalog
        import pathlib

        root = pathlib.Path(BASE, "p08_proj_root")
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root)
        project = c.portal.call(cat.register_project, "proj-p08", str(root))
        pid = project["project_id"]
        aid = f"codex:project:{pid}:beta"
        c.portal.call(cat.upsert_agent, {
            "agent_id": aid, "engine": "codex", "engine_profile_id": "beta",
            "name": "Beta", "source_scope": "project", "source_id": f"{pid}:beta",
            "metadata": {"project_id": pid}, "profile_hash": "hP"})
        c.portal.call(cat.transition_agent, aid, "available")
        c.portal.call(c.app.state.trust.trust_project, pid)

        # project_id 匹配（显式）→ 201
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": aid, "project_id": pid},
                   headers=_headers())
        assert r.status_code == 201 and r.json()["project_id"] == pid, r.text

        # project_id 缺省 → 由 agent metadata 推导
        r = c.post("/api/tasks", json={"instruction": "y", "agent_id": aid}, headers=_headers())
        assert r.status_code == 201 and r.json()["project_id"] == pid, r.text

        # project_id 不匹配 → 422
        r = c.post("/api/tasks", json={"instruction": "z", "agent_id": aid,
                                       "project_id": "Pother000000"}, headers=_headers())
        assert r.status_code == 422 and "不匹配" in r.json()["detail"], r.text

        # 无 agent 时 project_id 原样记录（不做信任校验，路由在票 11）
        r = c.post("/api/tasks", json={"instruction": "w", "project_id": pid}, headers=_headers())
        assert r.status_code == 201 and r.json()["project_id"] == pid, r.text
