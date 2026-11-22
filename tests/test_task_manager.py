"""TaskManager 状态机与持久化单元测试（异步，直接驱动 TM）。"""
from __future__ import annotations

import os
import shutil

from app.task_manager import TASK_TERMINAL_STATES, TaskManager, InvalidTransition

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")


def _dir(name: str) -> str:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


async def _tm(name: str, **kw):
    tm = TaskManager(os.path.join(_dir(name), "unit_tm.db"), **kw)
    await tm.open()
    return tm


async def test_transition_with_empty_from_states_raises():
    tm = await _tm("guard")
    t = await tm.create_task(instruction="x")
    try:
        await tm.transition_task(t["task_id"], "running", from_states=set())
        raise AssertionError("应当抛出 InvalidTransition")
    except InvalidTransition:
        pass
    finally:
        await tm.close()


async def test_lifecycle_guards():
    tm = await _tm("lifecycle")
    t = await tm.create_task(instruction="x")
    tid = t["task_id"]
    run = t["runs"][0]
    assert await tm.transition_task(tid, "running") is True
    assert await tm.transition_task(tid, "running") is False  # 守卫拦截
    rid = run["run_id"]
    assert await tm.transition_run(rid, "running", started_at="t0") is True
    assert await tm.transition_run(rid, "completed", result="ok", finished_at="t1") is True
    assert await tm.transition_task(tid, "completed", result="ok") is True
    assert await tm.transition_task(tid, "reported") is True
    assert await tm.transition_task(tid, "running") is False  # reported 不能回 running
    await tm.close()


async def test_fifo_next_queued_run_with_exclude():
    tm = await _tm("fifo")
    t1 = await tm.create_task(instruction="first")
    t2 = await tm.create_task(instruction="second")
    r1 = t1["runs"][0]
    r2 = t2["runs"][0]
    nxt = await tm.next_queued_run(set())
    assert nxt["run_id"] == r1["run_id"]
    nxt = await tm.next_queued_run({t1["task_id"]})
    assert nxt["run_id"] == r2["run_id"]
    assert await tm.next_queued_run({t1["task_id"], t2["task_id"]}) is None
    await tm.close()


async def test_followup_new_run_not_overwrite():
    tm = await _tm("followup")
    t = await tm.create_task(instruction="v1")
    tid = t["task_id"]
    first_run = t["runs"][0]
    try:
        await tm.followup_task(tid, "v2")
        raise AssertionError("活跃任务追问应抛 InvalidTransition")
    except InvalidTransition:
        pass
    await tm.transition_task(tid, "failed", error="boom")
    assert tid and tm.get_task is not None
    t2 = await tm.followup_task(tid, "v2")
    runs = t2["runs"]
    assert [r["origin"] for r in runs] == ["submitted", "followup"]
    assert [r["attempt"] for r in runs] == [1, 2]
    assert runs[0]["run_id"] == first_run["run_id"]
    assert runs[0]["run_id"] != runs[1]["run_id"]
    assert t2["status"] == "queued"
    await tm.close()


async def test_side_effect_ledger_appends_and_persists():
    tm = await _tm("ledger")
    t = await tm.create_task(instruction="x")
    rid = t["runs"][0]["run_id"]
    ledger = await tm.append_side_effect(rid, {"kind": "codex_exec", "detail": "run1"})
    assert len(ledger) == 1
    ledger = await tm.append_side_effect(rid, {"kind": "codex_exec", "detail": "run2", "path": "D:\\x"})
    assert len(ledger) == 2
    persisted = await tm.get_run(rid)
    assert persisted["side_effects"] == ledger
    await tm.close()


async def test_suspend_and_resume_merge_approved_paths():
    tm = await _tm("suspend")
    t = await tm.create_task(instruction="x")
    tid, rid = t["task_id"], t["runs"][0]["run_id"]
    await tm.transition_task(tid, "running")
    await tm.transition_run(rid, "running")
    await tm.suspend_run(rid, wait_reason="blacklist",
                          pending_op={"kind": "write", "path": "C:\\a"},
                          approved_paths=["C:\\a"])
    run = await tm.get_run(rid)
    assert run["state"] == "waiting-approval"
    assert run["pending_op"]["approved_paths"] == ["C:\\a"]
    await tm.resume_wait_for_approved_path(rid, ["C:\\b", "C:\\a"])
    run = await tm.get_run(rid)
    assert sorted(run["pending_op"]["approved_paths"]) == ["C:\\a", "C:\\b"]
    assert set(run["pending_op"].keys()) >= {"pending_op", "approved_paths"}
    await tm.close()


async def test_incomplete_tasks_lists_active_states():
    tm = await _tm("incomplete")
    t1 = await tm.create_task(instruction="a")
    t2 = await tm.create_task(instruction="b")
    t3 = await tm.create_task(instruction="c")
    for tid, to_state, run_to in ((t1["task_id"], "running", "running"),
                                   (t2["task_id"], "waiting-approval", None)):
        assert await tm.transition_task(tid, to_state)
        if run_to:
            assert await tm.transition_run(t1["runs"][0]["run_id"], run_to)
    await tm.transition_task(t3["task_id"], "failed", error="x")
    incomplete = await tm.incomplete_tasks()
    ids = {t["task_id"] for t in incomplete}
    assert ids == {t1["task_id"], t2["task_id"]}
    assert t3["task_id"] not in ids
    await tm.close()


async def test_active_task_ids_reflects_running_and_waiting():
    tm = await _tm("active_ids")
    t1 = await tm.create_task(instruction="a")
    t2 = await tm.create_task(instruction="b")
    assert await tm.active_task_ids() == set()
    await tm.transition_run(t1["runs"][0]["run_id"], "running")
    assert await tm.active_task_ids() == {t1["task_id"]}
    await tm.transition_run(t2["runs"][0]["run_id"], "waiting-approval")
    assert await tm.active_task_ids() == {t1["task_id"], t2["task_id"]}
    await tm.transition_run(t1["runs"][0]["run_id"], "completed", result="done")
    assert await tm.active_task_ids() == {t2["task_id"]}
    await tm.close()
