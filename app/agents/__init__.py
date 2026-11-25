"""Agent Organization（第一批）：Agent Catalog 持久化 + 状态机 + 管理 API/CLI。

设计基线：docs/agent-organization/02-agent-catalog-design.md（方案 §4/§5）。
纪律：Provider Snapshot 与 Local Override 分离；缺失只降状态、永不删行；仅 available 可路由。
"""
from app.agents.catalog import (
    AGENT_STATUS_TRANSITIONS,
    AGENT_STATUSES,
    INVOCATION_MODES,
    PROJECT_TRUST_STATES,
    AgentCatalog,
    InvalidAgentTransition,
)
from app.agents.codex_discovery import (
    CodexProjectDiscoveryProvider,
    CodexUserDiscoveryProvider,
    build_codex_discovery,
)
from app.agents.discovery import (
    AgentDiscoveryService,
    DiscoveryResult,
    JarvisDiscoveryProvider,
    build_default_discovery,
)
from app.agents.project_trust import (
    ProjectTrustError,
    ProjectTrustService,
)

__all__ = [
    "AGENT_STATUSES",
    "AGENT_STATUS_TRANSITIONS",
    "INVOCATION_MODES",
    "PROJECT_TRUST_STATES",
    "AgentCatalog",
    "AgentDiscoveryService",
    "CodexProjectDiscoveryProvider",
    "CodexUserDiscoveryProvider",
    "DiscoveryResult",
    "InvalidAgentTransition",
    "JarvisDiscoveryProvider",
    "ProjectTrustError",
    "ProjectTrustService",
    "build_codex_discovery",
    "build_default_discovery",
]

