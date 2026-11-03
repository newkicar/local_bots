"""Codex CLI 子进程适配器（ADR-0002：外部 CLI + JSON 事件流，继承 mimo2codex 网关）。

纪律（AGENTS.md）：
- 引擎唯一接口 = 本适配器；Dispatcher 永不直接起 codex；
- 绝不写 ~/.codex/config.toml；模型/Skills/网关由 CLI 自身配置继承；
- 沙箱默认 read-only，仅任务显式写授权或已批准操作切 workspace-write；
- 取消 = 终止子进程树（Windows 下 taskkill /T 兜底，仅默认 spawn 路径）；
- 重启核对永远"不确定"：子进程无法跨服务存活 → 不盲目重试（票 04 转 waiting-approval）。

prompt 经 stdin 注入（exec/resume 均不带位置参数）；--json 逐行事件流：
thread.started → hooks.thread_bound；item.completed 的 agent_message 为最终回复
（最后一条胜出），其他 item 类型入 Side-Effect Ledger；turn.failed/error → EngineError。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from contextlib import suppress
from typing import Any, Awaitable, Callable

from app.adapters.base import EngineAdapter, EngineError, RunHooks

logger = logging.getLogger(__name__)

# 鸭子类型 spawn：spawn(cmd: list[str], cwd: str) -> async proc
# （proc：stdout.readline() / stderr.read() / stdin.write()+drain()+close() / wait() / kill() / pid）
SpawnFn = Callable[[list[str], str], Awaitable[Any]]


class CodexAdapter(EngineAdapter):
    """codex exec 子进程（--json 事件流；resume 复用 session；stdin 注入 prompt）。"""

    name = "codex"

    def __init__(self, cli: str = "codex", *, spawn: SpawnFn | None = None) -> None:
        self._cli = cli
        self._spawn = spawn  # 注入后跳过 shutil.which 检查（可测试）

    # ---------- EngineAdapter ----------
    async def ensure_thread(self, task: dict[str, Any], run: dict[str, Any],
                            hooks: RunHooks) -> str | None:
        """Codex 的 session id 由 CLI 在执行中产生（thread.started 事件）；
        已有绑定则沿用；新任务返回 None，执行中经 hooks.thread_bound 上报。"""
        return task.get("engine_thread_id")

    async def execute(self, task: dict[str, Any], run: dict[str, Any], thread: str | None,
                      hooks: RunHooks, *, resume_note: str | None = None) -> str:
        ws = task.get("workspace")
        if not ws or not os.path.isdir(ws):
            raise EngineError(f"workspace 缺席或不是目录：{ws!r}")
        cmd = self._build_cmd(thread, ws, task, run)
        prompt = task.get("instruction") or ""
        if resume_note:
            prompt = f"{prompt}\n[恢复备注] {resume_note}" if prompt else f"[恢复备注] {resume_note}"
        proc = None
        try:
            proc = await self._start(cmd, ws)
            self._write_stdin_sync(proc, prompt)
            await self._drain_stdin(proc)
            final_text: str | None = None
            turn_fail: str | None = None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    continue  # 非 JSON 行（诊断输出）跳过
                etype = event.get("type")
                if etype == "thread.started":
                    tid = event.get("thread_id") or event.get("id")
                    if tid:
                        await hooks.thread_bound(str(tid))
                elif etype == "item.completed":
                    item = event.get("item") or {}
                    itype = item.get("type")
                    if itype == "agent_message":
                        final_text = item.get("text") or ""
                    elif itype in ("reasoning", "error", "function_call_output"):
                        continue
                    else:
                        await hooks.side_effect(
                            itype or "unknown",
                            json.dumps(item, ensure_ascii=False)[:500],
                            item.get("path") or item.get("file_path"),
                        )
                elif etype in ("turn.failed", "error"):
                    turn_fail = str(event.get("error") or event.get("message") or text[:300])
            rc = await proc.wait()
            err_tail = (await self._read_stderr(proc))[-500:].strip()
            if turn_fail:
                raise EngineError(
                    f"codex 轮次失败：{turn_fail}"
                    + (f"（stderr 尾部 {err_tail}）" if err_tail else "")
                )
            if rc != 0:
                raise EngineError(f"codex 退出码 {rc}（{err_tail or '无 stderr'}）")
            return final_text if final_text else "（codex 未产出最终回复）"
        except asyncio.CancelledError:
            # 取消/关停：终止子进程树后原样抛出（Dispatcher 落 cancelled/interrupted）
            self._kill_tree(proc, injected=self._spawn is not None)
            raise

    async def reconcile_running(self, task: dict[str, Any], run: dict[str, Any],
                                thread: str | None) -> dict[str, Any]:
        return {
            "safe": False,
            "result": None,
            "detail": "codex exec 子进程无法跨服务重启存活，最终状态未知；需人工核实（不盲目重试）",
        }

    # ---------- 内部 ----------
    def _build_cmd(self, thread: str | None, ws: str, task: dict[str, Any],
                   run: dict[str, Any]) -> list[str]:
        if thread:
            # resume 继承会话沙箱（不带 -C/-s）
            return [self._cli, "exec", "resume", thread, "--json", "--skip-git-repo-check"]
        return [self._cli, "exec", "-C", ws, "-s", self._sandbox(task, run),
                "--json", "--skip-git-repo-check"]

    @staticmethod
    def _sandbox(task: dict[str, Any], run: dict[str, Any]) -> str:
        if task.get("writes_authorized"):
            return "workspace-write"
        if run.get("decision") == "approved" and run.get("pending_op"):
            return "workspace-write"
        return "read-only"

    async def _start(self, cmd: list[str], ws: str) -> Any:
        if self._spawn is not None:
            return await self._spawn(cmd, ws)
        resolved = shutil.which(self._cli)
        if resolved is None:
            raise EngineError(f"codex CLI 缺席：{self._cli!r}（shutil.which 未命中）")
        # npm 安装的 CLI 常是 .CMD shim：Windows CreateProcess 不能直接跑它，需经 cmd /c
        argv: list[str] = [resolved, *cmd[1:]]
        if resolved.lower().endswith((".cmd", ".bat", ".batch")):
            argv = [os.environ.get("ComSpec") or "cmd.exe", "/c", resolved, *cmd[1:]]
        kwargs: dict[str, Any] = {
            "cwd": ws,
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        cfn = getattr(subprocess, "CREATE_NO_WINDOW", None)
        if cfn:
            kwargs["creationflags"] = cfn
        return await asyncio.create_subprocess_exec(*argv, **kwargs)

    @staticmethod
    def _write_stdin_sync(proc: Any, prompt: str) -> None:
        if proc.stdin is not None:
            proc.stdin.write(prompt.encode("utf-8"))

    @staticmethod
    async def _drain_stdin(proc: Any) -> None:
        if proc.stdin is None:
            return
        await proc.stdin.drain()
        closed = proc.stdin.close()  # 真实 asyncio 管道 close() 同步；fake 可能 async——两者兼容
        if asyncio.iscoroutine(closed):
            await closed

    @staticmethod
    async def _read_stderr(proc: Any) -> str:
        if proc.stderr is None:
            return ""
        raw = await proc.stderr.read()
        return raw.decode("utf-8", errors="replace") if raw else ""

    @staticmethod
    def _kill_tree(proc: Any, *, injected: bool) -> None:
        if proc is None:
            return
        with suppress(ProcessLookupError):
            proc.kill()
        if injected:
            return  # 测试注入的 fake proc 无真实 PID，不 taskkill
        pid = getattr(proc, "pid", None)
        if not pid:
            return
        kw: dict[str, Any] = {"capture_output": True, "timeout": 5}
        cfn = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if cfn:
            kw["creationflags"] = cfn
        with suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], **kw)