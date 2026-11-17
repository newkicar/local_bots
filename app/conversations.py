"""对话持久化（SQLite，aiosqlite）——票 13 左侧对话栏。

三层语义：
- conversation_id：一次连续对话（同对话上下文连续，跨对话不连续）
- conversation_messages：该对话的消息流水（聊天页重放来源）
- conversation_tasks：该对话派发过的 task_id 链接（筛选域，tasks 表行不受影响）

纪律：禁模块级单例（与 TaskManager 一致，由 create_app 注入 db_path）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations(
    conversation_id TEXT PRIMARY KEY,
    title TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_messages(
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_msg ON conversation_messages(conversation_id);
CREATE TABLE IF NOT EXISTS conversation_tasks(
    conversation_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    PRIMARY KEY(conversation_id, task_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    def __init__(self, db_path) -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ConversationStore.open() not called")
        return self._db

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ---------- 对话 ----------
    async def create(self, title: str | None = None) -> dict[str, Any]:
        cid = "C" + uuid.uuid4().hex[:12]
        ts = _now()
        async with self.db.execute(
            "INSERT INTO conversations(conversation_id, title, created_at, updated_at) VALUES(?,?,?,?)",
            (cid, title, ts, ts),
        ):
            pass
        await self.db.commit()
        return {"conversation_id": cid, "title": title, "summary": None,
                "created_at": ts, "updated_at": ts}

    async def list_conversations(self) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT conversation_id, title, summary, created_at, updated_at "
            "FROM conversations ORDER BY updated_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"conversation_id": r[0], "title": r[1], "summary": r[2],
             "created_at": r[3], "updated_at": r[4]}
            for r in rows
        ]

    async def get(self, conversation_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT conversation_id, title, summary, created_at, updated_at "
            "FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        async with self.db.execute(
            "SELECT role, content FROM conversation_messages WHERE conversation_id=? ORDER BY message_id",
            (conversation_id,),
        ) as cur:
            messages = [{"role": r[0], "content": r[1]} for r in await cur.fetchall()]
        async with self.db.execute(
            "SELECT task_id FROM conversation_tasks WHERE conversation_id=? ORDER BY rowid",
            (conversation_id,),
        ) as cur:
            task_ids = [r[0] for r in await cur.fetchall()]
        return {
            "conversation_id": row[0], "title": row[1], "summary": row[2],
            "created_at": row[3], "updated_at": row[4],
            "messages": messages, "task_ids": task_ids,
        }

    async def append_message(self, conversation_id: str, role: str, content: str) -> None:
        ts = _now()
        exists = False
        async with self.db.execute(
            "SELECT 1 FROM conversations WHERE conversation_id=?", (conversation_id,)
        ) as cur:
            exists = (await cur.fetchone()) is not None
        if not exists:
            # 保险：未知 cid 补建（消息与标题语义由调用方负责）
            async with self.db.execute(
                "INSERT INTO conversations(conversation_id, created_at, updated_at) VALUES(?,?,?)",
                (conversation_id, ts, ts),
            ):
                pass
        async with self.db.execute(
            "INSERT INTO conversation_messages(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
            (conversation_id, role, content, ts),
        ):
            pass
        await self.db.execute(
            "UPDATE conversations SET updated_at=? WHERE conversation_id=?", (ts, conversation_id)
        )
        await self.db.commit()

    async def message_count(self, conversation_id: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id=?",
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def last_messages(self, conversation_id: str, limit: int = 2) -> list[dict[str, str]]:
        async with self.db.execute(
            "SELECT role, content FROM conversation_messages WHERE conversation_id=? "
            "ORDER BY message_id DESC LIMIT ?",
            (conversation_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    async def link_tasks(self, conversation_id: str, task_ids: list[str]) -> None:
        for task_id in task_ids:
            async with self.db.execute(
                "INSERT OR IGNORE INTO conversation_tasks(conversation_id, task_id) VALUES(?,?)",
                (conversation_id, task_id),
            ):
                pass
        if task_ids:
            await self.db.commit()

    async def find_conversations_for_task(self, task_id: str) -> list[str]:
        """反查链接了该任务的所有对话（票 14 跟进推送分发面；rowid 序）。"""
        async with self.db.execute(
            "SELECT conversation_id FROM conversation_tasks WHERE task_id=? ORDER BY rowid",
            (task_id,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def set_title(self, conversation_id: str, title: str) -> None:
        async with self.db.execute(
            "UPDATE conversations SET title=? WHERE conversation_id=?", (title, conversation_id)
        ):
            pass
        await self.db.commit()

    async def set_summary(self, conversation_id: str, summary: str) -> None:
        async with self.db.execute(
            "UPDATE conversations SET summary=?, updated_at=? WHERE conversation_id=?",
            (summary, _now(), conversation_id),
        ):
            pass
        await self.db.commit()

    async def delete(self, conversation_id: str) -> bool:
        async with self.db.execute(
            "DELETE FROM conversations WHERE conversation_id=?", (conversation_id,)
        ) as cur:
            changed = cur.rowcount > 0
        await self.db.execute(
            "DELETE FROM conversation_messages WHERE conversation_id=?", (conversation_id,)
        )
        await self.db.execute(
            "DELETE FROM conversation_tasks WHERE conversation_id=?", (conversation_id,)
        )
        await self.db.commit()
        return changed
