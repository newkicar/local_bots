"""票 13 验收：左侧对话栏 + 首轮自动摘要 + 跨对话共享持久记忆。

纪律：
- 不碰真实引擎、不出网：注入脚本化假模型（ScriptedModel）+ 持握型假引擎（HoldEngine）；
- 临时目录自建（AGENTS.md 禁用 tmp_path / tempfile）。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.adapters.base import EngineAdapter
from app.conversations import ConversationStore
from app.main import create_app
from app.memory import MAX_ENTRIES, MemoryStore
from app.secretary import Secretary
from app.settings import Settings
from app.task_manager import TaskManager

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")


def _settings(name: str, **kw) -> Settings:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    return Settings(
        ld_data_dir=os.path.join(d, "data"),
        ld_workspace_dir=os.path.join(d, "workspace"),
        ld_api_token="test-token",
        _env_file=None,
        **kw,
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


class ScriptedModel(BaseChatModel):
    """脚本化假模型：按序弹出预设 AIMessage；bind_tools 原样返回 self；永不出网。

    pydantic v2 下 BaseChatModel 是 pydantic 模型：实例属性必须 object.__setattr__。
    """

    def __init__(self, script: list[AIMessage]) -> None:
        super().__init__()
        object.__setattr__(self, "_script", list(script))
        object.__setattr__(self, "_i", 0)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        idx = min(self._i, len(self._script) - 1)
        m = self._script[idx]
        object.__setattr__(self, "_i", idx + 1)
        return ChatResult(generations=[ChatGeneration(message=m)])

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


class HoldEngine(EngineAdapter):
    """持握型假引擎：execute 阻塞等待 release；用于端到端不等待任务完成。"""

    def __init__(self, release: threading.Event, key: str = "hold") -> None:
        self.name = key
        self.release = release
        self.started = threading.Event()

    async def ensure_thread(self, task, run, hooks):
        thread = task.get("engine_thread_id") or f"thread-{task['task_id']}"
        self.started.set()
        await hooks.thread_bound(thread)
        return thread

    async def execute(self, task, run, thread, hooks, *, resume_note=None) -> str:
        await self.release.wait()
        return f"held {task['task_id']}"

    def reconcile_running(self, task, run, thread):
        return {"safe": False, "result": None, "detail": "hold"}


# ---------- ConversationStore CRUD ----------

async def test_conversation_store_crud():
    """create/list/get/append/link/set_title/set_summary/delete；重复 link 去重，删除互不影响。"""
    db_dir = os.path.join(BASE, "conv_crud")
    shutil.rmtree(db_dir, ignore_errors=True)
    os.makedirs(db_dir, exist_ok=True)
    store = ConversationStore(os.path.join(db_dir, "conversations.db"))
    await store.open()
    try:
        c1 = await store.create()
        assert c1["conversation_id"].startswith("C")
        c2 = await store.create(title="第二个对话")
        # list 按 updated_at 降序（c2 后创建，排前）
        assert [c["conversation_id"] for c in await store.list_conversations()] == [
            c2["conversation_id"], c1["conversation_id"],
        ]

        await store.append_message(c1["conversation_id"], "user", "你好")
        await store.append_message(c1["conversation_id"], "assistant", "您好")
        assert await store.message_count(c1["conversation_id"]) == 2
        assert await store.last_messages(c1["conversation_id"], 2) == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "您好"},
        ]

        detail = await store.get(c1["conversation_id"])
        assert len(detail["messages"]) == 2 and detail["task_ids"] == []

        await store.link_tasks(c1["conversation_id"], ["T1", "T2"])
        await store.link_tasks(c1["conversation_id"], ["T1"])  # 重复不重插
        assert (await store.get(c1["conversation_id"]))["task_ids"] == ["T1", "T2"]

        await store.set_title(c1["conversation_id"], "标题X")
        await store.set_summary(c1["conversation_id"], "摘要X")
        d = await store.get(c1["conversation_id"])
        assert d["title"] == "标题X" and d["summary"] == "摘要X"

        assert await store.delete(c1["conversation_id"]) is True
        assert await store.get(c1["conversation_id"]) is None
        assert await store.delete(c1["conversation_id"]) is False
        # 删 c1 不影响其他对话
        assert await store.get(c2["conversation_id"]) is not None
    finally:
        await store.close()


# ---------- MemoryStore 单元 ----------

def test_memory_store_dedup_strip_persist_fifo_remove():
    """strip 去重、空内容拒收、原子持久化、FIFO 上限、越界删除 False。"""
    p = os.path.join(BASE, "mem_unit", "memory.json")
    shutil.rmtree(os.path.dirname(p), ignore_errors=True)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    mem = MemoryStore(p)
    assert mem.add("  偏好中文  ") == {"ok": True, "deduped": False}
    assert mem.list()[0]["content"] == "偏好中文"  # strip 后入库
    assert mem.add("偏好中文")["deduped"] is True
    assert mem.add("")["ok"] is False

    for i in range(55):
        mem.add(f"e{i}")
    items = mem.list()
    assert len(items) == MAX_ENTRIES == 50
    assert items[0]["content"] == "e5"  # FIFO 丢弃最早 6 条（偏好中文 + e0..e4）

    # 新实例读回（持久化）
    assert [it["content"] for it in MemoryStore(p).list()] == [it["content"] for it in items]

    assert mem.remove(0) is True
    assert mem.remove(999) is False
    assert mem.list()[0]["content"] == "e6"


# ---------- 秘书接线（save_memory 工具 + 提示词注入） ----------

def test_secretary_memory_wiring():
    """有 MemoryStore：工具集含 save_memory 且系统提示词注入记忆；无则均不含。"""
    settings = _settings("sec_mem_wiring")
    mem = MemoryStore(settings.memory_path)
    mem.add("用户偏好简洁回答")
    sec = Secretary(TaskManager(settings.tasks_db_path), None, settings, memory=mem)
    tool_names = {t.name for t in sec._build_tools()}
    assert "save_memory" in tool_names
    sp = sec.system_prompt()
    assert "跨对话共享记忆" in sp and "用户偏好简洁回答" in sp

    st0 = _settings("sec_mem_none")
    sec0 = Secretary(TaskManager(st0.tasks_db_path), None, st0)
    assert "save_memory" not in {t.name for t in sec0._build_tools()}
    assert "跨对话共享记忆" not in sec0.system_prompt()


# ---------- 记忆 API ----------

def test_memory_api_list_and_delete():
    settings = _settings("mem_api")
    Path(settings.memory_path).write_text(
        json.dumps({"items": [
            {"content": "第一条", "created_at": ""},
            {"content": "第二条", "created_at": ""},
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )
    with TestClient(create_app(settings)) as c:
        got = c.get("/api/memory", headers=_headers()).json()
        assert [it["content"] for it in got["items"]] == ["第一条", "第二条"]
        assert c.delete("/api/memory/0", headers=_headers()).status_code == 200
        got2 = c.get("/api/memory", headers=_headers()).json()
        assert [it["content"] for it in got2["items"]] == ["第二条"]
        assert c.delete("/api/memory/99", headers=_headers()).status_code == 404


# ---------- 端到端：对话归档 + 首轮自动摘要 + 删除隔离 ----------

def test_chat_conversation_end_to_end():
    """首轮 chat 建对话+自动标题+后台摘要；同 cid 第二轮上下文连续并挂任务链接；
    删除对话 404 且 tasks.db 任务行保留。"""
    release = threading.Event()
    release.set()
    settings = _settings("conv_e2e")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "submit_task", "args": {"instruction": "整理季度报表"}, "id": "s1"},
        ]),
        AIMessage(content="任务已入队"),
        AIMessage(content="摘要文本"),  # summarize 的 ainvoke 弹出第 3 条
    ])

    def factory(tm, dispatcher, st):
        return Secretary(tm, dispatcher, st, model=model)

    h = _headers()
    msg = "帮我整理一下季度报表，另外把上周的会议纪要归档，麻烦你了，谢谢配合辛苦了。"
    with TestClient(create_app(settings, engines={"jarvis": HoldEngine(release, key="jarvis")},
                              secretary_factory=factory)) as c:
        # 1) 无 conversation_id → 自动新建
        r1 = c.post("/api/chat", json={"message": msg}, headers=h)
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        cid = b1["conversation_id"]
        assert cid.startswith("C")
        assert b1["title"] == msg[:20]
        tid = b1["task_ids"][0]

        # 2) 轮询等后台摘要落库（≤5s）
        deadline = time.time() + 5
        summary = None
        while time.time() < deadline:
            summary = c.get(f"/api/conversations/{cid}", headers=h).json().get("summary")
            if summary == "摘要文本":
                break
            time.sleep(0.05)
        assert summary == "摘要文本"

        # 3) 同对话第二轮：上下文连续、消息累积、任务链接保留
        r2 = c.post("/api/chat", json={"message": "继续", "conversation_id": cid}, headers=h)
        assert r2.status_code == 200, r2.text
        assert r2.json()["conversation_id"] == cid
        # 票 14：任务终态跟进报告自动落库（HoldEngine release 已置位 → 秒完成；轮询 ≤5s）
        deadline = time.time() + 5
        d = {}
        while time.time() < deadline:
            d = c.get(f"/api/conversations/{cid}", headers=h).json()
            if any(m["role"] == "report" for m in d["messages"]):
                break
            time.sleep(0.05)
        assert [m["role"] for m in d["messages"] if m["role"] != "report"] == ["user", "assistant", "user", "assistant"]
        reports = [m for m in d["messages"] if m["role"] == "report"]
        assert len(reports) == 1 and tid in reports[0]["content"]
        assert d["task_ids"] == [tid]
        assert d["title"] == msg[:20]
        assert len(c.get("/api/conversations", headers=h).json()) == 1

        # 4) 删除对话后 404；tasks.db 任务行保留
        assert c.delete(f"/api/conversations/{cid}", headers=h).status_code == 200
        assert c.get(f"/api/conversations/{cid}", headers=h).status_code == 404
        assert c.get(f"/api/tasks/{tid}", headers=h).status_code == 200