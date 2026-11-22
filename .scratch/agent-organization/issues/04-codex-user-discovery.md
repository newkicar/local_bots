# 04: Codex 用户级发现（~/.codex/agents）

**What to build:** app/agents/codex_discovery.py：读 CODEX_HOME（默认 ~/.codex）下
agents/*.toml，仅 tomllib metadata（name/description/developer_instructions 存在性），
绝不执行；目录缺失→空结果（非错误）；坏 TOML→跳过并记录 error。自定义 agent 一律
delegated_only，agent_id 形式 codex:project:* 不适用于 user scope（user scope 用
codex:<name>）。内建 codex:default/worker/explorer 已随 02 播种，此票只补用户自定义发现。

**Blocked by:** 02

**Status:** done

- [x] reader（CODEX_HOME 尊重 + 坏文件容忍）
- [x] 结果→catalog 写入（discovered/pending_review 按自动启用配置）
- [x] 测试并提交
