# 02 Agent Catalog 设计

## AgentDescriptor（逻辑模型，落 SQLite JSON/列混合）

agent_id（稳定 ID）: 内建 "engine:profile"（jarvis:default、codex:default、
codex:worker、codex:explorer）；用户自定义 "codex:<name>"（user scope）；
项目级 "codex:project:<project_id>:<name>"。
字段：agent_id, engine, engine_profile_id, name, description, source_scope
（user/project/builtin）, source_id, source_path, capabilities(JSON), invocation_mode,
profile_version, profile_hash, discovery_status, trust_state, metadata(JSON),
created_at, updated_at。

invocation_mode 枚举：direct（可作顶层执行体）/ delegated_only（只能被上层委派，
顶层调用必须显式拒绝）/ default（引擎缺省执行体）/ metadata_only（只登记不可路由）。

## 状态机

discovered → pending_review（行为性变更/项目级新发现，且未开自动启用）
→ available（唯一可路由态，需审批或自动启用配置）
→ disabled（本地 override 停用）
→ stale（同步源暂时缺失）/ unavailable（发现失败/unsupported）
→ rejected（审批明确拒绝）。
仅 available（且 enabled）进入路由候选。状态迁移走守卫函数，非法迁移抛错。

## 表（落既有 tasks.db，幂等 add-only）

agent_catalog：PK agent_id；UNIQUE(engine, engine_profile_id, source_scope, source_id)。
agent_overrides：PK agent_id；列 enabled/alias/extra_capabilities(JSON)/
routing_priority/auto_route_allowed/approval_state。**Provider 快照（catalog 行）
与本地 override 分离；sync 永不覆盖 override。**
agent_sync_runs：PK sync_id；scope/status/started_at/finished_at/
discovered/changed/missing/pending_review/errors(JSON)。

## 变更检测规则（§4.7）

- metadata-only（description 措辞、hash 未变）→ 直接更新 snapshot；
- 行为性（developer_instructions 变化、model 字段、tool policy、source_path 移动、
  TOML 内容 hash 变化）→ pending_review；
- 源缺失 → stale（连续缺失且曾 available）/unavailable（从未 available），永不删行。
