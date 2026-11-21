"""Agent Discovery 框架（票 03）：引擎 provider 注册表 + Jarvis provider。

设计基线：docs/agent-organization/02-agent-catalog-design.md（方案 §5）。
纪律：
- provider 只产 metadata-only 描述符（字段形态同 AgentCatalog.upsert_agent）；
  行为性变更（profile_hash 变化）由来源 reader 计算（票 04+）；
- EngineAdapter.discover_agents(scope) 可选方法、基类安全默认 []（FakeEngine 不动）；
- Jarvis provider 仅走公开 API：jarvis/__init__.py 只暴露 create_jarvis，
  无 list_profiles → 如实上报 discovery_unsupported 并记录最小 API 诉求；
  catalog 保留内建 jarvis:default。绝不 import Jarvis 内部模块、绝不发明参数；
- 本服务只做发现与快照 upsert，不做 mark_missing（那是票 07 sync orchestrator 的活）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agents.catalog import AgentCatalog

logger = logging.getLogger(__name__)

DISCOVERY_OK = "ok"
DISCOVERY_UNSUPPORTED = "discovery_unsupported"
DISCOVERY_ERROR = "error"

# Jarvis 最小 API 诉求（记录用，不发明、不实现）：
JARVIS_MINIMAL_API_ASK = (
    "Jarvis 公开面当前仅 create_jarvis(workspace, checkpointer)；"
    "最小诉求 = 增加一个公开只读的 profile/agent 清单 API"
    "（返回 {profile_id, name, description} 元数据），不改变既有工厂签名"
)


class DiscoveryProvider(Protocol):
    """引擎发现 provider：暴露 engine 属性 + async discover(scope) -> DiscoveryResult。"""

    engine: str

    async def discover(self, scope: str) -> "DiscoveryResult": ...


@dataclass(frozen=True)
class DiscoveryResult:
    """provider 单次发现结果（metadata-only，永不含执行语义）。

    status: ok / discovery_unsupported / error；
    agents: upsert_agent 形态描述符列表（非 ok 时恒为空）；
    detail: 失败原因或 API 诉求文本。
    """

    engine: str
    status: str
    agents: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""


class AgentDiscoveryService:
    """发现编排器：按 engine 注册 provider，discover(scope) 批量执行并 upsert ok 结果。

    - provider 失败隔离：单个 provider 抛错只记入该 engine 的 errors，不阻塞其余；
    - unsupported provider 不改 catalog（内建种子原样保留）；
    - 重复注册同 provider 幂等；provider 按 engine 名排序执行（结果确定性）。
    """

    def __init__(self, catalog: AgentCatalog) -> None:
        self._catalog = catalog
        self._providers: dict[str, DiscoveryProvider] = {}

    # ---------- provider 注册表 ----------
    def register_provider(self, provider: DiscoveryProvider) -> None:
        self._providers[provider.engine] = provider

    def engines(self) -> list[str]:
        return sorted(self._providers)

    # ---------- 发现 ----------
    async def discover(self, scope: str = "user") -> dict[str, Any]:
        """对所有已注册引擎执行发现；ok 结果 upsert 入 catalog（metadata-only）。

        返回 {"scope", "engines": [逐引擎报告], "upserted", "unsupported", "errors"}。
        """
        report: dict[str, Any] = {
            "scope": scope,
            "engines": [],
            "upserted": 0,
            "unsupported": [],
            "errors": [],
        }
        for engine in sorted(self._providers):
            provider = self._providers[engine]
            try:
                result = await provider.discover(scope)
            except Exception as exc:  # provider 失败隔离
                detail = f"{type(exc).__name__}: {exc}"
                logger.warning("discovery provider %s 失败: %s", engine, detail)
                report["errors"].append({"engine": engine, "detail": detail})
                report["engines"].append(
                    {"engine": engine, "status": DISCOVERY_ERROR, "detail": detail}
                )
                continue
            if result.status == DISCOVERY_OK:
                for desc in result.agents:
                    await self._catalog.upsert_agent(desc, change_kind="metadata")
                report["upserted"] += len(result.agents)
                report["engines"].append(
                    {"engine": engine, "status": DISCOVERY_OK,
                     "count": len(result.agents)}
                )
            elif result.status == DISCOVERY_UNSUPPORTED:
                report["unsupported"].append({"engine": engine, "detail": result.detail})
                report["engines"].append(
                    {"engine": engine, "status": DISCOVERY_UNSUPPORTED,
                     "detail": result.detail}
                )
            else:
                detail = f"provider 返回未知状态: {result.status}"
                report["errors"].append({"engine": engine, "detail": detail})
                report["engines"].append(
                    {"engine": engine, "status": DISCOVERY_ERROR, "detail": detail}
                )
        return report


class JarvisDiscoveryProvider:
    """Jarvis 引擎 provider：当前无 profile/agent 清单公开 API → 如实上报 unsupported。

    不 import Jarvis 内部模块、不发明 list_profiles 调用；
    catalog 保留内建 jarvis:default 种子。
    """

    engine = "jarvis"

    async def discover(self, scope: str) -> DiscoveryResult:
        return DiscoveryResult(
            self.engine, DISCOVERY_UNSUPPORTED, [], JARVIS_MINIMAL_API_ASK
        )


def build_default_discovery(catalog: AgentCatalog) -> AgentDiscoveryService:
    """默认 provider 集合（票 03）：jarvis。codex user/project provider 由票 04/06 注册。"""
    svc = AgentDiscoveryService(catalog)
    svc.register_provider(JarvisDiscoveryProvider())
    return svc
