"""Task 持久化与状态机（SQLite，aiosqlite）。

三层分离（CONTEXT.md 词表）：
- task_id：业务任务（tasks 表）
- engine_thread_id：引擎会话（引擎建 Thread 后立即写库，不等任务结束）
- run_id：Thread 内一次执行（runs 表）；重试/追问产生新 Run，不覆盖旧 Run

状态机（方案文档第五节 + ADR-0005）：
Task: queued -> running -> completed -> reported
分支: waiting-approval / failed / cancelled / rejected(reason=...) / interrupted
waiting-approval 进入来源：命中 Blacklist、重启核对无法确认副作用。

终态任务可经 followup（新 Run）重新进入 queued；旧 Run 行永久保留。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aiosqlite

TASK_TERMINAL_STATES = {"reported", "failed", "cancelled", "rejected"}
TASK_ACTIVE_STATES = {"queued", "running", "waiting-approval", "interrupted"}

TASK_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelled", "failed", "waiting-approval", "interrupted"},
    "running": {"completed", "failed", "cancelled", "rejected", "interrupted", "waiting-approval"},
    "waiting-approval": {"running", "rejected", "cancelled"},
    "completed": {"reported", "queued"},
    "reported": {"queued"},
    "failed": {"queued"},
    "cancelled": {"queued"},
    "rejected": {"queued"},
    "interrupted": {"running", "waiting-approval", "queued", "cancelled"},
}

RUN_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelled", "waiting-approval", "failed", "interrupted"},
    "running": {"completed", "failed", "cancelled", "rejected", "interrupted", "waiting-approval"},
    "waiting-approval": {"running", "rejected", "cancelled", "interrupted"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
    "rejected": set(),
    "interrupted": set(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
    task_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    engine TEXT NOT NULL DEFAULT 'jarvis',
    engine_thread_id TEXT,
    workspace TEXT,
    writes_authorized INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    parent_task_id TEXT,
    instruction TEXT,
    result TEXT,
    error TEXT,
    reject_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'submitted',
    queue_position INTEGER,
    started_at TEXT,
    finished_at TEXT,
    result TEXT,
    error TEXT,
    side_effects TEXT NOT NULL DEFAULT '[]',
    pending_op TEXT,
    wait_reason TEXT,
    wait_requested_at TEXT,
    decision TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, attempt);
CREATE TABLE IF NOT EXISTS notifications(
    notification_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_task ON notifications(task_id, created_at);
"""

