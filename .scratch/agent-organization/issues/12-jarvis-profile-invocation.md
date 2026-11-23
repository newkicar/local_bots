# 12: Jarvis profile 调用面启用——发现面升级 + adapter 透传（local_dots 侧）

**What to build:** Jarvis 侧已完成公开面扩展（ADR-0012 同款懒导出）：`jarvis/__init__.py`
经 PEP 562 导出 `create_jarvis`、`list_profiles`、`ProfileInfo`；
`list_profiles(*, user_home, workspace)` 三层 builtin→user→project 只读发现，
解析失败传播 `ProfileError`；`create_jarvis(workspace, checkpointer, *, ...,
profile_id=None, task_id=None, ...)`——`profile_id=None` 走默认路径零行为差异，
非 None 走 ProfileRegistry + bake_spec，`task_id` 用于 memory 命名空间。
本工单只做 local_dots 侧（绝不改 Jarvis 仓库），四处落地：

1. **`app/agents/discovery.py` 升级 `JarvisDiscoveryProvider`**（现为
   `discovery_unsupported` 桩，`JARVIS_MINIMAL_API_ASK` 诉求已被 Jarvis 满足）：
   - 懒 import 公开 `list_profiles`/`ProfileInfo`（仅 `from jarvis import ...`，
     遵守 AGENTS.md 纪律：绝不 import Jarvis 内部模块）；
   - `scope="user"` 调 `list_profiles()`（不传 workspace → builtin+user 层）；
     `scope="project"` 同样如实上报可发现集合——provider 构造器可选注入
     `workspace`（缺省 None → 不含 project 层，不发明路径）；
   - 描述符映射：`agent_id=f"jarvis:{profile_id}"`、`engine="jarvis"`、
     `engine_profile_id=profile_id`；metadata 带 `layer/version/is_legacy/
     role/description/engine`；`profile_version` 标记用 `layer`+`version`
     组合（ProfileInfo 无 hash，version=None 即 legacy）；`change_kind="metadata"`；
   - `list_profiles` 传播的 `ProfileError` 不在公开导出里 → provider 宽捕获
     Exception → 返回 `DISCOVERY_ERROR` + detail（编排器既有失败隔离接管，
     绝不静默丢）；
   - 内建种子 `jarvis:default` 保留；新行初始状态走 `catalog._initial_status`
     + `auto_enable_user_profiles` 语义（默认关 → 新 user profile 首现
     pending_review 而非自动 available，与票 09 fail-closed 精神一致）；
   - 模块 docstring 同步：删"仅 create_jarvis"旧述与最小 API 诉求段。
2. **`app/adapters/jarvis.py` 调用面**：
   - 默认工厂透传：`create_jarvis(ws, saver, profile_id=<非 None/"default"
     时传 engine_profile_id 否则省略>, task_id=<task["task_id"]>)`；
     None/"default" 路径字节级不变（现有测试锁死）；
   - `capabilities()` 改报：`agent_discovery=True`、
     `direct_profile_invocation=True`、`non_default_profile_policy`
     改为 catalog 门控语义；
   - `_check_profile`：None/"default" 放行不变；非默认 `engine_profile_id`
     仅当 catalog 中存在对应 jarvis engine 行时放行，否则显式 EngineError
     （fail-closed，绝不静默回退 default）；
   - 构造器加可选 catalog 注入位（缺省 None → 非默认一律显式失败；
     `app/main.py:103` 与 `tests/acceptance_server.py` 注入 catalog）。
3. **router / main**：结构无需改——票 08 已把 `engine_profile_id/
   profile_version/profile_hash` 冻结进任务快照，路由已按 effective_status
   门控；本工单仅验证 `jarvis:<pid>` 行可路由、快照正确。
4. **Web 下拉**：已接 catalog（票 11），新 profile 同步后自动可见，仅需验收。
5. **测试**：fake provider（monkeypatch jarvis 公开面或注入）+ 描述符映射
   断言 + 默认路径回归锁 + 非 catalog pid 显式失败 + catalog 门控放行 +
   同步后 missing→stale / 新 user profile→pending_review 行为 +
   fake `agent_factory` 断言工厂入参（profile_id/task_id 传递与否）。

**Blocked by:** 无（票 09 已 done；Jarvis 侧就绪；本工单不改 Jarvis 仓库）

**Status:** ready-for-agent

- [ ] discovery：JarvisDiscoveryProvider 升级为公开 list_profiles 实调（user scope 不带 workspace）
- [ ] discovery：描述符映射 + ProfileError → DISCOVERY_ERROR（不静默丢）
- [ ] discovery：内建 jarvis:default 种子保留；新行 pending_review 语义
- [ ] adapter：工厂透传 profile_id/task_id；None/"default" 路径字节级不变（回归锁）
- [ ] adapter：capabilities 改报 + _check_profile catalog 门控 fail-closed
- [ ] router 验证 jarvis:<pid> 可路由、任务快照冻结字段正确
- [ ] 测试全绿（WinError 5 纪律：自建临时目录）+ 提交

## Comments

- 2026-11-22 建单：来自"多身份调用"需求校对汇报（jarvis 修改方案 vs
  local_dots 修改方案 之"本侧"部分）。证据（只读核对）：
  Jarvis 侧 `jarvis/__init__.py`（PEP 562 导出 create_jarvis/list_profiles/
  ProfileInfo）、`src/profiles/discovery.py:29`（ProfileInfo 字段：
  profile_id/version/is_legacy/layer/role/description/engine，无 hash）、
  `src/profiles/discovery.py:41`（list_profiles 签名与 ProfileError 传播）、
  `src/agent.py:561`（create_jarvis 全签名，profile_id=None 默认路径不变）。
  local_dots 侧 `app/agents/discovery.py:8-10,33-38,152-165`（旧桩与最小
  API 诉求注释）、`app/agents/catalog.py:143`（auto_enable_user_profiles
  已可用）、`app/agents/sync.py:57`（full_provider_set 已注册该 provider，
  升级自动生效）、`app/adapters/jarvis.py:55-81`（capabilities/_check_profile
  现状）、`app/main.py:103`（构造点）。
- 注意：`sync.py` 的 full_provider_set 与 `discovery.py` 的
  build_default_discovery 共用同一 provider 类，升级一处两处生效；
  sync 引擎级 missing 断言随之激活（jarvis profile 消失→stale，不删行）。