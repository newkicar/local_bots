# 01 引擎能力矩阵（审计结论）

| 引擎 | agent 发现 | 直接指定 profile | 依据（本机实测） |
|---|---|---|---|
| Jarvis | 不支持（discovery_unsupported） | 不支持 | jarvis/__init__.py 只导出 create_jarvis；
真实签名 create_jarvis(workspace, checkpointer=None, *, model=None, store=None,
env_overrides=None, permission_state=None, mcp_tools=None)——没有 profile_id 参数，
没有 list_profiles()。 |
| Codex CLI 0.170.0 | 支持（文件发现：~/.codex/agents/*.toml + 项目 <root>/.codex/agents/*.toml，CODEX_HOME 可覆盖） | 无 --agent 旗标（-p/--profile 是配置层而非 agent） | 本机实测 codex --help；
内建 default=direct，worker/explorer=delegated_only；自定义 TOML（必填 name+
description+developer_instructions）一律 delegated_only。 |

## 向 Jarvis 仓库记录的最小 API 诉求（不阻塞本批）

1. `create_jarvis(workspace, checkpointer, *, profile_id=None)`：可选 profile 参数；
2. `list_profiles() -> list[ProfileMeta]`：公开只读发现接口。
在此之前：Jarvis adapter 能力面如实上报 agent_discovery=false /
direct_profile_invocation=false；catalog 保留内建 jarvis:default(direct)；
显式请求非默认 jarvis profile → 显式 EngineError，绝不静默回退。

## 纪律

- 绝不 import Jarvis 内部模块（adapter 是唯一接口）；
- 绝不发明 Codex 旗标（无 --agent）；~/.codex/config.toml 保持原样；
- 不读不执行用户 TOML 里的任何"指令"，仅 tomllib 解析元数据。
