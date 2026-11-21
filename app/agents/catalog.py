"""Agent Catalog：持久化 + 状态机（复用既有 tasks.db，aiosqlite）。

设计基线：docs/agent-organization/02-agent-catalog-design.md（方案 §4/§5）。
纪律：
- 幂等 add-only 迁移（先例 TaskManager.open()），旧库/重复 open 安全；
- Provider Snapshot（agent_catalog 行）与 Local Override（agent_overrides）分离，
  同步永不覆盖 override；缺失源只降状态（stale/unavailable），永不删行；
- 状态机守卫：仅 available 可进入路由候选（路由器在 app/agents/router.py，票 11）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

AGENT_STATUSES = (
    "discovered",
    "pending_review",
    "available",
    "disabled",
    "stale",
    "unavailable",
    "rejected",
)

AGENT_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "discovered": {"pending_review", "available", "unavailable"},
    "pending_review": {"available", "rejected", "disabled", "stale", "unavailable"},
    "available": {"disabled", "pending_review", "stale", "unavailable", "rejected"},
    "disabled": {"available", "pending_review", "stale", "unavailable", "rejected"},
    "stale": {"available", "pending_review", "unavailable", "disabled"},
    "unavailable": {"discovered", "pending_review", "available"},
    "rejected": {"pending_review", "available"},
}

INVOCATION_MODES = ("direct", "delegated_only", "default", "metadata_only")
PROJECT_TRUST_STATES = ("pending", "trusted", "revoked")

# 内建种子（能力矩阵审计结论，docs/agent-organization/01）：
# Jarvis 无 profile API → 只有 default；Codex 0.170.0 无 --agent，内建 default=direct。
_BUILTIN_AGENTS: list[dict[str, Any]] = [
    {"agent_id": "jarvis:default", "engine": "jarvis", "engine_profile_id": "default",
     "name": "Jarvis 默认", "description": "create_jarvis(workspace, checkpointer) 直连",
     "source_scope": "builtin", "source_id": "default", "invocation_mode": "direct"},
    {"agent_id": "codex:default", "engine": "codex", "engine_profile_id": "default",
     "name": "Codex 默认", "description": "codex exec 顶层执行（read-only / workspace-write）",
     "source_scope": "builtin", "source_id": "default", "invocation_mode": "direct"},
    {"agent_id": "codex:worker", "engine": "codex", "engine_profile_id": "worker",
     "name": "Codex Worker", "description": "Codex 内建 worker（仅可被上层委派）",
     "source_scope": "builtin", "source_id": "worker", "invocation_mode": "delegated_only"},
    {"agent_id": "codex:explorer", "engine": "codex", "engine_profile_id": "explorer",
     "name": "Codex Explorer", "description": "Codex 内建 explorer（仅可被上层委派）",
     "source_scope": "builtin", "source_id": "explorer", "invocation_mode": "delegated_only"},
]

_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_catalog(
    agent_id TEXT PRIMARY KEY,
    engine TEXT NOT NULL,
    engine_profile_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    source_scope TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_path TEXT,
    capabilities TEXT NOT NULL DEFAULT '{}',
    invocation_mode TEXT NOT NULL DEFAULT 'direct',
    profile_version TEXT,
    profile_hash TEXT,
    discovery_status TEXT NOT NULL DEFAULT 'discovered',
    trust_state TEXT NOT NULL DEFAULT 'pending',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(engine, engine_profile_id, source_scope, source_id)
);
CREATE TABLE IF NOT EXISTS agent_overrides(
    agent_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    alias TEXT,
    extra_capabilities TEXT NOT NULL DEFAULT '[]',
    routing_priority INTEGER NOT NULL DEFAULT 0,
    auto_route_allowed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_sync_runs(
    sync_id TEXT PRIMARY KEY,
    scope TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    discovered INTEGER NOT NULL DEFAULT 0,
    changed INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    pending_review INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS projects(
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    canonical_root TEXT NOT NULL,
    trust_state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_ROW_COLS = (
    "agent_id, engine, engine_profile_id, name, description, source_scope, source_id,"
    " source_path, capabilities, invocation_mode, profile_version, profile_hash,"
    " discovery_status, trust_state, metadata, created_at, updated_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InvalidAgentTransition(Exception):
    """非法 Agent 状态迁移（调用方按业务语义决定 409 或忽略）。"""


def _row_to_dict(row) -> dict[str, Any]:
    d = {c: row[c] for c in row.keys()}
    for k in ("capabilities", "metadata"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                d[k] = {}
    return d


class AgentCatalog:
    """Agent Catalog 存储（挂既有 tasks.db，与 TaskManager 共享文件、各持连接）。"""

    def __init__(self, db_path, *, clock=None,
                 auto_enable_user_profiles: bool = False,
                 auto_enable_project_profiles: bool = False) -> None:
        self._db_path = str(db_path)
        self._clock = clock or _now
        self._auto_user = bool(auto_enable_user_profiles)
        self._auto_project = bool(auto_enable_project_profiles)
        self._db: aiosqlite.Connection | None = None

    # ---------- 生命周期 ----------
    async def open(self) -> None:
        """幂等：建表 + 内建种子；旧 tasks.db 升级安全（add-only）。"""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_CATALOG_SCHEMA)
        await self._db.commit()
        await self.seed_builtins()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("AgentCatalog 尚未 open()")
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ---------- 内建种子 ----------
    async def seed_builtins(self) -> None:
        """幂等 upsert 4 个内建；rejected 的内建不强行拉回 available。"""
        for b in _BUILTIN_AGENTS:
            desc = {**b, "capabilities": {}, "metadata": {}, "profile_hash": "builtin",
                    "profile_version": "builtin", "source_path": None}
            await self._upsert_row(desc, change_kind="metadata", fresh_status="available")

    # ---------- 快照 upsert（Provider Snapshot） ----------
    async def _upsert_row(self, desc: dict[str, Any], *, change_kind: str,
                          fresh_status: str | None = None) -> dict[str, Any]:
        """upsert 一行快照。change_kind: metadata | behavior。

        行为性变更（profile_hash 变化 + behavior）把已路由过的 agent 拉回
        pending_review 重新审批；metadata-only 不改变状态。
        """
        now = self._clock()
        agent_id = desc["agent_id"]
        capabilities = json.dumps(desc.get("capabilities") or {}, ensure_ascii=False)
        metadata = json.dumps(desc.get("metadata") or {}, ensure_ascii=False)
        async with self.db.execute(
            "SELECT discovery_status FROM agent_catalog WHERE agent_id=?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            status = fresh_status or self._initial_status(desc.get("source_scope", "user"))
            trust = "trusted" if desc.get("source_scope") == "builtin" else "pending"
            await self.db.execute(
                """INSERT INTO agent_catalog(agent_id, engine, engine_profile_id, name,
                   description, source_scope, source_id, source_path, capabilities,
                   invocation_mode, profile_version, profile_hash, discovery_status,
                   trust_state, metadata, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (agent_id, desc["engine"], desc["engine_profile_id"], desc["name"],
                 desc.get("description"), desc.get("source_scope", "user"),
                 desc.get("source_id", ""), desc.get("source_path"), capabilities,
                 desc.get("invocation_mode", "direct"), desc.get("profile_version"),
                 desc.get("profile_hash"), status, trust, metadata, now, now),
            )
        else:
            prev = row["discovery_status"]
            status = prev
            async with self.db.execute(
                "SELECT profile_hash FROM agent_catalog WHERE agent_id=?", (agent_id,)
            ) as cur:
                r2 = await cur.fetchone()
                stored_hash = r2["profile_hash"] if r2 else None
            behavior = (
                change_kind == "behavior"
                and stored_hash != desc.get("profile_hash")
                and prev in ("available", "disabled", "stale")
            )
            if behavior:
                status = "pending_review"
            await self.db.execute(
                """UPDATE agent_catalog SET engine=?, engine_profile_id=?, name=?,
                   description=?, source_path=?, capabilities=?, invocation_mode=?,
                   profile_version=?, profile_hash=?, discovery_status=?, metadata=?,
                   updated_at=? WHERE agent_id=?""",
                (desc["engine"], desc["engine_profile_id"], desc["name"],
                 desc.get("description"), desc.get("source_path"), capabilities,
                 desc.get("invocation_mode", "direct"), desc.get("profile_version"),
                 desc.get("profile_hash"), status, metadata, now, agent_id),
            )
        await self.db.commit()
        return await self.get_agent(agent_id)

    def _initial_status(self, source_scope: str) -> str:
        if source_scope == "builtin":
            return "available"
        if source_scope == "user" and self._auto_user:
            return "available"
        if source_scope == "project" and self._auto_project:
            return "available"
        return "pending_review"

    async def upsert_agent(self, descriptor: dict[str, Any], *, change_kind: str = "metadata") -> dict[str, Any]:
        """对外入口：discovery/sync 提供快照 upsert（descriptor 字段见设计文档 §4.3）。"""
        return await self._upsert_row(descriptor, change_kind=change_kind)

    # ---------- 缺失处理（永不删行） ----------
    async def mark_missing(self, agent_ids: list[str]) -> list[dict[str, Any]]:
        """同步源缺失：曾可路由 → stale；否则 → unavailable。已 unavailable 保持。"""
        out = []
        for aid in agent_ids:
            got = await self.get_agent(aid)
            if got is None:
                continue
            prev = got["discovery_status"]
            if prev == "unavailable":
                out.append(got)
                continue
            to = "stale" if prev in ("available", "pending_review", "disabled") else "unavailable"
            row = await self.transition_agent(aid, to)
            out.append(row)
        return out

    # ---------- 查询（快照 + override 合并） ----------
    async def _merged(self, agent_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            f"SELECT {_ROW_COLS} FROM agent_catalog WHERE agent_id=?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        d = _row_to_dict(row)
        async with self.db.execute(
            "SELECT enabled, alias, extra_capabilities, routing_priority, auto_route_allowed"
            " FROM agent_overrides WHERE agent_id=?", (agent_id,)
        ) as cur:
            ov = await cur.fetchone()
        if ov:
            d["override"] = {
                "enabled": bool(ov["enabled"]),
                "alias": ov["alias"],
                "extra_capabilities": json.loads(ov["extra_capabilities"] or "[]"),
                "routing_priority": ov["routing_priority"],
                "auto_route_allowed": bool(ov["auto_route_allowed"]),
            }
            d["effective_status"] = "disabled" if not ov["enabled"] else d["discovery_status"]
        else:
            d["override"] = {}
            d["effective_status"] = d["discovery_status"]
        return d

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return await self._merged(agent_id)

    async def list_agents(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT {_ROW_COLS} FROM agent_catalog"
        if status:
            rows = await (await self.db.execute(
                sql + " WHERE discovery_status=?", (status,))).fetchall()
        else:
            rows = await (await self.db.execute(sql)).fetchall()
        out = []
        for r in rows:
            d = await self._merged(r["agent_id"])
            out.append(d)
        return out

    # ---------- 状态机守卫 ----------
    async def transition_agent(self, agent_id: str, to_status: str) -> dict[str, Any]:
        if to_status not in AGENT_STATUSES:
            raise InvalidAgentTransition(f"未知状态 {to_status!r}")
        got = await self.get_agent(agent_id)
        if got is None:
            raise KeyError(agent_id)
        cur = got["discovery_status"]
        if to_status not in AGENT_STATUS_TRANSITIONS.get(cur, set()):
            raise InvalidAgentTransition(f"{agent_id}: {cur} → {to_status} 非法迁移")
        now = self._clock()
        await self.db.execute(
            "UPDATE agent_catalog SET discovery_status=?, updated_at=? WHERE agent_id=?",
            (to_status, now, agent_id),
        )
        if to_status == "available":  # 审批通过即建立信任（§4.6：仅 available 可路由）
            await self.db.execute(
                "UPDATE agent_catalog SET trust_state='trusted' WHERE agent_id=?", (agent_id,)
            )
        await self.db.commit()
        return await self.get_agent(agent_id)

    async def reject_agent(self, agent_id: str, *, trust_reject: bool = False) -> dict[str, Any]:
        got = await self.get_agent(agent_id)
        if got is None:
            raise KeyError(agent_id)
        if trust_reject:
            await self.db.execute(
                "UPDATE agent_catalog SET trust_state='rejected', updated_at=? WHERE agent_id=?",
                (self._clock(), agent_id),
            )
        return await self.transition_agent(agent_id, "rejected")

    # ---------- Local Override（快照隔离） ----------
    async def set_override(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"enabled", "alias", "extra_capabilities", "routing_priority",
                   "auto_route_allowed"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"override 字段不合法: {sorted(bad)}")
        if await self.get_agent(agent_id) is None:
            raise KeyError(agent_id)
        now = self._clock()
        cur = await (await self.db.execute(
            "SELECT enabled, alias, extra_capabilities, routing_priority, auto_route_allowed"
            " FROM agent_overrides WHERE agent_id=?", (agent_id,))).fetchone()
        base = {
            "enabled": 1 if cur is None else cur["enabled"],
            "alias": cur["alias"] if cur else None,
            "extra_capabilities": json.loads(cur["extra_capabilities"]) if cur else [],
            "routing_priority": cur["routing_priority"] if cur else 0,
            "auto_route_allowed": 1 if cur and cur["auto_route_allowed"] else 0,
        }
        for k, v in fields.items():
            base[k] = (1 if v else 0) if k in ("enabled", "auto_route_allowed") else v
        if "extra_capabilities" in fields:
            base["extra_capabilities"] = json.dumps(fields["extra_capabilities"], ensure_ascii=False)
        elif "extra_capabilities" in base:
            base["extra_capabilities"] = json.dumps(base["extra_capabilities"], ensure_ascii=False)
        await self.db.execute(
            """INSERT INTO agent_overrides(agent_id, enabled, alias, extra_capabilities,
               routing_priority, auto_route_allowed, updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 enabled=excluded.enabled, alias=excluded.alias,
                 extra_capabilities=excluded.extra_capabilities,
                 routing_priority=excluded.routing_priority,
                 auto_route_allowed=excluded.auto_route_allowed,
                 updated_at=excluded.updated_at""",
            (agent_id, base["enabled"], base["alias"], base["extra_capabilities"],
             base["routing_priority"], base["auto_route_allowed"], now),
        )
        await self.db.commit()
        return await self.get_agent(agent_id)

    # ---------- 同步历史 ----------
    async def record_sync_run(self, *, scope: str, status: str,
                              discovered: int = 0, changed: int = 0, missing: int = 0,
                              pending_review: int = 0, errors: list[str] | None = None,
                              started_at: str | None = None,
                              finished_at: str | None = None,
                              sync_id: str | None = None) -> dict[str, Any]:
        sid = sync_id or f"S{uuid.uuid4().hex[:12]}"
        now = self._clock()
        await self.db.execute(
            """INSERT INTO agent_sync_runs(sync_id, scope, status, started_at, finished_at,
               discovered, changed, missing, pending_review, errors)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (sid, scope, status, started_at or now, finished_at or now,
             discovered, changed, missing, pending_review,
             json.dumps(errors or [], ensure_ascii=False)),
        )
        await self.db.commit()
        return {"sync_id": sid, "scope": scope, "status": status}

    async def sync_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await (await self.db.execute(
            "SELECT * FROM agent_sync_runs ORDER BY started_at DESC LIMIT ?", (limit,))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["errors"] = json.loads(d["errors"] or "[]")
            out.append(d)
        return out

    # ---------- 项目登记（信任状态机见 app/agents/project_trust.py，票 05） ----------
    async def register_project(self, name: str, root_path: str) -> dict[str, Any]:
        pid = f"P{uuid.uuid4().hex[:12]}"
        now = self._clock()
        await self.db.execute(
            """INSERT INTO projects(project_id, name, root_path, canonical_root,
               trust_state, created_at, updated_at) VALUES(?,?,?,?,?,?,?)""",
            (pid, name, root_path, str(Path(root_path)), "pending", now, now),
        )
        await self.db.commit()
        return await self.get_project(pid)

    async def set_project_canonical_root(self, project_id: str, canonical_root: str) -> None:
        await self.db.execute(
            "UPDATE projects SET canonical_root=?, updated_at=? WHERE project_id=?",
            (canonical_root, self._clock(), project_id),
        )
        await self.db.commit()

    async def set_project_trust(self, project_id: str, state: str) -> dict[str, Any]:
        if state not in PROJECT_TRUST_STATES:
            raise ValueError(f"未知信任状态 {state!r}")
        if await self.get_project(project_id) is None:
            raise KeyError(project_id)
        await self.db.execute(
            "UPDATE projects SET trust_state=?, updated_at=? WHERE project_id=?",
            (state, self._clock(), project_id),
        )
        await self.db.commit()
        return await self.get_project(project_id)

    async def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = await (await self.db.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,))).fetchone()
        return dict(row) if row else None

    async def list_projects(self, trust_state: str | None = None) -> list[dict[str, Any]]:
        if trust_state:
            rows = await (await self.db.execute(
                "SELECT * FROM projects WHERE trust_state=? ORDER BY created_at",
                (trust_state,))).fetchall()
        else:
            rows = await (await self.db.execute(
                "SELECT * FROM projects ORDER BY created_at")).fetchall()
        return [dict(r) for r in rows]