NotifyFn = Callable[[dict[str, Any]], Awaitable[None]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InvalidTransition(Exception):
    """非法状态迁移（调用方按业务语义决定 409 或忽略）。"""


class TaskManager:
    def __init__(self, db_path, notify: NotifyFn | None = None, *,
                 clock: Callable[[], str] | None = None) -> None:
        self._db_path = str(db_path)
        self._notify = notify
        self._clock = clock or _now
        self._db: aiosqlite.Connection | None = None

    # ---------- 生命周期 ----------
    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        # 幂等迁移（票 02）：旧库补齐 writes_authorized 列
        async with self.db.execute("PRAGMA table_info(tasks)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "writes_authorized" not in cols:
            await self.db.execute(
                "ALTER TABLE tasks ADD COLUMN writes_authorized INTEGER NOT NULL DEFAULT 0"
            )
            await self.db.commit()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("TaskManager 尚未 open()")
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _notify_event(self, event: dict[str, Any]) -> None:
        if self._notify is not None:
            await self._notify(event)

    # ---------- Task ----------
    async def create_task(
        self,
        *,
        instruction: str,
        engine: str = "jarvis",
        workspace: str | None = None,
        parent_task_id: str | None = None,
        writes_authorized: bool = False,
    ) -> dict[str, Any]:
        task_id = "T" + uuid.uuid4().hex[:12]
        ts = _now()
        run_id = "R" + uuid.uuid4().hex[:12]
        async with self.db.execute(
            "INSERT INTO tasks(task_id, seq, engine, workspace, writes_authorized, status, "
            "parent_task_id, instruction, created_at, updated_at) "
            "VALUES(?, (SELECT COALESCE(MAX(seq),0)+1 FROM tasks), ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (task_id, engine, workspace, 1 if writes_authorized else 0,
             parent_task_id, instruction, ts, ts),
        ):
            pass
        await self._insert_run(run_id, task_id, 1, "queued", origin="submitted")
        await self.db.commit()
        await self._notify_event({"type": "task_state", "task_id": task_id, "from": None, "to": "queued"})
        task = await self.get_task(task_id)
        assert task is not None
        return task

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT task_id, seq, engine, engine_thread_id, workspace, writes_authorized, status, "
            "parent_task_id, instruction, result, error, reject_reason, created_at, updated_at "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        task = self._task_row(row)
        task["runs"] = await self.get_runs(task_id)
        task["notifications"] = await self.get_notifications(task_id)
        return task

    async def list_tasks(self) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT task_id, seq, engine, engine_thread_id, workspace, writes_authorized, status, "
            "parent_task_id, instruction, result, error, reject_reason, created_at, updated_at "
            "FROM tasks ORDER BY seq DESC"
        ) as cur:
            rows = await cur.fetchall()
        out = [self._task_row(r) for r in rows]
        for t in out:
            t["runs"] = await self.get_runs(t["task_id"])
        return out

    @staticmethod
    def _task_row(row: tuple) -> dict[str, Any]:
        cols = ("task_id", "seq", "engine", "engine_thread_id", "workspace", "writes_authorized",
                "status", "parent_task_id", "instruction", "result", "error", "reject_reason",
                "created_at", "updated_at")
        d = dict(zip(cols, row))
        d["writes_authorized"] = bool(d["writes_authorized"])
        return d

    async def _insert_run(self, run_id: str, task_id: str, attempt: int, state: str, *,
                           origin: str = "submitted", queue_position: int | None = None) -> None:
        await self.db.execute(
            "INSERT INTO runs(run_id, task_id, attempt, state, origin, queue_position, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, task_id, attempt, state, origin, queue_position, _now()),
        )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT run_id, task_id, attempt, state, origin, queue_position, started_at, finished_at, "
            "result, error, side_effects, pending_op, wait_reason, wait_requested_at, decision, created_at "
            "FROM runs WHERE run_id=?",
            (run_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._run_row(row) if row else None

    @staticmethod
    def _run_row(row: tuple) -> dict[str, Any]:
        cols = ("run_id", "task_id", "attempt", "state", "origin", "queue_position", "started_at", "finished_at",
                "result", "error", "side_effects", "pending_op", "wait_reason", "wait_requested_at", "decision", "created_at")
        d = dict(zip(cols, row))
        d["side_effects"] = json.loads(d["side_effects"] or "[]")
        d["pending_op"] = json.loads(d["pending_op"]) if d["pending_op"] else None
        return d

    async def get_runs(self, task_id: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT run_id, task_id, attempt, state, origin, queue_position, started_at, finished_at, "
            "result, error, side_effects, pending_op, wait_reason, wait_requested_at, decision, created_at "
            "FROM runs WHERE task_id=? ORDER BY attempt",
            (task_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._run_row(r) for r in rows]

    # ---------- 状态迁移（带状态守卫，原子） ----------
    async def transition_task(self, task_id: str, to_state: str, *,
                               from_states: set[str] | None = None, **fields: Any) -> bool:
        allowed = from_states if from_states is not None else (
            {s for s, targets in TASK_TRANSITIONS.items() if to_state in targets}
        )
        if not allowed:
            raise InvalidTransition(f"无合法来源状态可迁移到 {to_state}")
        placeholders = ",".join("?" for _ in allowed)
        sets = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE tasks SET status=?{', ' + sets if sets else ''}, updated_at=? WHERE task_id=? AND status IN ({placeholders})"
        params: list[Any] = [to_state, *(fields.values()), _now(), task_id, *allowed]
        async with self.db.execute(sql, params) as cur:
            changed = cur.rowcount > 0
        await self.db.commit()
        if not changed:
            return False
        await self._notify_event({"type": "task_state", "task_id": task_id, "to": to_state, **fields})
        return True

    async def transition_run(self, run_id: str, to_state: str, *,
                              from_states: set[str] | None = None, **fields: Any) -> bool:
        allowed = from_states if from_states is not None else (
            {s for s, targets in RUN_TRANSITIONS.items() if to_state in targets}
        )
        placeholders = ",".join("?" for _ in allowed)
        sets = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE runs SET state=?{', ' + sets if sets else ''} WHERE run_id=? AND state IN ({placeholders})"
        params: list[Any] = [to_state, *(fields.values()), run_id, *allowed]
        async with self.db.execute(sql, params) as cur:
            changed = cur.rowcount > 0
        await self.db.commit()
        if changed:
            await self._notify_event({"type": "run_state", "run_id": run_id, "to": to_state, **fields})
        return changed

    async def set_task_thread(self, task_id: str, engine_thread_id: str) -> None:
        """引擎建 Thread 后立即写库（票 01 验收项，不等任务结束）。"""
        await self.db.execute(
            "UPDATE tasks SET engine_thread_id=?, updated_at=? WHERE task_id=?",
            (engine_thread_id, _now(), task_id),
        )
        await self.db.commit()
        await self._notify_event({
            "type": "task_state", "task_id": task_id, "to": "thread-bound",
            "engine_thread_id": engine_thread_id,
        })

    async def set_task_workspace(self, task_id: str, workspace: str) -> None:
        """任务级 workspace 自动分配（票 02：未显式指定时落 ld_workspace_dir/<task_id>）。"""
        await self.db.execute(
            "UPDATE tasks SET workspace=?, updated_at=? WHERE task_id=?",
            (workspace, _now(), task_id),
        )
        await self.db.commit()

    # ---------- Side-Effect Ledger（追加式，从不回滚） ----------
    async def append_side_effect(self, run_id: str, effect: dict[str, Any]) -> list[dict[str, Any]]:
        run = await self.get_run(run_id)
        assert run is not None
        ledger = run["side_effects"]
        ledger.append(effect)
        await self.db.execute("UPDATE runs SET side_effects=? WHERE run_id=?",
                               (json.dumps(ledger, ensure_ascii=False), run_id))
        await self.db.commit()
        return ledger

    # ---------- Followup（追问/重试：新 Run，不覆盖旧 Run） ----------
    async def followup_task(self, task_id: str, instruction: str) -> dict[str, Any]:
        task = await self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] in TASK_ACTIVE_STATES:
            raise InvalidTransition(f"任务 {task_id} 当前处于 {task['status']}，不能追问")
        runs = await self.get_runs(task_id)
        attempt = len(runs) + 1
        run_id = "R" + uuid.uuid4().hex[:12]
        await self._insert_run(run_id, task_id, attempt, "queued",
                                origin="followup", queue_position=await self.queued_count())
        await self.transition_task(task_id, "queued", from_states={task["status"]}, instruction=instruction)
        await self._notify_event({"type": "task_state", "task_id": task_id, "to": "queued",
                                  "new_run_id": run_id, "attempt": attempt})
        t = await self.get_task(task_id)
        assert t is not None
        return t

    async def queued_count(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM runs WHERE state='queued'") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ---------- 通知 ----------
    async def add_notification(self, task_id: str, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        nid = "N" + uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO notifications(notification_id, task_id, kind, payload, created_at) VALUES(?,?,?,?,?)",
            (nid, task_id, kind, json.dumps(payload or {}, ensure_ascii=False), _now()),
        )
        await self.db.commit()
        await self._notify_event({"type": "notification", "notification_id": nid,
                                  "task_id": task_id, "kind": kind, "payload": payload or {}})
        return {"notification_id": nid, "task_id": task_id, "kind": kind, "payload": payload or {}}

    async def get_notifications(self, task_id: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT notification_id, task_id, kind, payload, created_at FROM notifications "
            "WHERE task_id=? ORDER BY created_at",
            (task_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"notification_id": r[0], "task_id": r[1], "kind": r[2],
             "payload": json.loads(r[3] or "{}"), "created_at": r[4]}
            for r in rows
        ]

    # ---------- 调度器取件 ----------
    async def next_queued_run(self, exclude_tasks: set[str]) -> dict[str, Any] | None:
        """FIFO：按任务创建序（seq）+ attempt 取最早的 queued Run；
        exclude_tasks 中的任务（同 Thread 正在跑/等待中）被跳过，实现同 Thread 串行化。"""
        if exclude_tasks:
            ph = ",".join("?" for _ in exclude_tasks)
            sql = (f"SELECT r.run_id FROM runs r JOIN tasks t ON t.task_id=r.task_id "
                   f"WHERE r.state='queued' AND r.task_id NOT IN ({ph}) ORDER BY t.seq, r.attempt LIMIT 1")
            params: list[Any] = list(exclude_tasks)
        else:
            sql = ("SELECT r.run_id FROM runs r JOIN tasks t ON t.task_id=r.task_id "
                   "WHERE r.state='queued' ORDER BY t.seq, r.attempt LIMIT 1")
            params = []
        async with self.db.execute(sql, params) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        run = await self.get_run(row[0])
        assert run is not None
        return run

    async def incomplete_tasks(self) -> list[dict[str, Any]]:
        """重启核对（票 04）输入：所有未进入终态的任务。"""
        states = sorted(TASK_ACTIVE_STATES)
        ph = ",".join("?" for _ in states)
        async with self.db.execute(
            f"SELECT task_id, seq, engine, engine_thread_id, workspace, writes_authorized, status, parent_task_id, "
            f"instruction, result, error, reject_reason, created_at, updated_at FROM tasks "
            f"WHERE status IN ({ph}) ORDER BY seq",
            states,
        ) as cur:
            rows = await cur.fetchall()
        out = [self._task_row(r) for r in rows]
        for t in out:
            t["runs"] = await self.get_runs(t["task_id"])
        return out

    # ---------- 审批挂起/恢复 ----------
    async def suspend_run(self, run_id: str, *, wait_reason: str, pending_op: dict[str, Any] | None,
                           approved_paths: list[str]) -> None:
        ts = self._clock()
        await self.db.execute(
            "UPDATE runs SET state='waiting-approval', wait_reason=?, wait_requested_at=?, pending_op=?, "
            "decision='pending', origin=COALESCE(origin,'submitted') WHERE run_id=?",
            (wait_reason, ts, json.dumps({"pending_op": pending_op, "approved_paths": approved_paths},
                                          ensure_ascii=False), run_id),
        )
        await self.db.commit()
        await self._notify_event({"type": "run_state", "run_id": run_id, "to": "waiting-approval",
                                  "wait_reason": wait_reason})

    async def resume_wait_for_approved_path(self, run_id: str, paths: list[str]) -> None:
        run = await self.get_run(run_id)
        assert run is not None
        info = run["pending_op"] or {}
        info["approved_paths"] = sorted(set(info.get("approved_paths", [])) | set(paths))
        await self.db.execute("UPDATE runs SET pending_op=? WHERE run_id=?",
                               (json.dumps(info, ensure_ascii=False), run_id))
        await self.db.commit()

    # ---------- 票 03：审批查询与决策 ----------
    async def waiting_runs(self) -> list[dict[str, Any]]:
        """全部 waiting-approval Run（审批门禁清扫/待审列表）。"""
        async with self.db.execute(
            "SELECT run_id, task_id, attempt, state, origin, queue_position, started_at, finished_at, "
            "result, error, side_effects, pending_op, wait_reason, wait_requested_at, decision, created_at "
            "FROM runs WHERE state='waiting-approval' ORDER BY wait_requested_at, created_at"
        ) as cur:
            rows = await cur.fetchall()
        return [self._run_row(r) for r in rows]

    async def decide_run(self, run_id: str, decision: str) -> bool:
        """审批决策写入 run；仅 waiting-approval 可写，并发落空返回 False。"""
        async with self.db.execute(
            "UPDATE runs SET decision=? WHERE run_id=? AND state='waiting-approval'",
            (decision, run_id),
        ) as cur:
            changed = cur.rowcount > 0
        await self.db.commit()
        return changed

    # ---------- 活跃任务集（票 01：同 Thread 并发执行串行化） ----------
    async def active_task_ids(self) -> set[str]:
        """当前有 running/waiting-approval Run 的 task_id；调度跳过它们（同 Thread 串行）。"""
        async with self.db.execute(
            "SELECT DISTINCT task_id FROM runs WHERE state IN ('running', 'waiting-approval')"
        ) as cur:
            rows = await cur.fetchall()
        return {r[0] for r in rows}