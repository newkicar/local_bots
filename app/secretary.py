"""票 06：Secretary——自然语言派活、只读文件发现、任务工具接线。

纪律（方案文档第七节 / ADR-0001 / 票 06 验收）：
- 秘书是独立 DeepAgents 主代理，模型独立配置（ld_secretary_*，默认 mimo2codex 回环网关）；
- 引擎路由：缺省 jarvis；仅用户明说"用 Codex"才 codex（无 LLM 路由、无公司数据自动判定）；
- 文件发现：仅授权目录内（ld_secretary_authorized_dirs）、只读、带超时与结果数量上限，
  绝不扫全盘；权限由服务端 + Windows 账户强制，不靠 prompt（prompt 注入不能扩权）；
- 任务六件工具：submit_task / start_agent_session / list_tasks / get_task /
  followup_task / cancel_task——委派后本轮即结束，不等待引擎完成（"假异步"根治）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from app.dispatcher import Dispatcher
from app.settings import Settings
from app.task_manager import InvalidTransition, TaskManager

#: 发现类操作默认上限（构造函数 caps 可覆盖，测试用极小值）；超时/截断都返回 JSON，绝不失控。
DEFAULT_CAPS: dict[str, Any] = {
    "timeout_s": 30.0,               # 单次发现操作整体超时
    "list_dir_max": 500,            # list_dir 条目上限
    "glob_max_files": 200,          # find_files 结果上限
    "grep_max_matches": 50,         # grep_files 命中上限
    "grep_max_files_scanned": 2000, # grep_files 扫描文件数上限
    "read_max_chars": 20000,        # peek_file 读取字符上限
    "list_tasks_max": 50,           # list_tasks 结果上限
    "delay_s": 0.0,                 # 压测旋钮：>0 时每个发现操作先行睡够该时长（仅用于验证超时路径）
}

_GREP_MAX_FILE_BYTES = 1_000_000    # grep 跳过大文件（避免慢速全盘读取）
_HISTORY_MAX = 40                  # chat 携带的对话历史上限

SYSTEM_PROMPT = """你是"Local Dots"的秘书：帮用户用自然语言派发后台任务、追问并汇总结果。严格遵守：
1. 派活：一次性任务用 submit_task；长期会话用 start_agent_session（后续用 followup_task 在同一任务内追问）。
   引擎路由：缺省一律 jarvis；仅当用户明说"用 Codex / 交给 Codex"才传 engine="codex"。不要自行猜引擎。
2. 委派后本轮立即结束：不等待任务完成；结果后续用 get_task / list_tasks 查询汇总。
3. 文件发现（只读）：用户提到某项目/目录/文件时，先用 list_dir / find_files / grep_files / peek_file
   在本提示词末列出的授权目录列表下定位（不要猜测路径），再把任务派到定位到的目录（workspace 参数）。
   你没有任何写文件/执行命令的权限；不要调用 write_file / edit_file / execute 之类的工具。
4. 追问：用户问此前某个任务时，用 followup_task 在原任务 Thread 内追问（不混其他上下文）；取消用 cancel_task。
5. 汇总结果：用 get_task（含 runs 历史、通知与副作用台账）；若某 Run 处于 waiting-approval，
   告知用户待审批，并说明审批来源（黑名单或重启核对）。
6. 用简洁中文回复；涉及任务时附 task_id。
"""


def _jerr(msg: str) -> str:
    """统一 JSON 错误体：工具永不向 LLM 抛异常，错误以可读 JSON 文本返回。"""
    return json.dumps({"error": msg}, ensure_ascii=False)


def _safe_json(content: Any) -> Any:
    try:
        return json.loads(content)
    except Exception:
        return None


def _text_of(content: Any) -> str:
    """从 AIMessage.content 提取展示文本（str 或 block 列表两种形态）。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts).strip()
    return str(content).strip() if content else ""


