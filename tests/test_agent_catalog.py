"""票 02 验收：Agent Catalog 持久化 + 状态机 + 内建种子（直连存储，不经 HTTP）。

纪律：本机 pytest 临时目录 WinError-5 中毒——禁用 tmp_path/tempfile.mkdtemp，
自建 tests/tmp_workspace/；pytest asyncio_mode=auto，async 测试直接写。
"""
from __future__ import annotations

import os
import shutil

import pytest

from app.agents.catalog import AgentCatalog, InvalidAgentTransition
from app.task_manager import TaskManager

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")


def _db(name: str) -> str:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return os.path.join(d, "tasks.db")


def _desc(agent_id: str, scope: str = "user", *, hash_: str = "h1", version: str = "v1",
          mode: str = "delegated_only", name: str | None = None, desc_text: str | None = None) -> dict:
    return {
        "agent_id": agent_id, "engine": "codex", "engine_profile_id": agent_id.split(":")[-1],
        "name": name or agent_id, "description": desc_text, "source_scope": scope,
        "source_id": agent_id.split(":")[-1], "source_path": None, "capabilities": {},
        "invocation_mode": mode, "profile_version": version, "profile_hash": hash_,
        "metadata": {},
    }


async def test_new_db_seeds_four_builtins_and_idempotent():
    db = _db("cat_seed")
    cat = AgentCatalog(db)
    await cat.open()
    agents = await cat.list_agents()
    assert len(agents) == 4
    modes = {a["agent_id"]: a["invocation_mode"] for a in agents}
    assert modes == {
        "jarvis:default": "direct",
        "codex:default": "direct",
        "codex:worker": "delegated_only",
        "codex:explorer": "delegated_only",
    }
    for a in agents:
        assert a["discovery_status"] == "available" and a["trust_state"] == "trusted"
        assert a["source_scope"] == "builtin"
    await cat.close()
    c2 = AgentCatalog(db)
    await c2.open()  # 重复 open 幂等（含内建种子重复 upsert）
    assert len(await c2.list_agents()) == 4
    await c2.close()


async def test_old_tasks_db_upgrade_keeps_old_tasks_readable():
    db = _db("cat_upgrade")
    tm = TaskManager(db)
    await tm.open()
    task = await tm.create_task(instruction="legacy-task")
    await tm.close()

    cat = AgentCatalog(db)
    await cat.open()  # 旧 tasks.db 上 add-only 建 agent 表
    got = await cat.get_agent("jarvis:default")
    assert got is not None and got["discovery_status"] == "available"

    tm2 = TaskManager(db)
    await tm2.open()
    row = await tm2.get_task(task["task_id"])
    assert row["instruction"] == "legacy-task"  # 旧任务列语义不变
    await tm2.close()
    await cat.close()


async def test_upsert_initial_status_by_scope_and_auto_flags():
    db = _db("cat_upsert")
    cat = AgentCatalog(db)
    await cat.open()
    a = await cat.upsert_agent(_desc("codex:u1", "user"))
    assert a["discovery_status"] == "pending_review" and a["trust_state"] == "pending"
    p = await cat.upsert_agent(_desc("codex:p1", "project"))
    assert p["discovery_status"] == "pending_review"
    await cat.close()

    cat2 = AgentCatalog(db, auto_enable_user_profiles=True)
    await cat2.open()
    assert (await cat2.upsert_agent(_desc("codex:u2", "user")))["discovery_status"] == "available"
    assert (await cat2.upsert_agent(_desc("codex:p2", "project")))["discovery_status"] == "pending_review"
    await cat2.close()

    cat3 = AgentCatalog(db, auto_enable_project_profiles=True)
    await cat3.open()
    assert (await cat3.upsert_agent(_desc("codex:p3", "project")))["discovery_status"] == "available"
    await cat3.close()


async def test_builtin_upsert_stays_available():
    db = _db("cat_builtin")
    cat = AgentCatalog(db)
    await cat.open()
    b = await cat.upsert_agent(
        {"agent_id": "codex:default", "engine": "codex", "engine_profile_id": "default",
         "name": "Codex 默认", "description": "desc", "source_scope": "builtin",
         "source_id": "default", "source_path": None, "capabilities": {},
         "invocation_mode": "direct", "profile_version": "builtin", "profile_hash": "builtin",
         "metadata": {}})
    assert b["discovery_status"] == "available" and b["trust_state"] == "trusted"
    await cat.close()


