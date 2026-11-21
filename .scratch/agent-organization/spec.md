# Spec: Local Dots Agent Organization（第一批：审计 + Agent Catalog + Discovery + Sync + 路由）

Status: ready-for-agent

## Problem Statement

现状：任务只能隐式绑定"引擎默认行为"（Jarvis 无 profile API，Codex 无 --agent 旗标），
用户无法按"能力/角色"选择执行体；引擎能力边界没有记录；~/.codex/agents/*.toml 与
项目级 .codex/agents/*.toml 完全没有被发现；任务记录里没有执行体身份快照，
事后无法审计"这个任务到底是被什么 agent 用哪个版本的 profile 执行的"。

## Solution（第一批范围，对应方案 §21）

1. **Phase-0 审计**（docs/agent-organization/）：现状基线、引擎能力矩阵、
   Agent Catalog 设计、实施与迁移/测试计划。不做业务代码。
2. **Agent Catalog**（app/agents/，持久化进既有 tasks.db）：
   agent_catalog / agent_overrides / agent_sync_runs 三表 + projects 表（项目信任）；
   状态机 discovered→pending_review→available→disabled→stale/unavailable/rejected，
   仅 available 可路由；Provider Snapshot 与 Local Override 分离，sync 永不覆盖 override。
3. **Discovery**：AgentDiscoveryService + 引擎 provider 注册；
   Jarvis 仅走公开 API（create_jarvis 无 profile 能力 → 如实记 discovery_unsupported，
   保留内建 jarvis:default）；Codex 读 ~/.codex/agents/*.toml（CODEX_HOME）与
   受信任项目 <root>/.codex/agents/*.toml，metadata-only，坏文件跳过并记录；
   内建 codex:default=direct，codex:worker/codex:explorer=delegated_only。
4. **Sync 编排**：服务启动时 sync（LD_AGENT_SYNC_ON_STARTUP）+ POST /api/agents/sync
   + CLI `python -m app.cli agents sync`；结果 JSON（sync_id/status/discovered/changed/
   missing/pending_review/errors）；行为性变更→pending_review，缺失→stale/unavailable
   （永不删行）；agent_sync_runs 追加历史；幂等。
5. **任务路由（v1 explicit）**：提交任务可带 agent_id/project_id；
   优先级 explicit-agent > explicit-engine→默认 agent > 旧默认 jarvis；
   服务端校验（存在/available/enabled/trusted/调用模式匹配/引擎可用），不符→4xx；
   任务创建即冻结 agent 身份快照（agent_id、engine_profile_id/version/hash、
   project_id）；路由决策 JSON 落库（短、可审计、无隐藏链）；
   秘书 LLM 建议的 agent 一律服务端校验，成功仍返回 task_id。
6. **Adapter 能力面**：base.py 新增可选 discover_agents()/capabilities() 安全默认，
   旧 FakeEngine 不受影响；Jarvis 能力如实（agent_discovery=false、
   direct_profile_invocation=false，非默认 profile 显式 EngineError 绝不静默回退）；
   Codex 能力如实（default=direct、worker/explorer/自定义=delegated_only，
   不发明任何 --agent 旗标，stdin/JSONL/thread/取消/台账原样保留）。

## 硬约束（防腐化）

- 不新增 DAG、Context Bundle、Agent Session、Plan API、新秘书工具集。
- catalog 表落既有 tasks.db（幂等 add-only 迁移，先例 TaskManager.open()），不建新库。
- 项目信任：root 规范化，拒绝 symlink/`..`/盘符/UNC 逃逸；撤销信任只挡新任务，历史保留。
- 配置 §19 第一批 6 项进 settings.py + .env.example（无密钥）。
- 测试禁用 tmp_path/tempfile.mkdtemp（WinError-5），自建 tests/tmp_workspace/。
- 全量 pytest 保持绿（基线 100 passed）后才提交；Conventional Commits + 中文。
- 本地提交即可，push 由用户另行指示。
