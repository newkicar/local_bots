# 01: Phase-0 审计（现状基线 + 引擎能力矩阵 + Catalog 设计 + 迁移/测试计划）

**What to build:** docs/agent-organization/ 下 5 份审计文档：00-current-state-audit.md（调用链
Secretary→TaskManager→Dispatcher→EngineAdapter 映射 + 测试基线 100 passed + 关键代码事实）、
01-engine-capability-matrix.md（Jarvis 无 profile API → discovery_unsupported，向 Jarvis 仓库
记最小 API 诉求；Codex 0.170.0 无 --agent，内建 default=direct / worker、explorer=
delegated_only，TOML schema name+description+developer_instructions 必填→一律 delegated_only）、
02-agent-catalog-design.md（AgentDescriptor 字段、状态机、invocation_mode 枚举
direct/delegated_only/default/metadata_only、SQL 逻辑表）、03-implementation-plan.md
（11 票 frontier）、04-migration-and-test-plan.md（幂等 add-only 迁移 + 旧库升级测试 + 回归清单）。
同时把仓库根未跟踪的设计文档 "Local Dots Agent Organization 升级实施方案.md" 纳入 git。
纯文档，不改业务代码。

**Blocked by:** 无

**Status:** done

- [x] 5 份审计文档落 docs/agent-organization/
- [x] 设计文档入库（git add）
- [x] 全量 pytest 保持 100 passed 并提交
