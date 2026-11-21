# 11: 路由器 + 路由决策记录 + 端到端回归

**What to build:** app/agents/router.py：resolve(agent_id?, engine?, project_id?) 优先级
explicit-agent > explicit-engine→该引擎默认 agent > 旧默认 jarvis；v1
LD_AGENT_ROUTING_MODE=explicit（LLM 建议的 agent 也走同一服务端校验，不符→拒绝，
秘书成功仍返回 task_id）。路由决策 JSON（selected_agent_id, engine, engine_profile_id,
routing_mode, reason, alternatives, user_explicit_choice）短、可审计，经
transition_task(**fields) 落库。配置 6 项进 settings.py + .env.example
（LD_AGENT_CATALOG_ENABLED/SYNC_ON_STARTUP/ROUTING_MODE/AUTO_ENABLE_USER_PROFILES/
AUTO_ENABLE_PROJECT_PROFILES/PROJECT_TRUST_REQUIRED）。§21 十条验收清单全量回归
（Jarvis/Codex 旧任务、审批、取消、恢复、followup、SSE、Web、对话、100+ pytest）。

**Blocked by:** 07, 09, 10

**Status:** open

- [ ] router + 校验 + 路由 JSON 落库
- [ ] settings/.env.example 6 项
- [ ] 端到端回归全绿并提交
