"""Agent Sync 编排器（票 07）：discover + upsert + missing 降级 + runs 记录一趟完成。

设计基线：docs/agent-organization/02-agent-catalog-design.md（方案 §6）。
纪律：
- sync 只动 catalog 快照行 + sync_runs；override（票 02 的 alias/路由等）永不覆盖；
- provider 失败隔离：异常只令该 engine 放弃 missing 断言（failed_engines），
  同 engine 其余 provider 照常执行；
- unsupported 不弃 engine 的 missing 断言：full set 下 reader 对该 scope 报
  unsupported（如 user scope 的 codex project reader）是正常情形，
  engine 级 missing 判定必须保留；
- missing 只降级不删行：mark_missing 将 available/pending_review/disabled 降为
  stale，unavailable 保持，rejected 不迁移（非法迁移守卫）；
- project scope：票 06 撤销信任降级 stale 的行永不升级 unavailable
  （重新信任后可恢复）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.agents.catalog import AgentCatalog
from app.agents.codex_discovery import (
    CodexProjectDiscoveryProvider,
    CodexUserDiscoveryProvider,
)
from app.agents.discovery import (
    DISCOVERY_OK,
    DISCOVERY_UNSUPPORTED,
    AgentDiscoveryService,
    DiscoveryProvider,
    JarvisDiscoveryProvider,
)

logger = logging.getLogger(__name__)

VALID_SYNC_SCOPES = ("user", "project")

# 变更判定用的快照字段（不含 updated_at：其随每次 upsert 必变，不参与判定）
_SNAP_FIELDS = (
    "name",
    "description",
    "source_path",
    "invocation_mode",
    "profile_version",
    "profile_hash",
    "capabilities",
)


def full_provider_set(catalog: AgentCatalog, *, codex_home: str | None = None) -> list[DiscoveryProvider]:
    """full 发现集合（票 07）：固定顺序 jarvis + codex user reader + codex project reader。

    codex_home=None 表示 discover 时读 $CODEX_HOME（测试可 monkeypatch 环境变量）。
    """
    return [
        JarvisDiscoveryProvider(),
        CodexUserDiscoveryProvider(codex_home),
        CodexProjectDiscoveryProvider(catalog),
    ]


def build_full_discovery(catalog: AgentCatalog, *, codex_home: str | None = None) -> AgentDiscoveryService:
    """新 AgentDiscoveryService 注册 full provider 集合（与 build_codex_discovery 共存）。"""
    svc = AgentDiscoveryService(catalog)
    for provider in full_provider_set(catalog, codex_home=codex_home):
        svc.register_provider(provider)
    return svc


class AgentSyncOrchestrator:
    """一趟 sync：full 集 provider discover → upsert → missing 降级 → record_sync_run。

    - providers=None 用 full_provider_set（缺省）；
    - clock 可注入（缺省 UTC ISO 时间戳）；
    - 返回 = runs 记录字段 + discovered/changed/missing/pending_review/errors/unsupported。
    """

    def __init__(
        self,
        catalog: AgentCatalog,
        *,
        providers: list[DiscoveryProvider] | None = None,
        codex_home: str | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._catalog = catalog
        self._providers = (
            providers
            if providers is not None
            else full_provider_set(catalog, codex_home=codex_home)
        )
        self._clock = clock or (lambda: datetime.now(UTC).isoformat())

    async def run_sync(self, scope: str) -> dict[str, Any]:
        """对 scope（user/project）执行一趟 sync 并写一条 sync_runs 记录。"""
        if scope not in VALID_SYNC_SCOPES:
            raise ValueError(f"scope must be one of {VALID_SYNC_SCOPES}, got {scope!r}")
        started_at = self._clock()

        # 按 engine 分组（保注册顺序）
        by_engine: dict[str, list[DiscoveryProvider]] = {}
        for provider in self._providers:
            by_engine.setdefault(provider.engine, []).append(provider)
        claiming = set(by_engine)  # 仍持有 missing 断言权的 engine

        errors: list[str] = []
        unsupported: list[dict[str, Any]] = []
        failed_engines: set[str] = set()
        claims: set[str] = set()
        discovered = 0
        changed = 0

        for engine in sorted(by_engine):
            for provider in by_engine[engine]:
                cls = type(provider).__name__
                try:
                    res = await provider.discover(scope)
                except Exception as exc:  # provider 失败隔离
                    errors.append(f"{engine} {cls} failed: {type(exc).__name__}: {exc}")
                    failed_engines.add(engine)
                    logger.warning("sync provider %s %s 失败", engine, cls, exc_info=True)
                    continue
                if res.status == DISCOVERY_OK:
                    for desc in res.agents:
                        agent_id = desc["agent_id"]
                        prev = await self._catalog.get_agent(agent_id)
                        new = await self._catalog.upsert_agent(
                            desc, change_kind=res.change_kind
                        )
                        claims.add(agent_id)
                        discovered += 1
                        if prev is None or any(
                            prev.get(f) != new.get(f) for f in _SNAP_FIELDS
                        ):
                            changed += 1
                    for e in res.errors:  # 文件级错误：只记录，不翻 status、不算 fail
                        label = e.get("agent") or e.get("project")
                        errors.append(f"{engine} {cls} {label}: {e.get("detail", "")}")
                elif res.status == DISCOVERY_UNSUPPORTED:
                    # 不弃 claiming：reader 对该 scope unsupported 属正常情形
                    unsupported.append(
                        {
                            "engine": engine,
                            "detail": res.detail,
                            "errors": list(res.errors),
                        }
                    )
                else:
                    errors.append(f"{engine} {cls} unknown status: {res.status}")
                    failed_engines.add(engine)
            if engine in failed_engines:
                claiming.discard(engine)

        # missing 降级：只对仍持有 missing 断言权的 engine
        missing_ids: list[str] = []
        if claiming:
            for row in await self._catalog.list_agents():
                aid = row["agent_id"]
                if aid in claims:
                    continue
                if row["source_scope"] != scope or row["engine"] not in claiming:
                    continue
                if row["discovery_status"] == "rejected":
                    continue  # rejected → unavailable 非法迁移，保持不动
                if scope == "project" and row["discovery_status"] == "stale":
                    continue  # 撤销降级的行重新信任可恢复，永不升级 unavailable
                missing_ids.append(aid)
            if missing_ids:
                await self._catalog.mark_missing(missing_ids)

        # pending_review 事后计算（mark_missing 之后算最终态）
        pending_review = 0
        for aid in claims | set(missing_ids):
            agent = await self._catalog.get_agent(aid)
            if agent is not None and agent["discovery_status"] == "pending_review":
                pending_review += 1

        status = "error" if failed_engines else "ok"
        finished_at = self._clock()
        run = await self._catalog.record_sync_run(
            scope=scope,
            status=status,
            discovered=discovered,
            changed=changed,
            missing=len(missing_ids),
            pending_review=pending_review,
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )
        return {
            **run,
            "discovered": discovered,
            "changed": changed,
            "missing": len(missing_ids),
            "pending_review": pending_review,
            "errors": errors,
            "unsupported": unsupported,
        }
