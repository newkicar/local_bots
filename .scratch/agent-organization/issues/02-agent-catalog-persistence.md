# 02: AgentDescriptor + Catalog 持久化 + 管理 API/CLI

**What to build:** app/agents/catalog.py：AgentDescriptor dataclass + AgentCatalog（挂既有
tasks.db，TaskManager.open() 式幂等 PRAGMA add-only 迁移）：agent_catalog（UNIQUE
(engine, engine_profile_id, source_scope, source_id)）、agent_overrides、agent_sync_runs、
projects 表。状态 discovered/pending_review/available/disabled/stale/unavailable/rejected，
仅 available 可路由；Snapshot/Override 分离（enabled/alias/extra_capabilities/
routing_priority/auto_route_allowed/approval_state）。内建播种：jarvis:default(direct)、
codex:default(direct)、codex:worker、codex:explorer(delegated_only)。
GET /api/agents、GET /api/agents/{id}、启用/禁用/审批端点（挂既有 Bearer 鉴权）；
app/cli.py 最小 argparse：python -m app.cli agents list|inspect。

**Blocked by:** 01

**Status:** done

- [x] 三表 + projects 表幂等迁移（旧库升级测试）
- [x] catalog CRUD/状态迁移守卫（transition）
- [x] 内建种子 + API + CLI
- [x] 全量 pytest 绿并提交
