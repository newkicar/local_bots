"""票 06 验收：秘书自然语言派活、只读文件发现（授权目录+超时+上限）、任务工具接线、委派即返回。

纪律：
- 不碰真实引擎、不出网：注入脚本化假模型（ScriptedModel）+ 持握型假引擎（HoldEngine）；
- 发现类断言直接走工具层（ainvoke），验证服务端强制授权/超时/上限（权限不靠 prompt）；
- 临时目录自建（AGENTS.md 禁用 tmp_path / tempfile）。
"""
from __future__ import annotations

import asyncio
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
from app.main import create_app
from app.secretary import DEFAULT_CAPS, Secretary
from app.settings import Settings
from app.task_manager import TaskManager

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")
TERMINAL = ("reported", "failed", "cancelled", "rejected")


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


def _wait_terminal(c: TestClient, tid: str, want=TERMINAL, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    status = "?"
    while time.time() < deadline:
        t = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
        status = t["status"]
        if status in want:
            return t
        time.sleep(0.1)
    raise AssertionError(f"task {tid} 未在 {timeout}s 内到达 {want}: {status}")


class ScriptedModel(BaseChatModel):
    """脚本化假模型：按序弹出预设 AIMessage；bind_tools 原样返回 self；永不出网。

    pydantic v2 下 BaseChatModel 是 pydantic 模型：实例属性必须 object.__setattr__
    （普通赋值 / 先于 super().__init__() 的赋值都会被 pydantic 初始化清掉）。
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
    """持握型假引擎：execute 开始即置 started，随后阻塞等待 release（threading.Event，
    可跨 TestClient 的 portal 事件循环）；记录 thread 绑定以验收“同 Thread 追问”。"""

    def __init__(self, release: threading.Event, key: str = "hold") -> None:
        self.name = key
        self.release = release
        self.started = threading.Event()
        self.threads: list[str] = []
        self.exec_threads: list[str] = []
        self.results: list[str] = []

    async def ensure_thread(self, task, run, hooks):
        thread = task.get("engine_thread_id") or f"thread-{task['task_id']}"
        self.threads.append(thread)
        await hooks.thread_bound(thread)
        return thread

    async def execute(self, task, run, thread, hooks, *, resume_note=None) -> str:
        self.started.set()
        self.exec_threads.append(thread)
        await asyncio.to_thread(self.release.wait)
        result = f"held {task['task_id']} attempt={run['attempt']}"
        self.results.append(result)
        return result

    def reconcile_running(self, task, run, thread):
        return {"safe": False, "result": None, "detail": "hold"}


def test_chat_delegates_two_engines_and_returns_immediately():
    """自然语言同时派两个任务 → 显式 codex + 缺省 jarvis；委派即返回（两引擎已开始执行且任务非终态）。"""
    release = threading.Event()
    settings = _settings("sec_dual")
    eng_c, eng_j = HoldEngine(release, key="codex"), HoldEngine(release, key="jarvis")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "submit_task", "args": {"instruction": "分析项目 A", "engine": "codex"}, "id": "c1"},
            {"name": "submit_task", "args": {"instruction": "整理项目 B"}, "id": "c2"},
        ]),
        AIMessage(content="好的，两个任务已提交后台队列"),
    ])

    def factory(tm, dispatcher, st):
        return Secretary(tm, dispatcher, st, model=model)

    with TestClient(create_app(settings, engines={"codex": eng_c, "jarvis": eng_j},
                              secretary_factory=factory)) as c:
        r = c.post("/api/chat", json={"message": "让 Codex 分析项目 A，Jarvis 整理项目 B"},
                   headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"] == "好的，两个任务已提交后台队列"
        tids = body["task_ids"]
        assert len(tids) == 2

        # 委派即返回（“假异步”根治）：两个引擎已开始执行，任务尚未终态
        assert eng_c.started.wait(5) and eng_j.started.wait(5)
        for tid in tids:
            assert c.get(f"/api/tasks/{tid}", headers=_headers()).json()["status"] not in TERMINAL
        # 路由：显式 codex + 缺省 jarvis（无 LLM 路由）
        assert {c.get(f"/api/tasks/{t}", headers=_headers()).json()["engine"] for t in tids} == {"codex", "jarvis"}

        release.set()
        for tid in tids:
            assert _wait_terminal(c, tid)["status"] == "reported"
        for eng in (eng_c, eng_j):
            assert len(eng.results) == 1 and eng.results[0].startswith("held ")


def test_followup_returns_to_same_thread():
    """追问回到原任务同一 Thread：第二个 Run origin=followup，执行线程不变。"""
    release = threading.Event()
    settings = _settings("sec_follow")
    eng = HoldEngine(release, key="jarvis")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "submit_task", "args": {"instruction": "制定周计划"}, "id": "s1"},
        ]),
        AIMessage(content="任务已入队"),
    ])

    def factory(tm, dispatcher, st):
        return Secretary(tm, dispatcher, st, model=model)

    with TestClient(create_app(settings, engines={"jarvis": eng},
                              secretary_factory=factory)) as c:
        r1 = c.post("/api/chat", json={"message": "帮我制定周计划"}, headers=_headers())
        assert r1.status_code == 200, r1.text
        tid = r1.json()["task_ids"][0]
        release.set()
        assert _wait_terminal(c, tid)["status"] == "reported"

        # 第二轮对话：对原任务追问（复用同一 Thread，不混其他上下文）
        object.__setattr__(model, "_i", 0)
        object.__setattr__(model, "_script", [
            AIMessage(content="", tool_calls=[
                {"name": "followup_task", "args": {"task_id": tid, "instruction": "补充下周计划备注"}, "id": "f1"},
            ]),
            AIMessage(content="已补充备注，将在原 Thread 内追问"),
        ])
        r2 = c.post("/api/chat",
                    json={"message": "给那个任务补充下周计划的备注",
                          "history": [{"role": "user", "content": "帮我制定周计划"},
                                      {"role": "assistant", "content": "任务已入队"}]},
                    headers=_headers())
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert b2["task_ids"] == []          # 追问不产生新任务
        assert "已补充" in b2["reply"]

        assert _wait_terminal(c, tid)["status"] == "reported"
        t = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
        assert len(t["runs"]) == 2
        assert t["runs"][1]["origin"] == "followup"
        thread = f"thread-{tid}"
        assert eng.threads == [thread]                       # 首个 Run 绑定后即持久化
        assert eng.exec_threads == [thread, thread]          # 两次执行同 Thread


async def test_file_discovery_confined_read_only_capped():
    """发现工具：授权目录外 → 服务端强制拒绝（prompt 注入不能扩权）；结果命中上限即截断。"""
    d = os.path.join(BASE, "sec_disc")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    ws = os.path.join(d, "workspace")
    os.makedirs(os.path.join(ws, "proj_a", "notes"))
    Path(os.path.join(ws, "proj_a", "notes", "todo.md")).write_text("# 待办", encoding="utf-8")
    Path(os.path.join(ws, "proj_a", "app.py")).write_text("print('hello app')\n", encoding="utf-8")
    for n, txt in (("b1.txt", "hello one"), ("b2.txt", "hello two"), ("b3.txt", "hello three")):
        Path(os.path.join(ws, n)).write_text(txt, encoding="utf-8")
    outside = os.path.join(BASE, "sec_disc_outside")
    shutil.rmtree(outside, ignore_errors=True)
    os.makedirs(outside)
    Path(os.path.join(outside, "secret.txt")).write_text("outside secret", encoding="utf-8")

    st = Settings(ld_data_dir=os.path.join(d, "data"), ld_workspace_dir=ws,
                  ld_api_token="test-token", _env_file=None,
                  ld_secretary_authorized_dirs=ws)
    tm = TaskManager(st.tasks_db_path)
    await tm.open()
    try:
        sec = Secretary(
            tm, None, st,
            caps={**DEFAULT_CAPS, "glob_max_files": 2, "grep_max_matches": 1, "read_max_chars": 12},
            model=ScriptedModel([AIMessage(content="ok")]),
        )
        tools = {t.name: t for t in sec._build_tools()}

        # 授权目录内 list_dir 正常（含子目录与文件）
        d0 = json.loads(await tools["list_dir"].ainvoke({"path": ws}))
        names = {e["name"] for e in d0["entries"]}
        assert {"proj_a", "b1.txt", "b2.txt", "b3.txt"} <= names

        # 授权目录外：服务端强制拒绝，返回 JSON 错误（非 prompt 约束）
        err1 = json.loads(await tools["list_dir"].ainvoke({"path": outside}))
        assert "error" in err1 and "超出授权目录" in err1["error"]
        err2 = json.loads(await tools["peek_file"].ainvoke({"path": os.path.join(outside, "secret.txt")}))
        assert "error" in err2 and "超出授权目录" in err2["error"]

        # glob 结果上限（3 个 .txt，上限 2 → 截断）
        g = json.loads(await tools["find_files"].ainvoke({"pattern": "*.txt", "root": ws}))
        assert len(g["files"]) == 2 and "截断" in g["note"]

        # grep 命中上限（上限 1 条 → 截断）
        gr = json.loads(await tools["grep_files"].ainvoke({"pattern": "hello", "root": ws}))
        assert len(gr["matches"]) == 1 and "截断" in gr["note"]

        # peek 只读 + 按上限截断
        p = json.loads(await tools["peek_file"].ainvoke({"path": os.path.join(ws, "b1.txt"), "max_chars": 7}))
        assert p["truncated"] is True and p["text"] == "hello o"

        # 任务工具：不存在 → JSON 错误（永不向 LLM 抛异常）
        gt = json.loads(await tools["get_task"].ainvoke({"task_id": "T_no_such"}))
        assert "error" in gt and "不存在" in gt["error"]
        lt = json.loads(await tools["list_tasks"].ainvoke({}))
        assert lt["tasks"] == [] and lt["total"] == 0
    finally:
        await tm.close()


async def test_discovery_timeout_cap_path():
    """发现操作带整体超时：超时返回 JSON 提示而不是挂死（后台线程自行结束）。"""
    st = _settings("sec_timeout", ld_secretary_authorized_dirs=BASE)
    tm = TaskManager(st.tasks_db_path)
    await tm.open()
    try:
        sec = Secretary(
            tm, None, st,
            caps={**DEFAULT_CAPS, "timeout_s": 0.2, "delay_s": 1.0},   # 压测旋钮：先睡 1s 必超时
            model=ScriptedModel([AIMessage(content="ok")]),
        )
        tools = {t.name: t for t in sec._build_tools()}
        err = json.loads(await tools["list_dir"].ainvoke({"path": BASE}))
        assert "error" in err and "超时" in err["error"]
    finally:
        await tm.close()


def test_chat_endpoint_auth_and_shape():
    """/api/chat：无 Bearer 401；有则返回 {"reply","task_ids"}；支持携带历史。"""
    release = threading.Event()
    release.set()
    settings = _settings("sec_ep")
    model = ScriptedModel([AIMessage(content="你好，我可以帮你派发任务")])

    def factory(tm, dispatcher, st):
        return Secretary(tm, dispatcher, st, model=model)

    with TestClient(create_app(settings, engines={"jarvis": HoldEngine(release, key="jarvis")},
                              secretary_factory=factory)) as c:
        r0 = c.post("/api/chat", json={"message": "你好"})
        assert r0.status_code == 401

        r1 = c.post("/api/chat", json={"message": "你好"}, headers=_headers())
        assert r1.status_code == 200, r1.text
        assert r1.json() == {"reply": "你好，我可以帮你派发任务", "task_ids": []}

        r2 = c.post("/api/chat",
                    json={"message": "继续",
                          "history": [{"role": "user", "content": "你好"},
                                      {"role": "assistant", "content": "你好，我可以帮你派发任务"}]},
                    headers=_headers())
        assert r2.status_code == 200, r2.text
        assert set(r2.json().keys()) == {"reply", "task_ids"}


def test_secretary_source_discipline():
    """源码纪律：秘书模块不 os.chdir、不起 subprocess（引擎一律经 Dispatcher/TaskManager）。"""
    src = Path(__file__).resolve().parent.parent / "app" / "secretary.py"
    text = src.read_text(encoding="utf-8")
    assert "chdir" not in text
    assert "subprocess" not in text


def test_system_prompt_lists_authorized_roots():
    """秘书系统提示词附授权目录列表（LLM 发现入口；服务端强制校验由既有发现测试覆盖）。"""
    base_root = Path(BASE)
    settings = _settings("sec_roots_prompt", ld_secretary_authorized_dirs=str(base_root))
    sec = Secretary(TaskManager(settings.tasks_db_path), None, settings)
    sp = sec.system_prompt()
    assert str(base_root.resolve()) in sp
    assert "授权目录" in sp and "只读" in sp

    settings0 = _settings("sec_roots_empty", ld_secretary_authorized_dirs="")
    sec0 = Secretary(TaskManager(settings0.tasks_db_path), None, settings0)
    assert "未配置授权目录" in sec0.system_prompt()
