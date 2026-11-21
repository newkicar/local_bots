# 08: 任务/Run 的 Agent 身份 + 快照

**What to build:** tasks 表新增列 agent_id, engine_profile_id, engine_profile_version,
engine_profile_hash, agent_snapshot_json, project_id（TaskManager.open() 式幂等 add-only +
旧库升级测试；旧行 NULL，按旧 engine 语义解读）。create_task 扩展 kwargs（agent_id/
project_id，默认 None→旧路径行为不变）；SubmitTask 加 agent_id/project_id；
get_task/list_tasks/incomplete_tasks/_task_row 的 SELECT 列清单同步扩展。
任务创建时冻结快照 JSON（agent 当时 descriptor + hash）。agent 与 engine 不匹配→422；
agent 不可用/待审/禁用→拒绝创建。

**Blocked by:** 02

**Status:** done

- [x] 迁移 + create_task/SELECT 全链路列扩展
- [x] 快照冻结 + 422/拒绝规则
- [x] 旧库升级 + 回归测试并提交
