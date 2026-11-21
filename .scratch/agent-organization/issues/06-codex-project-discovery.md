# 06: Codex 项目级发现（<trusted>/.codex/agents）

**What to build:** 复用 04 reader，读 <trusted-project-root>/.codex/agents/*.toml；
未信任项目→不激活；项目级 agent 一律先 pending_review；agent_id 形式
codex:project:<project_id>:<name>；撤销项目信任→该项目 agent 转 stale（不删行）。

**Blocked by:** 04, 05

**Status:** done

- [x] 项目级读取 + 信任门控
- [x] 撤销信任→stale 测试
- [x] 提交
