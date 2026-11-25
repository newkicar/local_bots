"""票 14 验收：任务终态/待审批的确定性跟进报告。

纪律（同票 01/13）：
- 不碰真实引擎、不出网：TaskManager + ConversationStore + FakeHub 直驱
  （fu01..fu06），fu07 反查排序单测，fu08 走真 create_app 的挂起→waiting-approval E2E；
- 临时目录自建（AGENTS.md 禁用 tmp_path / tempfile），复用 tests/tmp_workspace。
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.conversations import ConversationStore
from app.followup import handle_task_event
from app.main import create_app
from app.secretary import Secretary
from app.task_manager import TaskManager
from tests.fakes import FakeEngine
from tests.test_conversations import ScriptedModel, _headers, _settings


class FakeHub:
    """只记录 publish；用于验证跟进报告经 SSE 面（conversation_updated）送达。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)


def _make(name: str) -> tuple[TaskManager, ConversationStore, FakeHub]:
    """直驱三件套：tm.notify 晚期绑定 handle_task_event（鸭子类型，失败隔离）。"""
    settings = _settings(name)
    fake = FakeHub()

    async def notify(ev):
        await handle_task_event(ev, tm, conv, fake)

    tm = TaskManager(settings.tasks_db_path, notify=notify)
    conv = ConversationStore(settings.conversations_db_path)
    return tm, conv, fake


async def test_fu01_completed_report_closes_to_reported():
    """completed → 恰 1 条 report（含 tid 与结果）+ completed→reported 闭环 + 1 个 SSE 事件；
    中间态（queued/running）与 reported 二次迁移不产生任何 report。"""
    tm, conv, fake = _make("fu01")
    await tm.open()
    await conv.open()
    try:
        cid = (await conv.create())["conversation_id"]
        tid = (await tm.create_task(instruction="整理季度报表", engine="fake"))["task_id"]
        await conv.link_tasks(cid, [tid])

        await tm.transition_task(tid, "running")
        assert await conv.message_count(cid) == 0
        assert not fake.events, "中间态（queued/running）不得推送"

        await tm.transition_task(tid, "completed", result="季度报表已生成 [结果摘要]")
        reports = [m for m in (await conv.get(cid))["messages"] if m["role"] == "report"]
        assert len(reports) == 1
        assert tid in reports[0]["content"] and "季度报表已生成 [结果摘要]" in reports[0]["content"]
        assert (await tm.get_task(tid))["status"] == "reported"
        assert fake.events == [{"type": "conversation_updated", "conversation_id": cid}]
    finally:
        await tm.close()
        await conv.close()


async def test_fu02_failed_report_keeps_failed():
    """failed → report 含错误原因；状态停在 failed（不迁 reported）。"""
    tm, conv, fake = _make("fu02")
    await tm.open()
    await conv.open()
    try:
        cid = (await conv.create())["conversation_id"]
        tid = (await tm.create_task(instruction="跑备份", engine="fake"))["task_id"]
        await conv.link_tasks(cid, [tid])
        await tm.transition_task(tid, "running")
        await tm.transition_task(tid, "failed", error="engine boom")
        reports = [m for m in (await conv.get(cid))["messages"] if m["role"] == "report"]
        assert len(reports) == 1
        assert tid in reports[0]["content"] and "engine boom" in reports[0]["content"]
        assert (await tm.get_task(tid))["status"] == "failed"
    finally:
        await tm.close()
        await conv.close()


async def test_fu03_rejected_report_with_reason():
    """rejected → report 含拒绝原因；状态停在 rejected。"""
    tm, conv, fake = _make("fu03")
    await tm.open()
    await conv.open()
    try:
        cid = (await conv.create())["conversation_id"]
        tid = (await tm.create_task(instruction="删日志", engine="fake"))["task_id"]
        await conv.link_tasks(cid, [tid])
        await tm.transition_task(tid, "running")
        await tm.transition_task(tid, "rejected", reject_reason="审批拒绝：越权写 C:\\secret")
        reports = [m for m in (await conv.get(cid))["messages"] if m["role"] == "report"]
        assert len(reports) == 1
        assert "审批拒绝：越权写 C:\\secret" in reports[0]["content"]
        assert (await tm.get_task(tid))["status"] == "rejected"
    finally:
        await tm.close()
        await conv.close()


