"""票 11：Agent 路由器——任务路由与服务端 agent 校验的统一入口。

v1 规则（LD_AGENT_ROUTING_MODE=explicit）：
- 显式 agent_id 优先：完整服务端校验（存在 / available / 引擎匹配 /
  delegated_only 拒绝 / 引擎挂载 / 项目匹配 / 项目信任门）。
- 仅 engine 或缺省：路由到 "<engine>:default"；目录禁用、未登记或
  引擎未挂载时降级 legacy 引擎路由（沿用 engine 字段，不选 agent）。
- 校验失败抛 RoutingError（HTTP 422 detail + alternatives 建议）。
"""
from __future__ import annotations

from typing import Any

from app.agents.catalog import AgentCatalog
from app.agents.project_trust import ProjectTrustService


class RoutingError(Exception):
    """路由/校验失败：detail 为 422 文案，alternatives 为可替代 agent 建议。"""

    def __init__(self, detail: str, alternatives: list[str] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.alternatives = list(alternatives or [])

    @property
    def full_detail(self) -> str:
        if self.alternatives:
            return f"{self.detail}（可替代: {', '.join(self.alternatives)}）"
        return self.detail


def freeze_snapshot(agent: dict[str, Any]) -> dict[str, Any]:
    """票 08：任务创建时刻的 agent descriptor + hash 快照。"""
    return {
        "agent_id": agent["agent_id"],
        "engine": agent["engine"],
        "engine_profile_id": agent["engine_profile_id"],
        "engine_profile_version": agent.get("profile_version"),
        "engine_profile_hash": agent.get("profile_hash"),
        "name": agent.get("name"),
        "invocation_mode": agent.get("invocation_mode"),
        "source_scope": agent.get("source_scope"),
        "effective_status": agent.get("effective_status"),
    }


class AgentRouter:
    def __init__(self, catalog: AgentCatalog, trust: ProjectTrustService, *,
                 available_engines: set[str], routing_mode: str = "explicit",
                 catalog_enabled: bool = True, default_engine: str = "jarvis") -> None:
        self._catalog = catalog
        self._trust = trust
        self.available_engines = set(available_engines)
        self.routing_mode = routing_mode
        self.catalog_enabled = bool(catalog_enabled)
        self.default_engine = default_engine

    async def agent_row(self, agent_id: str | None) -> dict[str, Any] | None:
        if agent_id is None:
            return None
        return await self._catalog.get_agent(agent_id)

    async def resolve(self, *, agent_id: str | None = None, engine: str | None = None,
                     project_id: str | None = None) -> dict[str, Any]:
        base: dict[str, Any] = {
            "selected_agent_id": None, "engine": None, "engine_profile_id": None,
            "routing_mode": self.routing_mode, "reason": "", "alternatives": [],
            "user_explicit_choice": False,
        }
        if agent_id is None:
            return await self._resolve_by_engine(base, engine)
        return await self._resolve_by_agent(base, agent_id, engine, project_id)

    # ---------- engine 路径 ----------
    async def _resolve_by_engine(self, base: dict[str, Any], engine: str | None) -> dict[str, Any]:
        eng = (engine or self.default_engine).strip().lower()
        explicit = engine is not None
        base["engine"] = eng
        base["user_explicit_choice"] = explicit
        if not self.catalog_enabled:
            base["reason"] = f"目录禁用 → legacy 引擎路由（{eng}）"
            return base
        row = await self._catalog.get_agent(f"{eng}:default")
        if row is None:
            base["reason"] = f"引擎 {eng} 未登记 agent，走 legacy 引擎路由"
            return base
        if row["effective_status"] != "available":
            raise RoutingError(f"引擎 {eng} 默认 agent 不可用（effective_status={row['effective_status']!r}）")
        if eng not in self.available_engines:
            if explicit:
                raise RoutingError(f"引擎 {eng!r} 未挂载（可用引擎: {sorted(self.available_engines)}）")
            base["reason"] = f"缺省引擎 {eng} 未挂载，走 legacy 引擎路由"
            return base
        base["selected_agent_id"] = f"{eng}:default"
        base["engine_profile_id"] = row["engine_profile_id"]
        base["reason"] = f"显式引擎 → {eng}:default" if explicit else f"缺省路由 → {eng}:default"
        return base

    # ---------- agent 路径（检查顺序固定，文案逐字——测试靠其匹配） ----------
    async def _resolve_by_agent(self, base: dict[str, Any], agent_id: str,
                                engine: str | None, project_id: str | None) -> dict[str, Any]:
        base["user_explicit_choice"] = True
        if not self.catalog_enabled:
            raise RoutingError(f"agent 目录已禁用（LD_AGENT_CATALOG_ENABLED=false），拒绝显式 agent: {agent_id}")
        row = await self._catalog.get_agent(agent_id)
        if row is None:
            raise RoutingError(f"agent not found: {agent_id}")
        st = row["effective_status"]
        if st != "available":
            raise RoutingError(f"agent {agent_id} 不可用（effective_status={st!r}）")
        eng = row["engine"]
        if engine is not None and engine != eng:
            raise RoutingError(f"agent {agent_id} 属于 {eng!r} 引擎，与请求 engine={engine!r} 不匹配")
        if row.get("invocation_mode") == "delegated_only":
            raise RoutingError(f"agent {agent_id} 为 delegated_only（仅可被上层委派，不可直接提交）",
                               alternatives=[f"{eng}:default"])
        if eng not in self.available_engines:
            raise RoutingError(f"agent {agent_id} 的引擎 {eng!r} 未挂载（可用引擎: {sorted(self.available_engines)}）",
                               alternatives=await self._alternatives())
        agent_pid = (row.get("metadata") or {}).get("project_id")
        if project_id is not None and agent_pid is not None and project_id != agent_pid:
            raise RoutingError(f"agent {agent_id} 属于项目 {agent_pid!r}，与请求 project_id={project_id!r} 不匹配")
        if row.get("source_scope") == "project" and not await self._trust.can_use_project_agent(agent_pid or "", "project"):
            raise RoutingError(f"项目 {agent_pid!r} 未通过信任校验，项目级 agent {agent_id} 不可用",
                               alternatives=[f"{eng}:default"])
        base["selected_agent_id"] = agent_id
        base["engine"] = eng
        base["engine_profile_id"] = row["engine_profile_id"]
        base["reason"] = f"显式 agent → {agent_id}"
        return base

    async def _alternatives(self, limit: int = 3) -> list[str]:
        """最多 limit 个：effective available + 非 delegated_only + 引擎已挂载。"""
        out: list[str] = []
        for a in await self._catalog.list_agents("available"):
            if a.get("effective_status") != "available":
                continue
            if a.get("invocation_mode") == "delegated_only":
                continue
            if a["engine"] not in self.available_engines:
                continue
            out.append(a["agent_id"])
            if len(out) >= limit:
                break
        return out