async def test_behavior_change_resets_pending_review_metadata_does_not():
    db = _db("cat_behavior")
    cat = AgentCatalog(db, auto_enable_user_profiles=True)
    await cat.open()
    d = _desc("codex:beh", "user")
    assert (await cat.upsert_agent(d))["discovery_status"] == "available"
    # 同 hash + behavior 变更 → 不拉回（仍 metadata 等价）
    assert (await cat.upsert_agent({**d, "profile_version": "v1.1"}, change_kind="behavior"))["discovery_status"] == "available"
    # hash 变化 + behavior → 拉回 pending_review 重新审批
    out = await cat.upsert_agent({**d, "profile_version": "v2", "profile_hash": "h2", "desc_text": "x"},
                                 change_kind="behavior")
    assert out["discovery_status"] == "pending_review"
    await cat.close()


async def test_mark_missing_lowers_status_and_never_deletes_rows():
    db = _db("cat_missing")
    cat = AgentCatalog(db, auto_enable_user_profiles=True)
    await cat.open()
    await cat.upsert_agent(_desc("codex:m1", "user"))
    rows = await cat.mark_missing(["codex:m1", "jarvis:default", "no-such-agent"])
    assert len(rows) == 2  # 未知 id 忽略
    assert (await cat.get_agent("codex:m1"))["discovery_status"] == "stale"      # available → stale
    assert (await cat.get_agent("jarvis:default"))["discovery_status"] == "stale"
    await cat.mark_missing(["codex:m1"])
    assert (await cat.get_agent("codex:m1"))["discovery_status"] == "unavailable"  # stale → unavailable
    await cat.mark_missing(["codex:m1"])
    assert (await cat.get_agent("codex:m1"))["discovery_status"] == "unavailable"   # 已 unavailable 保持
    assert await cat.get_agent("codex:m1") is not None  # 行永不删
    await cat.close()


async def test_transition_guard_and_trust_side_effects():
    db = _db("cat_trans")
    cat = AgentCatalog(db)
    await cat.open()
    with pytest.raises(InvalidAgentTransition):
        await cat.transition_agent("jarvis:default", "discovered")  # available → discovered 非法
    with pytest.raises(InvalidAgentTransition):
        await cat.transition_agent("jarvis:default", "weird")       # 未知状态
    with pytest.raises(KeyError):
        await cat.transition_agent("nope", "available")             # 不存在的 agent
    assert (await cat.transition_agent("jarvis:default", "disabled"))["discovery_status"] == "disabled"
    ok = await cat.transition_agent("jarvis:default", "available")
    assert ok["trust_state"] == "trusted"  # 审批通过即建立信任
    got = await cat.reject_agent("jarvis:default", trust_reject=True)
    assert got["discovery_status"] == "rejected" and got["trust_state"] == "rejected"
    with pytest.raises(InvalidAgentTransition):
        await cat.reject_agent("jarvis:default", trust_reject=True)  # rejected 终态（仅可回 pending_review/available）
    await cat.close()


async def test_override_separated_from_snapshot():
    db = _db("cat_override")
    cat = AgentCatalog(db)
    await cat.open()
    got = await cat.set_override("codex:worker", enabled=False)
    assert got["override"]["enabled"] is False
    assert got["effective_status"] == "disabled"
    assert got["discovery_status"] == "available"  # 快照状态不受 override 影响
    got = await cat.set_override("codex:worker", enabled=True, routing_priority=5, alias="W")
    assert got["effective_status"] == "available"
    assert got["override"]["routing_priority"] == 5 and got["override"]["alias"] == "W"
    with pytest.raises(ValueError):
        await cat.set_override("codex:worker", bad_field=1)
    with pytest.raises(KeyError):
        await cat.set_override("nope", enabled=True)
    await cat.close()


async def test_sync_runs_append_and_readback():
    db = _db("cat_sync")
    cat = AgentCatalog(db)
    await cat.open()
    r1 = await cat.record_sync_run(scope="user", status="ok", discovered=2, changed=1, errors=["e1"])
    r2 = await cat.record_sync_run(scope="project", status="ok")
    assert r1["sync_id"] != r2["sync_id"]
    runs = await cat.sync_runs()
    assert [r["scope"] for r in runs] == ["project", "user"]  # 倒序（最近在前）
    assert runs[0]["errors"] == [] and runs[1]["errors"] == ["e1"]
    assert runs[1]["discovered"] == 2 and runs[1]["changed"] == 1
    await cat.close()


async def test_project_registration_and_trust_states():
    db = _db("cat_proj")
    cat = AgentCatalog(db)
    await cat.open()
    p = await cat.register_project("myproj", r"D:\Projects\myproj")
    assert p["trust_state"] == "pending"
    assert (await cat.set_project_trust(p["project_id"], "trusted"))["trust_state"] == "trusted"
    assert (await cat.set_project_trust(p["project_id"], "revoked"))["trust_state"] == "revoked"
    assert [x["project_id"] for x in await cat.list_projects(trust_state="revoked")] == [p["project_id"]]
    with pytest.raises(ValueError):
        await cat.set_project_trust(p["project_id"], "weird")
    with pytest.raises(KeyError):
        await cat.set_project_trust("Px", "trusted")
    await cat.close()