async def test_fu04_cancelled_report_notes_ledger():
    """cancelled → report 注明 Side-Effect Ledger 只追加不回滚；状态停在 cancelled。"""
    tm, conv, fake = _make("fu04")
    await tm.open()
    await conv.open()
    try:
        cid = (await conv.create())["conversation_id"]
        tid = (await tm.create_task(instruction="长任务", engine="fake"))["task_id"]
        await conv.link_tasks(cid, [tid])
        await tm.transition_task(tid, "cancelled")
        reports = [m for m in (await conv.get(cid))["messages"] if m["role"] == "report"]
        assert len(reports) == 1
        assert "只追加不回滚" in reports[0]["content"]
        assert (await tm.get_task(tid))["status"] == "cancelled"
    finally:
        await tm.close()
        await conv.close()


async def test_fu05_unlinked_task_no_report_no_transition():
    """无对话链接 → handler 空转：0 条 report、0 个 SSE 事件、completed 不迁 reported。"""
    tm, conv, fake = _make("fu05")
    await tm.open()
    await conv.open()
    try:
        cid = (await conv.create())["conversation_id"]
        tid = (await tm.create_task(instruction="无人认领", engine="fake"))["task_id"]
        assert await conv.find_conversations_for_task(tid) == []
        await tm.transition_task(tid, "running")
        await tm.transition_task(tid, "completed", result="ok")
        assert await conv.message_count(cid) == 0
        assert not fake.events
        assert (await tm.get_task(tid))["status"] == "completed"
    finally:
        await tm.close()
        await conv.close()


async def test_fu06_two_conversations_each_one_report():
    """同一任务被两个对话链接 → completed 时每个对话恰 1 条 report（各自独立）。"""
    tm, conv, fake = _make("fu06")
    await tm.open()
    await conv.open()
    try:
        c1 = (await conv.create())["conversation_id"]
        c2 = (await conv.create())["conversation_id"]
        tid = (await tm.create_task(instruction="双对话任务", engine="fake"))["task_id"]
        await conv.link_tasks(c1, [tid])
        await conv.link_tasks(c2, [tid])
        assert await conv.find_conversations_for_task(tid) == [c1, c2]
        await tm.transition_task(tid, "running")
        await tm.transition_task(tid, "completed", result="双对话结果")
        for cid in (c1, c2):
            reports = [m for m in (await conv.get(cid))["messages"] if m["role"] == "report"]
            assert len(reports) == 1
            assert tid in reports[0]["content"] and "双对话结果" in reports[0]["content"]
        assert len(fake.events) == 2
    finally:
        await tm.close()
        await conv.close()


async def test_fu07_find_conversations_for_task_ordering():
    """未 link → []；先后 link 按 rowid 序返回（不反查、不排序错乱）。"""
    tm, conv, fake = _make("fu07")
    await tm.open()
    await conv.open()
    try:
        tid = "T-unknown"
        assert await conv.find_conversations_for_task(tid) == []
        c1 = (await conv.create())["conversation_id"]
        c2 = (await conv.create())["conversation_id"]
        await conv.link_tasks(c1, [tid])
        assert await conv.find_conversations_for_task(tid) == [c1]
        await conv.link_tasks(c2, [tid])
        assert await conv.find_conversations_for_task(tid) == [c1, c2]
    finally:
        await tm.close()
        await conv.close()


def test_fu08_waiting_approval_report_e2e():
    """挂起链路 E2E：FakeEngine 首次 execute 挂起 → run/task 均 waiting-approval →
    跟进报告自动落库且含挂起原因；任务状态保持 waiting-approval（GET 惰性迁移只兜 completed）。"""
    settings = _settings("fu08")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "submit_task",
             "args": {"instruction": "整理季度报表", "engine": "fake"}, "id": "s1"}]),
        AIMessage(content="任务已入队"),
        AIMessage(content="摘要文本"),
    ])

    def factory(tm, dispatcher, st):
        return Secretary(tm, dispatcher, st, model=model)

    h = _headers()
    with TestClient(create_app(settings, engines={"fake": FakeEngine(suspend_times=1)},
                               secretary_factory=factory)) as c:
        r = c.post("/api/chat", json={"message": "帮我整理季度报表"}, headers=h)
        assert r.status_code == 200, r.text
        b = r.json()
        cid = b["conversation_id"]
        tid = b["task_ids"][0]
        deadline = time.time() + 5
        reports: list[dict] = []
        while time.time() < deadline:
            reports = [m for m in c.get(f"/api/conversations/{cid}", headers=h).json()["messages"]
                       if m["role"] == "report"]
            if reports:
                break
            time.sleep(0.05)
        assert len(reports) == 1
        assert "fake-blacklist" in reports[0]["content"] and tid in reports[0]["content"]
        assert c.get(f"/api/tasks/{tid}", headers=h).json()["status"] == "waiting-approval"