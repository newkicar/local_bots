"""Local Dots V1.0 服务入口（FastAPI，仅监听 127.0.0.1）。

票 01 骨架：Bearer Token 认证 + 任务 API + SSE 状态推送。
票 02 起挂载 codex 真实引擎（默认 sandbox read-only + 粗粒度黑名单兜底）；票 05 挂载 jarvis（缺省引擎，ADR-0001）。
票 07 挂载 web/ 原生 Web UI（StaticFiles 于 /，API 鉴权不变）。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel

from app.settings import Settings, get_settings
from app.adapters.base import EngineAdapter
from app.adapters.codex import CodexAdapter
from app.adapters.jarvis import JarvisAdapter
from app.approval import ApprovalGate
from app.dispatcher import Dispatcher
from app.permissions import Blacklist, coarse_task_policy
from app.reconcile import reconcile_on_startup
from app.secretary import Secretary
from app.task_manager import TASK_TERMINAL_STATES, InvalidTransition, TaskManager


class SSEHub:
    """把 TaskManager 通知事件广播给所有 SSE 订阅者（只推状态迁移，不逐字流式）。"""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    async def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)


class SubmitTask(BaseModel):
    instruction: str
    engine: str | None = None
    workspace: str | None = None
    parent_task_id: str | None = None
    writes_authorized: bool = False


class FollowupRequest(BaseModel):
    instruction: str


class RejectApproval(BaseModel):
    reason: str


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, str]] = []


def _default_engines(settings: Settings) -> dict[str, EngineAdapter]:
    """票 02 挂 codex（外部 CLI，经 mimo2codex 网关）；票 05 在此加 jarvis。测试注入假引擎。"""
    return {
        "codex": CodexAdapter(cli=settings.ld_codex_cli),
        "jarvis": JarvisAdapter(settings.checkpoints_db_path),
    }


def create_app(
    settings: Settings | None = None,
    engines: dict[str, EngineAdapter] | None = None,
    secretary: Secretary | None = None,
    secretary_factory: Callable[[TaskManager, Dispatcher, Settings], Secretary] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.ld_data_dir.mkdir(parents=True, exist_ok=True)
    settings.ld_workspace_dir.mkdir(parents=True, exist_ok=True)
    # 粗粒度黑名单文件缺省初始化（[]）；运行期追加即时生效（permissions.Blacklist 每次重读）
    bl_path = settings.blacklist_path
    bl_path.parent.mkdir(parents=True, exist_ok=True)
    if not bl_path.exists():
        bl_path.write_text("[]", encoding="utf-8")

    hub = SSEHub()
    tm = TaskManager(settings.tasks_db_path, notify=hub.publish)
    bl = Blacklist(settings.blacklist_path)
    resolved_engines = _default_engines(settings) if engines is None else engines
    dispatcher = Dispatcher(
        tm,
        resolved_engines,
        max_concurrent=settings.ld_max_concurrent_runs,
        policy=coarse_task_policy(bl),
    )
    gate = ApprovalGate(
        tm, dispatcher, bl, timeout_min=settings.ld_approval_timeout_min
    )
    sec = secretary
    if sec is None:
        sec = (
            secretary_factory(tm, dispatcher, settings)
            if secretary_factory is not None
            else Secretary(tm, dispatcher, settings)
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await tm.open()
        # 重启核对（票 04）：以持久化状态为输入，恢复或转等待核实，绝不盲目重试
        await reconcile_on_startup(tm, resolved_engines, dispatcher)
        dispatcher.start()
        gate.start()
        try:
            yield
        finally:
            await gate.stop()
            await dispatcher.stop()
            await tm.close()

    app = FastAPI(title="Local Dots V1.0", lifespan=lifespan)
    app.state.hub = hub
    app.state.tm = tm
    app.state.dispatcher = dispatcher
    app.state.gate = gate
    app.state.secretary = sec

    async def _auth(request: Request) -> None:
        header = request.headers.get("authorization", "")
        got = (
            header[7:].strip()
            if header.lower().startswith("bearer ")
            else request.query_params.get("token", "")
        )
        if got != settings.ld_api_token:
            raise HTTPException(status_code=401, detail="missing or invalid token")

    auth = Depends(_auth)

    @app.post("/api/tasks", status_code=201, dependencies=[auth])
    async def submit_task(body: SubmitTask) -> dict[str, Any]:
        task = await tm.create_task(
            instruction=body.instruction,
            engine=body.engine or "jarvis",
            workspace=body.workspace,
            parent_task_id=body.parent_task_id,
            writes_authorized=body.writes_authorized,
        )
        if task["workspace"] is None:
            ws = settings.ld_workspace_dir / task["task_id"]
            ws.mkdir(parents=True, exist_ok=True)
            await tm.set_task_workspace(task["task_id"], str(ws))
            task["workspace"] = str(ws)
        return task

    @app.get("/api/tasks", dependencies=[auth])
    async def list_tasks() -> list[dict[str, Any]]:
        return await tm.list_tasks()

    @app.get("/api/tasks/{task_id}", dependencies=[auth])
    async def get_task(task_id: str) -> dict[str, Any]:
        task = await tm.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] == "completed" and await tm.transition_task(task_id, "reported"):
            task["status"] = "reported"
        return task

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[auth])
    async def cancel_task(task_id: str) -> dict[str, Any]:
        if await tm.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")
        try:
            ledger = await dispatcher.cancel_task(task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="task not found")
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"task_id": task_id, "status": "cancelled", "ledger": ledger}

    @app.post("/api/tasks/{task_id}/followup", dependencies=[auth])
    async def followup_task(task_id: str, body: FollowupRequest) -> dict[str, Any]:
        if await tm.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")
        try:
            return await tm.followup_task(task_id, body.instruction)
        except KeyError:
            raise HTTPException(status_code=404, detail="task not found")
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/api/runs/{run_id}", dependencies=[auth])
    async def get_run(run_id: str) -> dict[str, Any]:
        run = await tm.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @app.get("/api/approvals", dependencies=[auth])
    async def list_approvals() -> list[dict[str, Any]]:
        return await gate.list_pending()

    @app.post("/api/approvals/{run_id}/approve", dependencies=[auth])
    async def approve_run(run_id: str) -> dict[str, Any]:
        try:
            return await gate.approve(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found")
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/approvals/{run_id}/reject", dependencies=[auth])
    async def reject_run(run_id: str, body: RejectApproval) -> dict[str, Any]:
        try:
            return await gate.reject(run_id, body.reason)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found")
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/chat", dependencies=[auth])
    async def chat(body: ChatRequest) -> dict[str, Any]:
        """秘书聊天（票 06）：自然语言派活与任务汇总；委托后立即返回，不等待引擎。"""
        return await sec.chat(body.message, body.history or None)

    @app.get("/api/notifications", dependencies=[auth])
    async def notifications(task_id: str = Query(...)) -> list[dict[str, Any]]:
        if await tm.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")
        return await tm.get_notifications(task_id)

    @app.get("/api/health", dependencies=[auth])
    async def health() -> dict[str, Any]:
        return {
            "queued": await tm.queued_count(),
            "active": len(await tm.active_task_ids()),
        }

    @app.get("/api/events", dependencies=[auth])
    async def events() -> StreamingResponse:
        q = hub.subscribe()

        async def gen():
            yield "retry: 3000\n\n"
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 票 07：原生 Web UI（无框架）。静态页仅承载登录与 API 调用，
    # 自身不含密钥；鉴权一律在 /api（Bearer Token，SSE 走 ?token= 查询参数）。
    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


app = create_app()