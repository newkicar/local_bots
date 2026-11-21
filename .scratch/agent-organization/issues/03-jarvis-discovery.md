# 03: Discovery 框架 + Jarvis provider

**What to build:** app/agents/discovery.py：AgentDiscoveryService + 引擎 provider 注册表；
EngineAdapter.discover_agents(scope) 可选方法，基类安全默认 []（FakeEngine 不动）。
Jarvis provider 仅走公开 API（jarvis/__init__.py 只暴露 create_jarvis，无 list_profiles）：
provider 返回 discovery_unsupported + 记录最小 API 诉求，catalog 保留内建 jarvis:default。
绝不 import Jarvis 内部模块、绝不发明参数。

**Blocked by:** 02

**Status:** open

- [ ] AgentDiscoveryService + 注册表 + 默认 []
- [ ] Jarvis provider（unsupported 如实上报）
- [ ] 测试（含 FakeEngine 兼容）并提交
