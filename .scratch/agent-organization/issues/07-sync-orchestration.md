# 07: Sync 编排（启动 + API + CLI）

**What to build:** app/agents/sync.py：AgentSyncOrchestrator.run_sync(scope)——对每个引擎
provider 调 discover_agents，upsert catalog：metadata-only 变更→更新 snapshot；行为性变更
（instructions/model/tool policy/源路径）→pending_review；源缺失→stale/unavailable（永不删行）；
agent_sync_runs 追加（sync_id/时间/计数/errors）。触发：FastAPI lifespan（LD_AGENT_SYNC_ON_STARTUP）
+ POST /api/agents/sync（结果 JSON：sync_id/status/discovered/changed/missing/pending_review/errors）
+ CLI agents sync。幂等；永不覆盖 Local Override。

**Blocked by:** 03, 06

**Status:** open

- [ ] orchestrator + 变更分类（metadata vs behavior）
- [ ] 启动钩子 + API + CLI
- [ ] 测试（幂等、override 保护、missing 不删行）并提交