class Secretary:
    """秘书主代理：意图路由 + 只读文件发现 + 任务工具接线。

    - model 可注入（测试用脚本化假模型）；缺省时按 Settings 惰性构建真实模型（mimo2codex 网关）；
    - 发现工具：服务端授权目录强校验 + 只读 + asyncio.timeout 超时 + 结果上限；
    - 任务工具：包 TaskManager/Dispatcher，KeyError/InvalidTransition 一律转 JSON 错误体。
    """

    def __init__(
        self,
        tm: TaskManager,
        dispatcher: Dispatcher,
        settings: Settings,
        *,
        model: BaseChatModel | None = None,
        caps: dict[str, Any] | None = None,
    ) -> None:
        self._tm = tm
        self._dispatcher = dispatcher
        self._settings = settings
        self._caps = {**DEFAULT_CAPS, **(caps or {})}
        self._model = model
        self._agent = None
        self._engine_names: set[str] = set(getattr(dispatcher, "_engines", {}))
        # 授权目录根（normcase + resolve；Windows 大小写不敏感的包含判定）
        self._roots: list[Path] = [
            Path(os.path.normcase(str(p))).resolve() for p in settings.authorized_dirs
        ]
        self._roots_display: list[Path] = [
            Path(str(p)).resolve() for p in settings.authorized_dirs
        ]

    # ---------- 对话入口 ----------

    async def chat(
        self, message: str, history: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        """自然语言消息 → {"reply", "task_ids"}；本次调用不等待任务完成。"""
        msgs: list[BaseMessage] = []
        for h in (history or [])[-_HISTORY_MAX:]:
            role = str(h.get("role", ""))
            content = str(h.get("content", ""))
            if role == "user" and content:
                msgs.append(HumanMessage(content=content))
            elif role == "assistant" and content:
                msgs.append(AIMessage(content=content))
        msgs.append(HumanMessage(content=str(message)))
        result = await self._get_agent().ainvoke({"messages": msgs})
        reply = ""
        task_ids: list[str] = []
        for m in result.get("messages", []):
            if isinstance(m, ToolMessage) and m.name in ("submit_task", "start_agent_session"):
                d = _safe_json(m.content)
                if isinstance(d, dict) and d.get("task_id") and not d.get("error"):
                    task_ids.append(str(d["task_id"]))
            elif isinstance(m, AIMessage) and not m.tool_calls:
                txt = _text_of(m.content)
                if txt:
                    reply = txt
        return {"reply": reply, "task_ids": task_ids}

    def _get_agent(self):
        """惰性构建 DeepAgents 主代理（一次）；注入模型优先，否则按 Settings 取。"""
        if self._agent is None:
            from deepagents import create_deep_agent

            model = self._model if self._model is not None else self._default_model()
            self._agent = create_deep_agent(
                model=model,
                tools=self._build_tools(),
                system_prompt=self.system_prompt(),
            )
        return self._agent

    def system_prompt(self) -> str:
        """本实例的完整系统提示词：基础纪律 + 授权目录列表（发现工具入口；服务端仍强校验）。"""
        return SYSTEM_PROMPT + self._authorized_roots_note()

    def _authorized_roots_note(self) -> str:
        lines = [f"- {p}" for p in self._roots_display] or ["-（未配置授权目录：只读发现不可用）"]
        return (
            "\n授权目录（只读；文件发现只能在这些目录下进行，"
            "list_dir / find_files / grep_files / peek_file 的路径必须是它们的绝对路径）：\n"
            + "\n".join(lines)
        )

    def _default_model(self) -> BaseChatModel:
        """秘书模型（OpenAI 兼容网关；默认 Settings 指向 mimo2codex 回环，不出回环）。"""
        from langchain.chat_models import init_chat_model

        s = self._settings
        return init_chat_model(
            f"openai:{s.ld_secretary_model}",
            base_url=s.ld_secretary_base_url,
            api_key=s.ld_secretary_api_key,
        )

    # ---------- 任务工具内核 ----------

    async def _submit(
        self,
        instruction: str,
        engine: str | None,
        workspace: str | None,
        writes_authorized: bool,
        *,
        session: bool,
    ) -> str:
        eng = (engine or "jarvis").strip().lower()
        if eng not in self._engine_names:
            return _jerr(f"未知引擎: {engine!r}；可用: {sorted(self._engine_names)}")
        if workspace is not None and not os.path.isdir(os.path.normcase(str(workspace))):
            return _jerr(f"workspace 不存在或不是目录: {workspace}")
        try:
            task = await self._tm.create_task(
                instruction=instruction,
                engine=eng,
                workspace=workspace,
                writes_authorized=writes_authorized,
            )
        except Exception as exc:  # 工具永不向 LLM 抛异常
            return _jerr(f"任务创建失败: {exc}")
        if task["workspace"] is None:
            auto = self._settings.ld_workspace_dir / task["task_id"]
            auto.mkdir(parents=True, exist_ok=True)
            await self._tm.set_task_workspace(task["task_id"], str(auto))
            task["workspace"] = str(auto)
        hint = (
            "会话已建立；后续用 followup_task(task_id) 在同一 Thread 追问；本轮不等待执行完成。"
            if session
            else "已进入队列；本轮不等待完成；稍后用 get_task(task_id) 查看结果。"
        )
        return json.dumps(
            {"task_id": task["task_id"], "status": task["status"], "engine": eng,
             "workspace": task["workspace"], "session": session, "hint": hint},
            ensure_ascii=False,
        )

    # ---------- 发现工具内核（只读 + 授权目录 + 上限；超时由 _with_timeout 包裹） ----------

    def _authorized_path(self, p: str) -> tuple[Path | None, str | None]:
        """服务端授权校验：仅允许授权目录内路径；返回 (路径, None) 或 (None, 错误 JSON)。"""
        try:
            target = Path(os.path.normcase(str(p))).resolve()
        except (OSError, RuntimeError, ValueError):
            return None, _jerr(f"无效路径: {p}")
        for root in self._roots:
            try:
                target.relative_to(root)
                return target, None
            except ValueError:
                continue
        return None, _jerr(f"路径超出授权目录: {p}（只能在授权目录内做只读发现）")

    async def _with_timeout(self, core: Callable, *args: Any) -> str:
        """同步内核放线程池跑，asyncio.timeout 整体封顶；超时返回 JSON 提示（后台线程自行结束）。"""
        try:
            async with asyncio.timeout(self._caps["timeout_s"]):
                return await asyncio.to_thread(core, *args)
        except TimeoutError:
            return _jerr(
                f"操作超时（> {self._caps['timeout_s']}s）；请缩小搜索范围或调低结果上限后重试"
            )

    def _list_dir_core(self, path: str) -> str:
        time.sleep(self._caps.get("delay_s") or 0.0)
        target, err = self._authorized_path(path)
        if err:
            return err
        if not target.is_dir():
            return _jerr(f"不是目录: {path}")
        cap = int(self._caps["list_dir_max"])
        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            for i, e in enumerate(sorted(target.iterdir(), key=lambda x: x.name)):
                if i >= cap:
                    truncated = True
                    break
                is_file = e.is_file()
                entries.append(
                    {"name": e.name, "is_dir": not is_file,
                     "size": (e.stat().st_size if is_file else None)}
                )
        except OSError as exc:
            return _jerr(f"无法读取目录: {exc}")
        out: dict[str, Any] = {"path": str(target), "entries": entries}
        if truncated:
            out["note"] = f"截断：仅显示前 {cap} 项"
        return json.dumps(out, ensure_ascii=False)

    def _find_files_core(self, pattern: str, root: str) -> str:
        time.sleep(self._caps.get("delay_s") or 0.0)
        target, err = self._authorized_path(root)
        if err:
            return err
        g = pattern if "**" in pattern else f"**/{pattern}"
        cap = int(self._caps["glob_max_files"])
        hits: list[dict[str, Any]] = []
        truncated = False
        try:
            n = 0
            for f in sorted(target.glob(g)):
                if f.is_file():
                    if n >= cap:
                        truncated = True
                        break
                    hits.append({"path": str(f), "size": f.stat().st_size})
                    n += 1
        except (OSError, ValueError) as exc:
            return _jerr(f"搜索失败: {exc}")
        out: dict[str, Any] = {"pattern": pattern, "root": str(target), "files": hits}
        if truncated:
            out["note"] = f"截断：仅显示前 {cap} 个文件"
        return json.dumps(out, ensure_ascii=False)

    def _grep_files_core(self, pattern: str, root: str) -> str:
        time.sleep(self._caps.get("delay_s") or 0.0)
        target, err = self._authorized_path(root)
        if err:
            return err
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return _jerr(f"无效正则: {exc}")
        cap_m = int(self._caps["grep_max_matches"])
        cap_f = int(self._caps["grep_max_files_scanned"])
        matches: list[dict[str, Any]] = []
        scanned, trunc_m, trunc_f = 0, False, False
        for dirpath, _dirnames, filenames in os.walk(target):
            if trunc_m and trunc_f:
                break
            for fn in filenames:
                if trunc_m or trunc_f:
                    break
                fp = Path(dirpath) / fn
                try:
                    st = fp.stat()
                except OSError:
                    continue
                scanned += 1
                if scanned > cap_f:
                    trunc_f = True
                    break
                if st.st_size > _GREP_MAX_FILE_BYTES:
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                        for ln, line in enumerate(fh, 1):
                            if rx.search(line):
                                matches.append(
                                    {"path": str(fp), "line": ln, "text": line.strip()[:300]}
                                )
                                if len(matches) >= cap_m:
                                    trunc_m = True
                                    break
                except OSError:
                    continue
        out: dict[str, Any] = {
            "pattern": pattern, "root": str(target),
            "files_scanned": scanned, "matches": matches,
        }
        notes: list[str] = []
        if trunc_m:
            notes.append(f"仅显示前 {cap_m} 条命中")
        if trunc_f:
            notes.append(f"仅扫描前 {cap_f} 个文件")
        if notes:
            out["note"] = "截断：" + "；".join(notes)
        return json.dumps(out, ensure_ascii=False)

    def _peek_file_core(self, path: str, max_chars: int | None) -> str:
        time.sleep(self._caps.get("delay_s") or 0.0)
        target, err = self._authorized_path(path)
        if err:
            return err
        if not target.is_file():
            return _jerr(f"文件不存在: {path}")
        cap = int(self._caps["read_max_chars"])
        limit = min(int(max_chars), cap) if max_chars else cap
        try:
            with target.open("rb") as fh:
                raw = fh.read(limit + 1)
        except OSError as exc:
            return _jerr(f"无法读取文件: {exc}")
        if b"\x00" in raw[:8192]:
            return _jerr(f"二进制文件（不读取）: {path}")
        truncated = len(raw) > limit
        text = raw[:limit].decode("utf-8", errors="replace")
        return json.dumps(
            {"path": str(target), "chars": len(text), "truncated": truncated, "text": text},
            ensure_ascii=False,
        )

    # ---------- 工具工厂（接 DeepAgents 主代理） ----------

    def _build_tools(self) -> list:
        """十件工具：任务六件 + 只读发现四件；全部返回 JSON 字符串，永不向 LLM 抛异常。"""

        @tool
        async def submit_task(
            instruction: str,
            engine: str | None = None,
            workspace: str | None = None,
            writes_authorized: bool = False,
        ) -> str:
            """创建一次性后台任务并立即返回（不等待完成）。
            instruction：任务指令（自然语言）。engine："jarvis"（缺省）或 "codex"（仅当用户明说用 Codex）。
            workspace：任务工作目录（必须是已存在的目录；省略则自动建隔离目录）。
            writes_authorized：任务是否被授权写文件（默认 False，保守）。
            """
            return await self._submit(instruction, engine, workspace, writes_authorized, session=False)

        @tool
        async def start_agent_session(
            instruction: str,
            engine: str | None = None,
            workspace: str | None = None,
            writes_authorized: bool = False,
        ) -> str:
            """开启一个长期 Agent 会话（绑定引擎 + 工作目录；后续追问复用同一 Thread）。
            当用户希望就某事持续追问引擎时使用；之后用 followup_task(task_id) 在同 Thread 内追问。
            参数与 submit_task 相同。
            """
            return await self._submit(instruction, engine, workspace, writes_authorized, session=True)

        @tool
        async def list_tasks(status: str | None = None, limit: int | None = None) -> str:
            """列出后台任务及状态（queued/running/completed/failed/...）。
            status：可选过滤；limit：最多条数（服务端有上限）。"""
            cap = int(self._caps["list_tasks_max"])
            n = cap
            if limit:
                try:
                    n = min(int(limit), cap)
                except (TypeError, ValueError):
                    n = cap
            rows = await self._tm.list_tasks()
            if status:
                rows = [r for r in rows if r["status"] == status]
            compact = [
                {"task_id": r["task_id"], "status": r["status"], "engine": r["engine"],
                 "instruction": r["instruction"][:80], "updated_at": r["updated_at"]}
                for r in rows[:n]
            ]
            return json.dumps(
                {"tasks": compact, "shown": len(compact), "total": len(rows)},
                ensure_ascii=False,
            )

        @tool
        async def get_task(task_id: str) -> str:
            """查看单个任务详情：状态、结果、错误、runs 历史、通知（含待审批与副作用台账）。"""
            t = await self._tm.get_task(task_id)
            if t is None:
                return _jerr(f"任务不存在: {task_id}")
            t["runs"] = await self._tm.get_runs(task_id)
            t["notifications"] = await self._tm.get_notifications(task_id)
            return json.dumps(t, ensure_ascii=False, default=str)

        @tool
        async def followup_task(task_id: str, instruction: str) -> str:
            """对原任务追问（任务须已终态）：复用同一 Thread 与上下文，不混其他任务。
            若任务仍在进行中会返回错误；先 get_task 查看状态。"""
            try:
                t = await self._tm.followup_task(task_id, instruction)
            except KeyError:
                return _jerr(f"任务不存在: {task_id}")
            except InvalidTransition as exc:
                return _jerr(str(exc))
            return json.dumps(
                {"task_id": t["task_id"], "status": t["status"],
                 "engine_thread_id": t.get("engine_thread_id"),
                 "hint": "追问已入队；将复用原任务 Thread（不混其他上下文）"},
                ensure_ascii=False,
            )

        @tool
        async def cancel_task(task_id: str) -> str:
            """取消整个任务（在飞/排队 Run 停下，返回结局台账）；终态任务不可取消。"""
            try:
                ledger = await self._dispatcher.cancel_task(task_id)
            except KeyError:
                return _jerr(f"任务不存在: {task_id}")
            except InvalidTransition as exc:
                return _jerr(str(exc))
            return json.dumps(
                {"task_id": task_id, "status": "cancelled", "ledger": ledger},
                ensure_ascii=False, default=str,
            )

        @tool
        async def list_dir(path: str = ".") -> str:
            """列出目录直接子项（仅限授权目录内；只读；条目数有上限）。"""
            return await self._with_timeout(self._list_dir_core, path)

        @tool
        async def find_files(pattern: str, root: str = ".") -> str:
            """在授权目录内按 glob 递归查找文件（如 "*.md"；用 "**" 跨级）。结果有上限。"""
            return await self._with_timeout(self._find_files_core, pattern, root)

        @tool
        async def grep_files(pattern: str, root: str = ".") -> str:
            """在授权目录内按正则（忽略大小写）搜文本文件内容；跳过 >1MB 文件。命中与扫描文件数均有上限。"""
            return await self._with_timeout(self._grep_files_core, pattern, root)

        @tool
        async def peek_file(path: str, max_chars: int | None = None) -> str:
            """读取授权目录内文本文件（只读；按上限截断；二进制不读）。"""
            return await self._with_timeout(self._peek_file_core, path, max_chars)

        return [
            submit_task, start_agent_session, list_tasks, get_task,
            followup_task, cancel_task, list_dir, find_files, grep_files, peek_file,
        ]