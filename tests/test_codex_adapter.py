"""票 02：codex exec 子进程适配 + 粗粒度黑名单兜底（假 spawn 单测 + 可跳过的真 CLI 冒烟）。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app.adapters.codex import CodexAdapter
from app.main import create_app
from tests.fakes import FakeEngine
from tests.test_api_skeleton import (
    TERMINAL, _headers, _settings, _wait_terminal,
)


class FakeStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def drain(self) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._d = data

    async def read(self) -> bytes:
        return self._d


class FakeProc:
    """模拟 codex 子进程（duck-typed）：JSONL 事件流 + 可注入退出码/阻塞 stdout。"""

    def __init__(self, events: list[str], rc: int = 0, stderr: str = "",
                 block_stdout: bool = False) -> None:
        self._lines = [e.encode("utf-8") + b"\n" for e in events]
        self.returncode = rc
        self._stderr = stderr.encode("utf-8")
        self._block = block_stdout
        self._block_event = asyncio.Event()
        self.killed = False
        self.stdin = FakeStdin()
        self.stdout = self
        self.stderr = FakeStderr(self._stderr)
        self.pid = 42424

    async def readline(self) -> bytes:
        if self._block:
            await self._block_event.wait()  # 永不返回，直到任务被取消
        if self._lines:
            return self._lines.pop(0)
        return b""

    async def read(self, n: int = -1) -> bytes:
        """票 11：分块 read 兼容（适配器自组行缓冲，不再依费 readline）。"""
        if self._block:
            await self._block_event.wait()
        if not self._lines:
            return b""
        data = self._lines.pop(0)
        if 0 <= n < len(data):
            self._lines.insert(0, data[n:])
            return data[:n]
        return data

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class FakeSpawn:
    def __init__(self, *, events: list[str] | None = None, rc: int = 0,
                 stderr: str = "", block_stdout: bool = False) -> None:
        self.events = events or []
        self.rc = rc
        self.stderr = stderr
        self.block_stdout = block_stdout
        self.calls: list[dict] = []
        self.last_proc: FakeProc | None = None

    async def __call__(self, cmd: list[str], cwd: str) -> FakeProc:
        self.calls.append({"cmd": list(cmd), "cwd": cwd})
        self.last_proc = FakeProc(self.events, rc=self.rc, stderr=self.stderr,
                                  block_stdout=self.block_stdout)
        return self.last_proc


def _codex_events(thread: str = "01TESTTHREAD", result: str = "done codex") -> list[str]:
    return [
        json.dumps({"type": "thread.started", "thread_id": thread}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "shell", "command": "echo hi", "exit_code": 0}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": result}}),
        json.dumps({"type": "turn.completed"}),
    ]


def _wait_status(c: TestClient, tid: str, want: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    cur: dict = {}
    while time.time() < deadline:
        cur = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
        if cur["status"] == want:
            return cur
        time.sleep(0.1)
    raise AssertionError(f"task {tid} 未在 {timeout}s 内到 {want}: {cur.get('status')}")


def test_codex_submit_lifecycle_flags_stdin_ledger():
    spawn = FakeSpawn(events=_codex_events())
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    settings = _settings("codex-basic")
    with TestClient(create_app(settings, {"codex": adapter})) as c:
        r = c.post("/api/tasks", json={"instruction": "只回复一个词：OK", "engine": "codex"},
                   headers=_headers())
        assert r.status_code == 201
        t = _wait_terminal(c, r.json()["task_id"])
        assert t["status"] == "reported"
        assert t["engine_thread_id"] == "01TESTTHREAD"        # thread 建后立即写库
        assert "done codex" in (t["result"] or "")
        assert t["workspace"] and os.path.isdir(t["workspace"])  # 自动分配每任务 workspace
        cmd = spawn.calls[0]["cmd"]
        assert cmd[0] == "codex" and cmd[1] == "exec"
        assert cmd[cmd.index("-C") + 1] == t["workspace"]
        assert cmd[cmd.index("-s") + 1] == "read-only"
        assert "--json" in cmd and "--skip-git-repo-check" in cmd
        assert "--ephemeral" in cmd  # 票 11 E2E：一次性任务不写全局线程存储
        assert spawn.last_proc.stdin.data.decode("utf-8") == "只回复一个词：OK"
        assert any(e["kind"] == "shell" for e in t["runs"][0]["side_effects"])


def test_codex_followup_resumes_thread_without_c_s():
    spawn = FakeSpawn(events=_codex_events())
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    settings = _settings("codex-followup")
    with TestClient(create_app(settings, {"codex": adapter})) as c:
        r = c.post("/api/tasks", json={"instruction": "第一步", "engine": "codex"},
                   headers=_headers())
        tid = r.json()["task_id"]
        _wait_terminal(c, tid)
        r2 = c.post(f"/api/tasks/{tid}/followup", json={"instruction": "第二步"},
                    headers=_headers())
        assert r2.status_code == 200
        _wait_terminal(c, tid)
        assert len(spawn.calls) == 2
        cmd2 = spawn.calls[1]["cmd"]
        assert cmd2[1] == "exec" and cmd2[2] == "resume" and cmd2[3] == "01TESTTHREAD"
        assert "-C" not in cmd2 and "-s" not in cmd2
        assert "--ephemeral" not in cmd2  # resume 需读持久化线程，不带 ephemeral
        assert spawn.last_proc.stdin.data.decode("utf-8") == "第二步"


def test_codex_writes_authorized_switches_sandbox():
    spawn = FakeSpawn(events=_codex_events())
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    settings = _settings("codex-write")
    with TestClient(create_app(settings, {"codex": adapter})) as c:
        r = c.post("/api/tasks", json={"instruction": "改文件", "engine": "codex",
                                       "writes_authorized": True},
                   headers=_headers())
        assert r.json()["writes_authorized"] is True
        _wait_terminal(c, r.json()["task_id"])
        cmd = spawn.calls[0]["cmd"]
        assert cmd[cmd.index("-s") + 1] == "workspace-write"


def test_codex_cli_missing_fails_run_only():
    adapter = CodexAdapter(cli="ld-no-such-cli")  # 默认 spawn → shutil.which 守卫
    fake = FakeEngine()
    settings = _settings("codex-missing")
    with TestClient(create_app(settings, {"codex": adapter, "fake": fake})) as c:
        rc = c.post("/api/tasks", json={"instruction": "x", "engine": "codex"},
                    headers=_headers())
        rf = c.post("/api/tasks", json={"instruction": "y", "engine": "fake"},
                    headers=_headers())
        tc = _wait_terminal(c, rc.json()["task_id"], want=("failed",))
        assert "缺席" in (tc["runs"][0]["error"] or "")
        assert _wait_terminal(c, rf.json()["task_id"])["status"] == "reported"


def test_codex_nonzero_exit_failed_with_stderr():
    spawn = FakeSpawn(events=[], rc=1, stderr="boom: 权限拒绝")
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    fake = FakeEngine()
    settings = _settings("codex-boom")
    with TestClient(create_app(settings, {"codex": adapter, "fake": fake})) as c:
        rc = c.post("/api/tasks", json={"instruction": "x", "engine": "codex"},
                    headers=_headers())
        rf = c.post("/api/tasks", json={"instruction": "y", "engine": "fake"},
                    headers=_headers())
        tc = _wait_terminal(c, rc.json()["task_id"], want=("failed",))
        err = tc["runs"][0]["error"] or ""
        assert "codex 退出码 1" in err and "boom: 权限拒绝" in err
        assert _wait_terminal(c, rf.json()["task_id"])["status"] == "reported"


def test_codex_cancel_kills_subprocess():
    spawn = FakeSpawn(events=_codex_events(), block_stdout=True)
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    settings = _settings("codex-cancel")
    with TestClient(create_app(settings, {"codex": adapter})) as c:
        r = c.post("/api/tasks", json={"instruction": "很慢的任务", "engine": "codex"},
                   headers=_headers())
        tid = r.json()["task_id"]
        cur = _wait_status(c, tid, "running")
        pr = c.post(f"/api/tasks/{tid}/cancel", headers=_headers())
        assert pr.status_code == 200, pr.text
        assert spawn.last_proc.killed is True
        assert c.get(f"/api/tasks/{tid}", headers=_headers()).json()["status"] == "cancelled"


def test_codex_blacklist_coarse_gate_waiting_approval():
    settings = _settings("codex-blacklist")
    bl = settings.blacklist_path
    bl.parent.mkdir(parents=True, exist_ok=True)
    bl.write_text(json.dumps([str(settings.ld_workspace_dir)]), encoding="utf-8")
    spawn = FakeSpawn(events=_codex_events())
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    with TestClient(create_app(settings, {"codex": adapter})) as c:
        r = c.post("/api/tasks", json={"instruction": "进黑名单目录", "engine": "codex"},
                   headers=_headers())
        tid = r.json()["task_id"]
        cur = _wait_status(c, tid, "waiting-approval")
        run = cur["runs"][0]
        assert run["state"] == "waiting-approval"
        assert run["wait_reason"] == "blacklist"
        assert run["pending_op"]["pending_op"]["kind"] == "task_workspace"
        assert spawn.calls == []  # 引擎从未启动
        kinds = [n["kind"] for n in c.get("/api/notifications",
                                           params={"task_id": tid},
                                           headers=_headers()).json()]
        assert "approval_requested" in kinds


def _gateway_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8788), timeout=2):
            return True
    except OSError:
        return False


def test_codex_real_cli_smoke_when_available():
    """真 CLI 冒烟（票 02 验收：引擎真机连通性可跳过集成测试）；缺席则 skip。"""
    if shutil.which("codex") is None:
        pytest.skip("codex CLI 缺席，跳过真机冒烟")
    if not _gateway_up():
        pytest.skip("mimo2codex 网关 127.0.0.1:8788 不可达，跳过真机冒烟")
    settings = _settings("codex-smoke")
    with TestClient(create_app(settings, {"codex": CodexAdapter(cli="codex")})) as c:
        r = c.post("/api/tasks", json={"instruction": "只回复一个词：OK", "engine": "codex"},
                   headers=_headers())
        t = _wait_terminal(c, r.json()["task_id"], timeout=120)
        assert t["status"] == "reported", t["status"]
        assert "OK" in (t["result"] or "")
        assert t["engine_thread_id"]

# ---------------------------------------------------------------------------
# REGRESSION_REL_WS_ABS: relative workspace -> absolute -C (codex os error 3 fix)
# ---------------------------------------------------------------------------

async def test_relative_workspace_yields_absolute_dash_c():
    """Relative task workspace must reach codex as an absolute -C: the child
    cwd is the workspace itself, so a relative -C would be re-resolved
    against that cwd and codex would fail with os error 3."""
    from app.adapters.base import RunHooks

    spawn = FakeSpawn(events=_codex_events())
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    ws_rel = os.path.join("tests", "tmp_workspace", "rel_ws_codex")
    os.makedirs(ws_rel, exist_ok=True)

    class _Dispatcher:
        async def thread_bound(self, task_id, thread_id):
            pass

        async def record_side_effect(self, run_id, kind, detail, path=None):
            pass

    task = {"task_id": "Trel", "workspace": ws_rel, "instruction": "hi",
            "engine_thread_id": None, "writes_authorized": False}
    run = {"run_id": "R1"}
    result = await adapter.execute(task, run, None, RunHooks(task, run, _Dispatcher()))
    assert result == "done codex"
    cmd = spawn.calls[0]["cmd"]
    expected_c = os.path.abspath(ws_rel)
    assert cmd[cmd.index("-C") + 1] == expected_c
    assert os.path.isabs(cmd[cmd.index("-C") + 1])
    assert spawn.calls[0]["cwd"] == expected_c

# ---------------------------------------------------------------------------
# TICKET_11: 长 JSON 事件行越 StreamReader 64 KiB 行长按制（回归，真实子进程管道）
# ---------------------------------------------------------------------------

async def test_codex_long_json_event_line_real_pipe():
    """128 KiB agent_message 事件经真实子进程管道（默认 64 KiB StreamReader limit）：
    修复前 readline() 抛 ValueError: Separator is not found, and chunk exceed the limit。"""
    from app.adapters.base import RunHooks

    program = (
        "import json, sys\n"
        "lines = [\n"
        "    json.dumps({'type': 'thread.started', 'thread_id': '01BIGLINE'}),\n"
        "    json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'x' * 131072}}),\n"
        "    json.dumps({'type': 'turn.completed'}),\n"
        "]\n"
        "sys.stdout.write('\\n'.join(lines) + '\\n')\n"
        "sys.stdout.flush()\n"
    )

    class _RealPipeSpawn:
        async def __call__(self, cmd, cwd):
            return await asyncio.create_subprocess_exec(
                sys.executable, "-c", program,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)

    adapter = CodexAdapter(cli="python", spawn=_RealPipeSpawn())
    ws = os.path.join("tests", "tmp_workspace", "ws_long_line_codex")
    os.makedirs(ws, exist_ok=True)

    class _Dispatcher:
        async def thread_bound(self, task_id, thread_id):
            pass

        async def record_side_effect(self, run_id, kind, detail, path=None):
            pass

    task = {"task_id": "Tlong", "workspace": ws, "instruction": "\u8f93\u51fa\u957f\u6587\u672c",
            "engine_thread_id": None, "writes_authorized": False}
    run = {"run_id": "Rlong"}
    result = await adapter.execute(task, run, None, RunHooks(task, run, _Dispatcher()))
    assert result == "x" * 131072

# ---------------------------------------------------------------------------
# TICKET_11: 异常路径（非取消）同样杀进程树，防孤儿 CLI 残留
# ---------------------------------------------------------------------------

async def test_codex_crash_path_kills_process_tree():
    """stdout.read 抛出未预期异常（模拟管道崩溃）：EngineError/crash 路径先终止
    子进程树再抛出；注入 spawn 时仅 proc.kill() 不 taskkill（_kill_tree injected 分支）。"""
    proc = FakeProc(events=[], rc=1)

    class _CrashRead:
        def __init__(self, inner):
            self._inner = inner

        async def read(self, n=-1):
            raise RuntimeError("pipe exploded")

    class _CrashSpawn:
        def __init__(self):
            self.calls = []

        async def __call__(self, cmd, cwd):
            self.calls.append((cmd, cwd))
            proc.stdout = _CrashRead(proc.stdout)
            return proc

    from app.adapters.base import RunHooks

    spawn = _CrashSpawn()
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    ws = os.path.join("tests", "tmp_workspace", "ws_crash_codex")
    os.makedirs(ws, exist_ok=True)

    class _Dispatcher:
        async def thread_bound(self, task_id, thread_id):
            pass

        async def record_side_effect(self, run_id, kind, detail, path=None):
            pass

    task = {"task_id": "Tcrash", "workspace": ws, "instruction": "x",
            "engine_thread_id": None, "writes_authorized": False}
    run = {"run_id": "Rcrash"}
    with pytest.raises(RuntimeError):
        await adapter.execute(task, run, None, RunHooks(task, run, _Dispatcher()))
    assert proc.killed is True


# ---------------------------------------------------------------------------
# TICKET_11B: 新任务 --ephemeral 避全局 thread-store writer 冲突（resume 不带）
# ---------------------------------------------------------------------------

async def test_codex_new_task_ephemeral_resume_persisted():
    """一次性后台任务（无 thread）带 --ephemeral：不写 ~/.codex 全局线程存储，不再与
    用户自己的 codex 会话/惑这冲突（thread-store conflict: active writer）；
    resume 路径必须读已持久化线程，不带 --ephemeral。"""
    from app.adapters.base import RunHooks

    spawn = FakeSpawn(events=_codex_events())
    adapter = CodexAdapter(cli="codex", spawn=spawn)
    ws = os.path.join("tests", "tmp_workspace", "ws_eph_codex")
    os.makedirs(ws, exist_ok=True)

    class _Dispatcher:
        async def thread_bound(self, task_id, thread_id):
            pass

        async def record_side_effect(self, run_id, kind, detail, path=None):
            pass

    hooks = RunHooks({"task_id": "Teph", "workspace": ws},
                     {"run_id": "Reph"}, _Dispatcher())
    task = {"task_id": "Teph", "workspace": ws, "instruction": "x",
            "engine_thread_id": None, "writes_authorized": False}
    run = {"run_id": "Reph"}
    await adapter.execute(task, run, None, hooks)
    task2 = dict(task)
    task2["engine_thread_id"] = "01TESTTHREAD"
    await adapter.execute(task2, run, "01TESTTHREAD", hooks)
    new_cmd = spawn.calls[0]["cmd"]
    resume_cmd = spawn.calls[1]["cmd"]
    assert "--ephemeral" in new_cmd
    assert "--ephemeral" not in resume_cmd
