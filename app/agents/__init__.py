"""Agent Organization（第一批）：Agent Catalog 持久化 + 状态机 + 管理 API/CLI。

设计基线：docs/agent-organization/02-agent-catalog-design.md（方案 §4/§5）。
纪律：Provider Snapshot 与 Local Override 分离；缺失只降状态、永不删行；仅 available 可路由。
"""
from app.agents.catalog import (
    AGENT_STATUSES,
    AGENT_STATUS_TRANSITIONS,
    INVOCATION_MODES,
    PROJECT_TRUST_STATES,
    AgentCatalog,
    InvalidAgentTransition,
)

__all__ = [
    "AGENT_STATUSES",
    "AGENT_STATUS_TRANSITIONS",
    "INVOCATION_MODES",
    "PROJECT_TRUST_STATES",
    "AgentCatalog",
    "InvalidAgentTransition",
]
