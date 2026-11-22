# Local Dots Agent Organization 升级实施方案

> **目标仓库：** https://github.com/newkicar/local_bots\
> **配套执行引擎：** https://github.com/newkicar/truly-jarvis + Codex
> CLI\
> **交付对象：** Codex / Jarvis / 其他开发 Agent\
> **文档日期：** 2026-11-21\
> **目标：** 将 Local Dots 从"个人秘书 +
> 双引擎异步任务执行器"升级为"个人 AI 组织控制平面（Personal AI
> Organization Control Plane）"。\
> **核心原则：** 保留现有可运行能力；Local Dots
> 管理组织、目录、路由、编排和共享上下文；Jarvis / Codex 管理各自的
> Agent 身份、专业 Skills、内部 Memory 和 Runtime。

------------------------------------------------------------------------

# 0. 给开发 Agent 的最高优先级指令

你正在维护一个已经有真实用户、SQLite 数据和可运行功能的 Windows 本地
Python 项目。**本任务不是推倒重写。**

开始修改前，必须阅读：

1.  仓库根目录 `AGENTS.md`。
2.  仓库根目录 `CONTEXT.md`。
3.  仓库根目录 `README.md`。
4.  根目录原始设计文档 `###LocalDots｜Wi.txt`。
5.  `docs/adr/` 中全部现有 ADR。
6.  `.scratch/` 中现有阶段任务与实施记录。
7.  当前代码、测试和 Git 工作区状态。

本文基于 2026-11-21 可访问的公开 `master`
分支进行架构设计。**用户本机工作区可能比公开分支更新。** 必须以当前本机
checkout 为实际开发基线，并对照本文修正文件名、接口和实施顺序。

## 0.1 第一轮任务：审计，不改代码

先完成：

-   阅读完整仓库。
-   运行当前测试并记录基线。
-   检查 Git 状态，不覆盖用户未提交修改。
-   画出现有 Secretary → TaskManager → Dispatcher → EngineAdapter →
    Engine 的调用链。
-   检查现有 Codex CLI 版本及其实际支持的 Custom Agent 调用方式。
-   检查 Truly-Jarvis 当前实际安装版本、公开 API 和本地工作区版本。
-   确认 Jarvis Profile
    升级是否已经实现、是否仅有设计文档、是否尚未合并。
-   产出文件级实施计划、迁移计划和测试计划。

第一轮交付：

``` text
docs/
└── agent-organization/
    ├── 00-current-state-audit.md
    ├── 01-engine-capability-matrix.md
    ├── 02-agent-catalog-design.md
    ├── 03-implementation-plan.md
    └── 04-migration-and-test-plan.md
```

审计完成后，再按本文 Phase 顺序实施。不要一次性大改所有模块。

## 0.2 不可违反的现有工程纪律

必须保留并遵守当前 `AGENTS.md` 中的约束：

-   Dispatcher 只依赖 `app.adapters.base.EngineAdapter` 抽象。
-   Dispatcher 不直接导入 Jarvis 内部模块，也不直接启动 Codex CLI。
-   Jarvis Adapter 只能通过 Jarvis 的公开 API / 工厂调用，不能 import
    Jarvis 内部模块。
-   Codex Adapter 只能调用 Codex CLI 的受支持接口，不能擅自修改
    `~/.codex/config.toml`。
-   禁止 `os.chdir()` 修改进程全局工作目录。
-   所有 Workspace 显式传入 Adapter / Engine。
-   Checkpointer 由 Local Dots 注入，不使用模块级单例。
-   保持 Task / Engine Thread / Run 三层分离。
-   `engine_thread_id` 建立后立即持久化。
-   同一 Task / Thread 的并发执行必须串行化。
-   取消、审批、重启恢复和副作用台账不得退化。
-   默认监听 `127.0.0.1`，不得因增加 Agent API 改成公网监听。
-   API Token、Codex 凭据、模型网关凭据不能进入日志、前端或 Catalog。

## 0.3 本次不做的事

-   不复制 Jarvis 的 Profile、Memory、Skill 系统。
-   不复制 Codex 的 Custom Agent 配置。
-   不创建每个 Agent 一个常驻 Python 进程。
-   不让 Agent 之间绕过 Local Dots 随意互相调用。
-   不在第一阶段引入 Redis、消息队列、向量数据库或微服务。
-   不一次性把所有任务改成复杂 DAG 工作流。
-   不把所有临时 SubAgent 注册成长期 Agent。
-   不自动修改用户的 Codex 配置文件。
-   不在没有真实支持的情况下伪造 Codex CLI 的 `--agent` 参数。

------------------------------------------------------------------------

# 1. 项目定位与目标架构

## 1.1 Local Dots 的定位变化

当前 Local Dots V1.0 的核心是：

-   FastAPI 本地服务和 Web UI。
-   Secretary 自然语言交互。
-   Codex / Jarvis 两个 Engine Adapter。
-   SQLite Task / Run 持久化。
-   异步 Dispatcher。
-   Approval Gate、取消、恢复、通知。
-   跨对话秘书记忆。

升级后，Local Dots 的定位变为：

**个人 AI 组织控制平面（Personal AI Organization Control Plane）。**

它管理多个不同引擎中的逻辑 Agent，理解每个 Agent 的能力，选择合适的
Agent，组织任务分工，调度执行，管理共享上下文，并验收交付。

Local Dots 不负责某个 Agent 内部如何思考、如何使用
Skill、如何维护专业记忆。

## 1.2 三层职责

``` text
Thomas / User
      |
      v
Local Dots
  - Secretary / Chief of Staff
  - Agent Discovery & Catalog
  - Agent Router
  - Task Planner
  - Dispatcher
  - Approval / Policy
  - Shared Context / Project Memory
  - Result Review / Artifacts
      |
      +--------------------------+
      |                          |
      v                          v
Truly-Jarvis Runtime          Codex Runtime
  - Agent Profiles              - Custom Agents
  - Agent Memory                 - Custom instructions
  - Skills                       - Skills
  - Tools / Middleware           - CLI / Subagent runtime
  - Permissions                  - Sandbox / approvals
  - Worker Runtime               - Worker / subagent
```

## 1.3 责任归属矩阵

  ------------------------------------------------------------------------
  能力                    权威归属                Local Dots 的职责
  ----------------------- ----------------------- ------------------------
  Jarvis Agent Profile    Truly-Jarvis            发现、引用、路由

  Jarvis Agent Memory     Truly-Jarvis            不复制；可接收候选状态

  Jarvis Agent Skills     Truly-Jarvis            不复制；展示摘要

  Jarvis Runtime          Truly-Jarvis            通过公开 Adapter 调用

  Codex Custom Agent 定义 Codex 配置体系          发现、识别调用模式

  Codex Skills            Codex 配置体系          不复制

  Codex CLI Runtime       Codex CLI               通过 Codex Adapter 调用

  Secretary Memory        Local Dots              管理

  User Shared Memory      Local Dots              管理

  Project Shared Context  Local Dots              管理

  Agent Catalog           Local Dots              管理统一索引

  Task / Run / Plan       Local Dots              管理

  跨 Agent 编排           Local Dots              管理

  跨引擎审批和审计        Local Dots              管理外层策略

  Agent 内部工具权限      对应 Runtime            不绕过；与 Local Dots
                                                  外层策略叠加
  ------------------------------------------------------------------------

## 1.4 核心架构原则

1.  Agent Profile 属于 Engine，不属于 Local Dots。
2.  Agent Catalog 属于 Local Dots，是外部代理的统一索引，不是第二套
    Profile。
3.  Agent 是长期逻辑身份；Worker 是一次执行实例。
4.  Task 是业务任务；Thread 是引擎会话；Run 是一次执行。
5.  Local Dots 的 Task Workspace 不等于 Project Root。
6.  Local Dots Shared Memory 不等于 Agent Memory。
7.  Adapter 是 Local Dots 与 Engine 之间唯一的执行边界。
8.  Agent Discovery 不等于自动授予权限。
9.  Agent 能力标签用于路由，不是安全权限。
10. 先实现可解释、可追踪的确定性路由，再逐步开放自动规划。

------------------------------------------------------------------------

# 2. 当前代码基线与需要保护的资产

开发 Agent 必须核实本机实际代码。公开 master
分支当前可以看到以下结构与行为。

## 2.1 当前关键模块

  --------------------------------------------------------------------------------------------------------------------------------------------------
  当前文件                                                         现有职责                                   本次改造原则
  ---------------------------------------------------------------- ------------------------------------------ --------------------------------------
  `app/main.py`                                                    FastAPI 入口、依赖装配、API、lifespan      增加 Discovery / Catalog / Agent
                                                                                                              API，避免继续堆业务逻辑

  `app/task_manager.py`                                            SQLite Task / Run、状态机、通知            扩展身份快照和后续 Plan
                                                                                                              元数据，保留状态机

  `app/dispatcher.py`                                              异步队列、并发限制、同 Task 串行、Run 驱动 保持 Engine 无关；只从 Task 的 engine
                                                                                                              选择 Adapter

  `app/adapters/base.py`                                           `EngineAdapter`、`RunHooks`、异常协议      扩展可选 Discovery / Capability
                                                                                                              协议，保持兼容

  `app/adapters/jarvis.py`                                         调用 Jarvis 公开工厂、注入 Checkpointer    增加 Profile ID / Context Bundle
                                                                                                              参数，但只调用 Jarvis 公共 API

  `app/adapters/codex.py`                                          启动 Codex CLI、解析 JSON                  在核实 CLI 能力后增加 Custom Agent /
                                                                   事件流、取消子进程                         delegation 支持

  `app/secretary.py`                                               秘书                                       从 Engine 选择升级为 Agent
                                                                   Prompt、任务工具、文件只读发现、对话记忆   目录查询和身份路由

  `app/memory.py`                                                  当前跨对话 JSON 记忆                       保留为 Secretary Memory；新增 Shared
                                                                                                              Memory 时不得混用

  `app/settings.py`                                                `.env` 配置、路径、模型、并发等            增加
                                                                                                              Catalog、Discovery、Routing、Project
                                                                                                              Trust 配置

  `app/conversations.py`                                           Secretary Conversation 持久化              保留；不要与 Engine Thread 混淆

  `app/followup.py`                                                任务事件回推秘书对话                       保留；增加 Agent / Plan 元数据

  `app/permissions.py`、`app/approval.py`、`app/auto_approve.py`   外层策略与审批                             作为新编排系统的安全边界继续使用

  `app/reconcile.py`                                               服务重启时核对运行状态                     保留并覆盖新增 Agent 元数据

  `web/`                                                           原生 HTML / CSS / JS                       增加 Agent 目录、选择器和同步状态

  `tests/`                                                         当前回归测试                               扩展 Catalog、Adapter、路由和 DAG 测试
  --------------------------------------------------------------------------------------------------------------------------------------------------

## 2.2 当前任务数据模型

公开 master 的 `tasks` 表已有：

``` text
task_id
seq
engine
engine_thread_id
workspace
writes_authorized
status
parent_task_id
instruction
result
error
reject_reason
created_at
updated_at
```

`runs` 表独立记录每次执行，包括：

``` text
run_id
task_id
attempt
state
origin
queue_position
started_at
finished_at
result
error
side_effects
pending_op
wait_reason
wait_requested_at
decision
created_at
```

本次要扩展身份快照，但不能破坏现有字段或旧库数据。

## 2.3 当前 Adapter 约束

当前 `EngineAdapter` 主要提供：

``` python
ensure_thread(...)
execute(...)
reconcile_running(...)
```

`RunHooks` 负责：

-   `thread_bound()`：Thread 建立后立即写库。
-   `side_effect()`：追加副作用台账。
-   `request_operation()`：敏感操作预审批。

Dispatcher 当前通过 `task["engine"]` 找到对应
Adapter。这个结构应保留，不要让 Dispatcher 直接知道 `jarvis_profile_id`
或 Codex TOML 文件格式。

## 2.4 当前 Secretary 的行为需要兼容

当前 Secretary 的工具包括：

-   `submit_task`
-   `start_agent_session`
-   `list_tasks`
-   `get_task`
-   `followup_task`
-   `cancel_task`
-   只读文件发现工具

当前路由规则在领域文档中约定为：默认 Jarvis；只有用户明确要求 Codex
时才选择 Codex。

**本次第一阶段必须保留这个默认规则。** 新的 Agent
自动路由先以显式配置或建议模式启用，不能静默改变用户已经习惯的行为。

------------------------------------------------------------------------

# 3. 目标目录结构

建议结构如下，具体按仓库现有组织调整：

``` text
app/
├── main.py
├── settings.py
├── secretary.py
├── task_manager.py
├── dispatcher.py
├── memory.py
├── conversations.py
├── followup.py
├── permissions.py
├── approval.py
├── auto_approve.py
├── reconcile.py
│
├── agents/
│   ├── __init__.py
│   ├── models.py
│   ├── catalog.py
│   ├── discovery.py
│   ├── router.py
│   ├── sync.py
│   └── project_trust.py
│
├── context/
│   ├── __init__.py
│   ├── bundle.py
│   └── shared_memory.py
│
├── planning/
│   ├── __init__.py
│   ├── models.py
│   ├── plan_manager.py
│   └── dependency_resolver.py
│
└── adapters/
    ├── base.py
    ├── codex.py
    └── jarvis.py

data/
├── tasks.db
├── checkpoints.db
├── conversations.db
├── memory.json
└── agent_catalog.db  # 可选：也可复用 tasks.db，需由审计决定

docs/
└── agent-organization/

tests/
├── test_agent_catalog.py
├── test_agent_discovery.py
├── test_agent_router.py
├── test_adapter_agent_selection.py
├── test_context_bundle.py
├── test_task_plans.py
└── ...
```

**不要为了照搬目录结构而过度拆分。** 如果当前仓库更适合将 Catalog /
Discovery 合并到一个模块，遵循现有风格。

建议第一阶段先复用
`tasks.db`，减少数据库连接和迁移复杂度。只有明确需要隔离 Catalog
生命周期时才单独创建 `agent_catalog.db`。

------------------------------------------------------------------------

# 4. Agent Catalog：统一代理目录

## 4.1 定位

Local Dots 不创建和维护 Jarvis / Codex 的完整 Agent Profile。

它只保存一份规范化的代理目录：

-   这个 Agent 来自哪个 Engine。
-   对应 Engine 的 Profile / Agent ID。
-   名称和描述。
-   能力标签。
-   配置来源和作用域。
-   调用模式。
-   版本和配置 Hash。
-   是否发现、待审核、可用、禁用或失效。
-   Local Dots 用户侧的别名、优先级和启用设置。

## 4.2 稳定 ID

建议：

``` text
jarvis:researcher
jarvis:developer
jarvis:reviewer

codex:developer
codex:reviewer
codex:explorer
```

项目级代理需要加入项目作用域，避免同名冲突：

``` text
codex:project:<project_id>:frontend
codex:project:<project_id>:backend
```

Catalog 的 `agent_id` 是 Local Dots 自己的稳定 ID；`engine_profile_id`
是引擎原生 ID。

## 4.3 AgentDescriptor 统一数据结构

建议 Pydantic 模型或 dataclass：

``` python
class AgentDescriptor:
    agent_id: str
    engine: str
    engine_profile_id: str

    name: str
    description: str

    source_scope: str       # user / project / builtin
    source_id: str | None
    source_path: str | None

    capabilities: list[str]
    invocation_mode: str    # direct / delegated_only / default / metadata_only

    profile_version: str | None
    profile_hash: str

    discovery_status: str   # discovered / pending_review / available / disabled / stale / unavailable
    trust_state: str        # trusted / pending / rejected

    metadata: dict
```

字段只是目标 Schema，需根据仓库风格实现。

## 4.4 不要把源配置和 Local Dots 覆盖设置混在一起

建议分开：

**Provider Snapshot（引擎发现的快照）**

-   名称。
-   描述。
-   Profile ID。
-   Source Path。
-   Version / Hash。
-   原生配置摘要。
-   Invocation Mode。

**Local Override（Local Dots 用户侧覆盖）**

-   是否启用。
-   用户别名。
-   用户补充能力标签。
-   路由优先级。
-   是否允许自动路由。
-   用户审核状态。

同步时更新 Provider Snapshot，不能覆盖 Local Override。

## 4.5 数据库建议

``` sql
CREATE TABLE IF NOT EXISTS agent_catalog (
    agent_id TEXT PRIMARY KEY,
    engine TEXT NOT NULL,
    engine_profile_id TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    source_id TEXT,
    source_path TEXT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    invocation_mode TEXT NOT NULL DEFAULT 'metadata_only',
    profile_version TEXT,
    profile_hash TEXT NOT NULL,
    discovery_status TEXT NOT NULL,
    trust_state TEXT NOT NULL,
    provider_metadata_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(engine, engine_profile_id, source_scope, source_id)
);

CREATE TABLE IF NOT EXISTS agent_overrides (
    agent_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    alias TEXT,
    extra_capabilities_json TEXT NOT NULL DEFAULT '[]',
    routing_priority INTEGER NOT NULL DEFAULT 100,
    auto_route_allowed INTEGER NOT NULL DEFAULT 0,
    approval_state TEXT NOT NULL DEFAULT 'pending',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agent_catalog(agent_id)
);

CREATE TABLE IF NOT EXISTS agent_sync_runs (
    sync_id TEXT PRIMARY KEY,
    engine TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
```

SQL 只是逻辑设计，必须根据现有 SQLite 迁移方式调整。SQLite
外键需确认连接是否启用 `PRAGMA foreign_keys=ON`。

## 4.6 Catalog 状态

建议：

  状态               含义                       能否被 Router 调用
  ------------------ -------------------------- --------------------
  `discovered`       首次发现，未审核           否
  `pending_review`   配置变化或信任待确认       否
  `available`        已通过验证且启用           是
  `disabled`         用户主动停用               否
  `stale`            来源暂时不可读或同步过期   默认否
  `unavailable`      Engine / Profile 不可用    否
  `rejected`         用户拒绝                   否

自动发现只负责登记，不等于自动激活。

对已信任的用户级 Profile，可根据用户策略自动激活；项目级新 Agent
默认必须审核。

## 4.7 变化检测

同步时：

1.  发现新 Agent。
2.  解析并校验元数据。
3.  计算 Profile Hash。
4.  与 Catalog 现有记录比较。
5.  仅元数据变化时更新快照。
6.  指令、模型、工具策略、来源路径等可影响执行行为的配置变化，标记
    `pending_review`。
7.  不再发现的 Agent 标记 `unavailable` 或 `stale`，不能直接删除。
8.  历史 Task 的 Agent 快照永远保留。

不得在同步时自动覆盖用户的 alias、priority、enabled 等 Local Override。

------------------------------------------------------------------------

# 5. Agent Discovery：自动发现与同步

## 5.1 架构

``` text
AgentDiscoveryService
        |
        +-- JarvisDiscoveryProvider
        |
        +-- CodexDiscoveryProvider
        |
        +-- BuiltinAgentProvider
        |
        v
Descriptor Validator
        |
        v
Trust / Scope Checker
        |
        v
Profile Hash Calculator
        |
        v
Catalog Sync
        |
        v
Agent Catalog
```

每个 Engine 自己实现发现逻辑。Local Dots
不直接解析所有引擎的内部运行时。

## 5.2 Jarvis Discovery

目标：Jarvis 通过**公开 API**提供 Profile 元数据。

建议 Truly-Jarvis 公开接口：

``` python
from jarvis import list_profiles

profiles = list_profiles()
```

每个 Profile 返回最少：

``` json
{
  "id": "developer",
  "name": "Developer",
  "description": "Software development and code maintenance",
  "capabilities": ["python", "coding", "testing"],
  "version": "1",
  "profile_hash": "...",
  "enabled": true,
  "scope": "user"
}
```

具体函数名由 Jarvis 项目最终确定，但必须形成稳定公共 API。

Local Dots 的 `JarvisAdapter`：

-   只调用 `jarvis` 包公开 API。
-   不 import `jarvis.agent_profiles`、`jarvis.internal.*` 等内部模块。
-   Jarvis 未实现 Discovery API 时，明确报告 `discovery_unsupported`。
-   不要通过扫描 Jarvis 内部 Python 源码猜 Profile。
-   可在未来支持公开、版本化的 manifest 文件作为兼容发现方式。

执行时目标接口：

``` python
create_jarvis(
    workspace=task["workspace"],
    checkpointer=checkpointer,
    profile_id=task["engine_profile_id"],
    context_bundle=task.get("context_bundle"),
)
```

必须先确认 Jarvis 实际实现了这些参数；未实现时不能传入虚构参数。

## 5.3 Codex Discovery

用户级自定义子代理候选目录：

``` text
~/.codex/agents/*.toml
```

项目级自定义子代理候选目录：

``` text
<trusted-project-root>/.codex/agents/*.toml
```

还要考虑 `CODEX_HOME` 等 Codex
环境配置对用户目录的影响。路径规则必须按目标机器安装的 Codex CLI
版本和官方文档验证。

Codex 自定义 Agent 文件常见元数据包括：

-   `name`
-   `description`
-   `developer_instructions`
-   可选模型 / reasoning 配置及其他受支持字段

建议使用 Python 3.12 标准库 `tomllib` 读取 TOML。只读取元数据，不执行
TOML 文件中的任何代码。

### 极其重要：区分 Codex Custom Agent 的调用模式

Codex Custom Agents 主要用于 Codex 内部的 SubAgent /
Delegation。**不能假定 `codex exec` 可以通过一个未经验证的
`--agent <name>` 参数，直接把某个 Custom Agent 当作顶层执行身份。**

发现结果必须包含：

``` text
invocation_mode:
- direct
- delegated_only
- default
- metadata_only
```

规则：

1.  如果目标 Codex CLI 有正式支持的直接调用机制，并经过集成测试，才标记
    `direct`。
2.  如果该 Custom Agent 只能由 Codex 主代理内部委派，标记
    `delegated_only`。
3.  `delegated_only` 代理可以显示在目录中，但不能被 Local Dots
    假装成独立的 Codex 顶层 Worker。
4.  Local Dots 可以把任务交给 Codex
    主代理，并在任务指令中明确要求它委派给指定 Custom Agent；但此时
    Local Dots 跟踪的是 Codex 主 Run，而不是独立跟踪该 SubAgent
    的生命周期。
5.  如果当前 `codex exec` 运行模式无法暴露或使用 SubAgent
    能力，必须明确标记为不支持，不能静默假装已经调用指定角色。
6.  不要修改
    `~/.codex/config.toml`；不要为了支持目录发现而重写用户配置。
7.  对项目级 `.codex/agents/` 只允许在用户明确标记为 trusted
    的项目中发现和使用。

### Codex 能力映射

Codex TOML 的 `description` 可以作为路由参考；不要假设它有 Local Dots
专用的 `capabilities` 字段。

能力标签来源优先级：

1.  Local Dots 用户覆盖（`agent_overrides.extra_capabilities_json`）。
2.  Engine 公开 Discovery API 中明确提供的能力标签。
3.  由用户审核的本地映射文件。

不能因为 LLM 从描述中猜测出某个能力，就把它当作已验证能力或安全权限。

## 5.4 内置代理

可发现并展示 Engine 的内置 Agent，例如 Codex 的默认 / worker /
explorer（具体名称按当前 CLI 版本核实）。

内置代理也必须记录调用模式。不能假定内置角色都可以由 Local Dots
直接启动。

## 5.5 同步触发方式

第一版实现三种：

1.  Local Dots 启动时同步一次。
2.  API 手动触发同步。
3.  CLI 手动触发同步。

建议命令：

``` powershell
python -m app.cli agents list
python -m app.cli agents sync
python -m app.cli agents inspect jarvis:developer
```

命令名可服从现有 CLI 体系；若当前没有
CLI，不必为了几个命令强行引入复杂框架。

**第一版不做 Windows 文件系统 Watcher。** 文件监控在
Windows、网络盘、编辑器原子替换等场景容易出现重复事件和漏事件。先用启动同步 +
手动同步，后续再加低频轮询或 Watcher。

## 5.6 同步 API

建议：

``` text
GET  /api/agents
GET  /api/agents/{agent_id}
POST /api/agents/sync
POST /api/agents/{agent_id}/enable
POST /api/agents/{agent_id}/disable
POST /api/agents/{agent_id}/approve
```

同步 API 应返回：

``` json
{
  "sync_id": "...",
  "status": "completed",
  "discovered": 3,
  "changed": 1,
  "missing": 0,
  "pending_review": 1,
  "errors": []
}
```

所有接口沿用当前 Bearer Token 认证。不要在前端或日志暴露 Profile
原始敏感配置。

------------------------------------------------------------------------

# 6. Project Trust：项目级 Agent 的安全边界

## 6.1 为什么必须单独做

项目级 `.codex/agents/` 可能来自 Git 仓库。仓库中的 Agent 配置可以影响
Agent 行为。

不能因为用户把某个目录作为任务 Workspace，就默认信任其中所有 Agent
配置。

## 6.2 Project Root 与 Task Workspace 必须分离

当前 Workspace 通常是：

``` text
workspace/<task_id>/
```

它是一次任务的工作目录。

Project Root 是用户真实项目目录，例如：

``` text
D:/work/my-project/
```

两者不是一个概念。

建议增加 Project Registry：

``` sql
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    canonical_root TEXT NOT NULL,
    trust_state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Task 增加 `project_id`，但保留独立的 `workspace`。

## 6.3 Trust 规则

-   用户级 Profile 来源于用户自己的 Agent 目录。
-   项目级 Profile 仅扫描已登记且 `trusted` 的 Project Root。
-   项目根路径必须 canonicalize。
-   检查符号链接和路径穿越。
-   不跟随超出项目根的链接扫描。
-   新项目级 Agent 默认 `pending_review`。
-   项目文件不能扩大 Local Dots 全局权限。
-   Agent 描述和能力标签不能代替实际权限检查。
-   用户取消项目信任后，项目 Agent 不再允许新任务使用；历史 Task
    记录保留。

------------------------------------------------------------------------

# 7. Agent Router：从 Engine Routing 升级为 Agent Routing

## 7.1 目标

当前任务请求主要选择：

``` text
engine = codex / jarvis
```

升级后优先选择：

``` text
agent_id = jarvis:developer
```

再从 Catalog 得到：

``` text
engine = jarvis
engine_profile_id = developer
```

Engine 仍然保留，是为了 Adapter 路由和向后兼容。

## 7.2 路由优先级

``` text
1. 用户明确指定 Agent
2. 用户明确指定 Engine → 对应 Engine Default Agent
3. 用户允许自动路由 → Agent Router 根据能力选择
4. 否则保持旧默认规则（Jarvis）
```

第一版建议：

``` text
LD_AGENT_ROUTING_MODE = explicit
```

后续可支持：

-   `explicit`：只使用用户明确指定的 Agent / Engine。
-   `suggest`：Secretary 推荐 Agent，必要时询问用户。
-   `auto`：Secretary 自动选择已启用且可直接调用的 Agent。

**不得在第一阶段静默把旧的默认 Jarvis / 明确 Codex
规则改成自由自动路由。**

## 7.3 Router 选择过程

``` text
User Request
    |
    v
Intent / Task Type
    |
    v
Explicit Agent or Engine?
    |
    +-- Yes --> Resolve Catalog Entry
    |
    +-- No --> Check Routing Mode
                   |
                   +-- explicit --> Legacy Default
                   |
                   +-- suggest --> Suggest Candidate
                   |
                   +-- auto --> Match Capabilities
                                   |
                                   v
                            Validate Availability
                                   |
                                   v
                            Validate Invocation Mode
                                   |
                                   v
                            Validate Trust / Approval
                                   |
                                   v
                              Selected Agent
```

LLM 可以提出候选 Agent，但最终选择必须由服务端校验：

-   Agent 存在。
-   `available`。
-   已启用。
-   已通过信任审核。
-   调用模式适合当前任务。
-   对应 Engine 可用。
-   当前用户权限允许。

## 7.4 Router 结果必须可解释

保存：

``` json
{
  "selected_agent_id": "jarvis:developer",
  "engine": "jarvis",
  "engine_profile_id": "developer",
  "routing_mode": "auto",
  "reason": "任务涉及 Python 代码修改，匹配 developer 的能力标签",
  "alternatives": ["codex:developer"],
  "user_explicit_choice": false
}
```

不要保存隐藏推理链。只记录简短、可审计的路由理由。

------------------------------------------------------------------------

# 8. Task / Run 数据模型升级

## 8.1 Task 新增字段

建议：

``` text
agent_id
engine_profile_id
engine_profile_version
engine_profile_hash
agent_snapshot_json
project_id
plan_id
context_bundle_id
```

继续保留：

``` text
task_id
engine
engine_thread_id
workspace
writes_authorized
status
parent_task_id
instruction
result
error
reject_reason
created_at
updated_at
```

其中：

-   `engine`：Adapter 路由所需，保留。
-   `agent_id`：Local Dots Catalog 稳定 ID。
-   `engine_profile_id`：Engine 内部 Profile / Agent ID。
-   `engine_profile_version/hash`：配置快照。
-   `agent_snapshot_json`：任务创建时必要的描述、调用模式和路由元数据。
-   `project_id`：关联真实项目根目录。
-   `plan_id`：未来工作计划。
-   `context_bundle_id`：未来共享上下文引用。

## 8.2 Task 创建 API 兼容

现有 API：

``` json
{
  "instruction": "Review this project",
  "engine": "jarvis",
  "workspace": "D:/work",
  "writes_authorized": false
}
```

继续有效。

新增：

``` json
{
  "instruction": "Review this project",
  "agent_id": "jarvis:developer",
  "project_id": "project-123",
  "workspace": "D:/localdots/workspace/T123",
  "writes_authorized": false
}
```

兼容规则：

-   传 `agent_id` 时，从 Catalog 解析 Engine 和 Profile。
-   同时传 `agent_id` 和 `engine` 时必须校验一致，不一致返回 422。
-   只传旧 `engine` 时映射到该 Engine 的 Default Agent。
-   不传两者时保持旧默认 Jarvis。
-   未启用、待审核、失效的 Agent 不得创建新任务。
-   任务创建时固定 Agent 快照；后续 Profile 改动不能悄悄改变已有 Task。
-   历史 Task 缺少新字段时按旧 `engine` 解释，不要求重写历史记录。

## 8.3 SQLite 迁移

遵循当前 `TaskManager.open()` 中幂等迁移的模式，或在审计后采用更清晰的
`schema_migrations`。

要求：

-   先备份 `tasks.db`。
-   使用 `PRAGMA table_info` 检查字段。
-   只增加列，不重建旧表，除非确有必要。
-   对旧记录填入可解释的兼容默认值。
-   迁移后旧 Task、Run、Notification、Side-Effect Ledger 仍可读取。
-   测试从真实旧 Schema 创建的数据库升级。
-   迁移必须可重复执行。

## 8.4 Agent 快照冻结

Task 创建时保存：

-   `agent_id`
-   `engine`
-   `engine_profile_id`
-   `profile_version`
-   `profile_hash`
-   `invocation_mode`
-   `project_id`
-   `workspace`

之后 Agent 配置变化只影响新 Task。历史 Task
的详情仍能看到当时使用的身份。

------------------------------------------------------------------------

# 9. Adapter 协议升级

## 9.1 保持 Adapter 边界

Dispatcher 继续只依赖：

``` python
EngineAdapter
```

不得在 Dispatcher 中写：

``` python
if engine == "jarvis":
    ...
elif engine == "codex":
    ...
```

也不得在 Dispatcher 中解析 Profile。

## 9.2 可选 Discovery 接口

建议在 `EngineAdapter` 增加非强制能力：

``` python
async def discover_agents(self, scope) -> list[AgentDescriptor]:
    return []
```

或者由独立的 Discovery Provider 注册，具体取决于当前代码结构。

老的测试假 Adapter 不应该因为新增 Discovery
方法而全部失效。可以使用默认实现或独立协议。

## 9.3 Adapter Capabilities

建议每个 Adapter 声明：

``` json
{
  "engine": "jarvis",
  "agent_discovery": true,
  "direct_profile_invocation": true,
  "delegated_agent_invocation": false,
  "profile_version_pinning": true,
  "context_bundle": true,
  "resume": true,
  "approval": true
}
```

Codex 的能力必须根据实际 CLI 版本和集成测试填写，不能照抄 Jarvis。

## 9.4 Jarvis Adapter

目标：

-   `discover_agents()` 调用 Jarvis 公开 Profile Discovery API。
-   执行时把 `engine_profile_id` 传给 Jarvis 公开工厂。
-   把 Task Workspace、Checkpointer、Context Bundle 显式传入。
-   不直接操作 Jarvis 的 Profile 目录或 Memory 文件。
-   Jarvis 旧 API 未升级时，保留 default Profile 的旧调用路径。
-   如果指定 Profile 不受当前 Jarvis API 支持，明确失败，不能静默退回
    default。

目标接口意图：

``` python
agent = await create_jarvis(
    workspace=task["workspace"],
    checkpointer=saver,
    profile_id=task["engine_profile_id"],
    context_bundle=context_bundle,
)
```

具体签名以 Truly-Jarvis 公共 API 为准。

## 9.5 Codex Adapter

当前 Codex Adapter 通过 CLI 子进程执行，并解析 JSON 事件流。

新增能力时：

-   先读取当前 CLI 的 `--help` 和官方配置说明。
-   检查当前 Custom Agent 能否直接作为顶层执行身份。
-   如果只能通过父 Codex 委派，则标记 `delegated_only`。
-   不发明 `--agent` 参数。
-   不修改 `~/.codex/config.toml`。
-   不覆盖用户原有 Provider、MCP、Sandbox、Approval 配置。
-   只传递经确认受支持的 CLI 参数。
-   保留现有 stdin Prompt、JSON 事件流、Thread 绑定、取消进程树和
    Side-Effect Ledger。
-   继续保留 `--ephemeral` 的现有设计和 resume 约束，不能破坏用户 Codex
    会话。

## 9.6 Context Bundle 传递

Adapter 不能随意把 Context Bundle 拼成系统指令。

应把它作为有来源的参考数据传入：

-   Jarvis：使用 Jarvis 公开 API 支持的结构化 Context 参数。
-   Codex：使用经过验证的任务上下文文件或明确的任务 Prompt
    区块；标注为参考资料，不允许覆盖 Agent Instructions / 安全规则。
-   不支持 Context Bundle 的
    Engine：明确标记能力缺失，不得假装已经注入。

------------------------------------------------------------------------

# 10. Secretary 升级为 Chief of Staff

## 10.1 定位

Secretary 仍是 Local Dots 的主代理，不应简单变成普通 Jarvis Profile。

它负责：

1.  理解用户目标。
2.  判断任务类型和复杂度。
3.  查询 Agent Catalog。
4.  推荐或选择 Agent。
5.  创建 Task 或 Task Plan。
6.  委派并立即返回。
7.  监控任务状态。
8.  处理失败、审批和异常。
9.  验收结果。
10. 将重要信息写入正确的 Memory Namespace。

## 10.2 新增 Secretary 工具

建议增加：

``` text
list_agents
get_agent
recommend_agent
sync_agents
```

其中：

-   `list_agents`：只列出可用或待审核 Agent。
-   `get_agent`：查看描述、能力、来源、调用模式、状态。
-   `recommend_agent`：按任务描述给出候选，不直接创建任务。
-   `sync_agents`：触发发现同步；建议仅允许用户显式要求或系统管理入口调用，避免
    LLM 随意扫描磁盘。

现有任务工具继续保留：

-   `submit_task`
-   `start_agent_session`
-   `list_tasks`
-   `get_task`
-   `followup_task`
-   `cancel_task`

## 10.3 新 Prompt 行为

Secretary 应知道：

-   不要仅凭 Agent 名称猜测能力。
-   先查 Catalog。
-   不要选择 `pending_review` / `disabled` / `unavailable` Agent。
-   不要把 `delegated_only` Agent 当成独立可执行 Worker。
-   用户明确指定 Agent 时尊重用户选择。
-   用户明确指定 Codex / Jarvis 时保持 Engine 选择。
-   自动路由只在配置开启时执行。
-   任务创建后立即返回 Task ID，不等待 Worker 完成。
-   复杂任务可以创建 Plan，但必须给出清晰的任务边界和验收标准。

## 10.4 Planner / Critic 的实现方式

第一阶段不必把 Planner、Critic 做成永久 Profile。

先让 Secretary 在单次对话中完成：

-   计划草拟。
-   任务拆解。
-   结果验收。

当真实使用证明内部步骤不够，再考虑独立 Reviewer Profile 或独立 Critic
Worker。

------------------------------------------------------------------------

# 11. Memory：Local Dots 与 Engine 的记忆归属

## 11.1 必须区分的记忆

  ----------------------------------------------------------------------------------
  Memory                  唯一权威归属            用途
  ----------------------- ----------------------- ----------------------------------
  Secretary Memory        Local Dots              秘书学到的协作方式、长期工作习惯

  User Shared Memory      Local Dots              用户长期偏好和明确授权共享的信息

  Project Shared Memory   Local Dots              跨 Engine 的项目背景、决策和约束

  Jarvis Agent Memory     Truly-Jarvis            每个 Jarvis Profile 的专业经验

  Codex Agent / Session   Codex                   Codex 自己的 Agent 配置与会话状态
  Context                                         

  Task Memory             Local Dots Task /       临时任务中间结果
                          Workspace               

  Checkpoint              对应 Engine             执行状态恢复
  ----------------------------------------------------------------------------------

## 11.2 当前 `app/memory.py` 的处理

当前 `MemoryStore` 使用 JSON 文件保存跨对话秘书记忆。

**它应继续作为 Secretary Memory，不要直接改名为 User Shared
Memory，也不要把它全部注入每个 Worker。**

先保留原行为和数据格式。

未来新增：

``` text
User Shared Memory
Project Shared Memory
Memory Candidate Inbox
```

这些是独立命名空间。

## 11.3 Context Bundle

Local Dots 向 Worker 提供经过授权的上下文快照：

``` json
{
  "context_bundle_id": "ctx-123",
  "user_context": {
    "preferences": []
  },
  "project_context": {
    "project_id": "project-123",
    "background": [],
    "decisions": [],
    "constraints": []
  },
  "task_context": {
    "objective": "Review the Python project",
    "acceptance_criteria": [],
    "constraints": []
  },
  "provenance": [],
  "created_at": "ISO-8601"
}
```

要求：

-   只注入当前任务有权访问的内容。
-   每条重要信息保留来源。
-   明确区分用户事实、项目事实、Agent 输出和待验证信息。
-   限制总长度 / Token 预算。
-   不包含 API Key、密码、凭证。
-   以只读快照传递。
-   Worker 不得通过修改 Context Bundle 反向修改 Shared Memory。

## 11.4 Memory Candidate

Agent 可以向 Local Dots 报告候选经验，但不应让 Local Dots 直接修改
Jarvis Memory 文件。

推荐流程：

``` text
Jarvis Worker
    |
    v
Jarvis Agent Memory Candidate
    |
    v
Jarvis Public API / Adapter
    |
    v
Local Dots Candidate Inbox
    |
    v
User / Secretary Review
    |
    v
Promote Request to Jarvis
    |
    v
Jarvis updates its own Memory
```

若 Jarvis 暂时没有 Memory Candidate 公共 API，先将候选作为 Task Artifact
保存；不要直接读写 Jarvis 内部文件。

Codex 如果没有相应 Memory
API，也不应伪装有独立持久记忆。先通过其原生配置、Skill 和会话机制工作。

------------------------------------------------------------------------

# 12. Task Planner：从父子任务升级为任务依赖图

此功能不是 V1.1 的前置。应在 Agent Catalog 和 Profile Routing
稳定后实施。

## 12.1 现有 `parent_task_id` 的边界

`parent_task_id` 表示父子层级，不等于依赖关系。

不要把它同时解释为"必须等待某个任务完成"。

## 12.2 新增 Plan 与依赖表

建议：

``` sql
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    dependency_type TEXT NOT NULL DEFAULT 'success',
    created_at TEXT NOT NULL,
    PRIMARY KEY(task_id, depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT,
    relative_path TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    checksum TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL
);
```

实际 Schema 需服从现有数据库迁移体系。

## 12.3 任务 DAG 规则

-   `parent_task_id`：表示层级归属。
-   `task_dependencies`：表示必须等待哪些 Task。
-   Task 只有在所有依赖满足条件后才进入可执行队列。
-   默认依赖类型为 `success`。
-   上游失败时，下游保持阻塞并通知 Secretary，不自动执行。
-   允许 `continue_on_failure` 必须显式配置。
-   检测循环依赖，创建 Plan 时拒绝有环图。
-   任务完成后检查下游是否可以入队。
-   继续使用现有 Dispatcher 和 TaskManager 状态机。

第一版可以不增加 `blocked` Task 状态，而是在队列查询中排除依赖未完成的
Task，并在 API 返回 `blocked_by`。若 UI 需要更清晰状态，再通过 ADR
决定是否增加新状态。

## 12.4 Artifact 规则

-   Artifact 必须归属于 Task / Run。
-   默认只能登记 Workspace 内文件。
-   保存相对路径、大小、Hash、类型和说明。
-   路径必须 canonicalize，防止符号链接越界。
-   Artifact 交接使用路径引用和摘要，不把所有文件内容复制进 Prompt。
-   上游 Artifact 应明确传给下游 Task。

## 12.5 DAG 示例

``` text
Plan: Build a local AI assistant demo

Researcher: Research candidate projects
        |
        v
Researcher: Recommend architecture
        |
        v
Developer: Implement demo
        |
        v
Reviewer: Review code and tests
        |
        v
Secretary: Accept and summarize
```

独立研究任务可并行；依赖实现的审查任务必须等待实现完成。

------------------------------------------------------------------------

# 13. Agent Session 与 Task Thread：不要混淆

当前系统已有：

-   Task。
-   Engine Thread。
-   Run。
-   `start_agent_session` 工具（以 Task + Engine Thread
    方式实现长期追问）。

这不等于已经有独立的 Agent Session 实体。

建议概念：

  概念            定义
  --------------- --------------------------------------------
  Agent Profile   长期身份配置
  Worker          一次运行实例
  Task            一项业务工作
  Engine Thread   某个引擎内连续执行上下文
  Agent Session   未来可选的、跨多个 Task 复用的长期工作会话

V1.1 不必新建 `agent_sessions` 表。

只有在用户确实需要"同一个 Agent 长期围绕某个主题连续工作，多个 Task
共享同一会话上下文"时，再设计 Agent Session Manager。

届时必须明确：

-   Session 属于哪个 Agent Profile。
-   Session 使用哪个 Engine。
-   Session 是否绑定 Project。
-   哪些 Task 可以加入 Session。
-   Session 的 Thread ID。
-   Session 何时归档。
-   Session 的并发写入如何串行化。
-   Session 的权限是否可以改变。

不能把一个 Profile 的所有任务默认塞进同一个 Thread。

------------------------------------------------------------------------

# 14. API 与 Web UI 改造

## 14.1 新增 Agent API

``` text
GET  /api/agents
GET  /api/agents/{agent_id}
POST /api/agents/sync
POST /api/agents/{agent_id}/enable
POST /api/agents/{agent_id}/disable
POST /api/agents/{agent_id}/approve
```

可选：

``` text
GET /api/agents/capabilities
GET /api/agents/sync-runs
```

所有接口继续使用现有 Bearer Token 认证。

## 14.2 Task API 扩展

`POST /api/tasks` 增加：

``` json
{
  "agent_id": "jarvis:developer",
  "project_id": "project-123"
}
```

保留原 `engine` 参数，兼容旧 UI / API。

## 14.3 新增 Plan API（后续阶段）

``` text
POST /api/plans
GET  /api/plans
GET  /api/plans/{plan_id}
POST /api/plans/{plan_id}/tasks
POST /api/plans/{plan_id}/start
POST /api/plans/{plan_id}/cancel
```

## 14.4 Web UI

第一阶段 Agent 页面至少显示：

-   Agent 名称。
-   Engine。
-   Profile ID。
-   描述。
-   能力标签。
-   用户级 / 项目级来源。
-   调用模式（直接 / 委派）。
-   当前状态。
-   配置版本。
-   最后同步时间。
-   启用 / 禁用。
-   审核 / 重新同步。

任务创建界面：

-   可选择 Agent。
-   可按 Engine 过滤。
-   显示 Agent 是否可直接调用。
-   保留旧 Engine 选择作为兼容入口。
-   未启用或待审核 Agent 不允许提交。

不要在第一阶段做复杂的 Agent 拖拽组织图。

------------------------------------------------------------------------

# 15. 权限与安全

## 15.1 双层权限

Local Dots 是外层控制平面；Engine Runtime 是内层执行权限。

``` text
Effective Operation
  must satisfy:
    Local Dots Global Policy
    AND Project Trust / Policy
    AND Task Authorization
    AND Engine Runtime Permission
    AND Required Approval
```

任意一层拒绝都必须拒绝。

`writes_authorized` 不能自动覆盖 Jarvis / Codex 自己的安全策略。

## 15.2 Catalog 不授予权限

Agent 的 `capabilities` 仅用于路由，例如：

``` text
python
research
code_review
testing
```

它不是：

``` text
filesystem_write
network_access
delete_files
```

即使 Agent 声称具备某项能力，也不能因此扩大权限。

## 15.3 项目 Agent 安全

-   项目级 Agent 只从已信任项目扫描。
-   新发现的项目 Agent 默认 pending review。
-   配置变更后重新审核。
-   不允许项目 Profile 修改全局审批策略。
-   不允许项目 Agent 访问其他 Project Memory。
-   不允许通过 symlink 逃出项目根。
-   不允许读取用户凭证或本机 Codex Token。

## 15.4 Codex 配置安全

-   绝不写入 `~/.codex/config.toml`。
-   不修改用户 Provider / MCP / Sandbox / Approval 设置。
-   项目级配置只在 Codex 当前版本认可的 trusted project 中使用。
-   Discovery 只读文件。
-   不因扫描到某个 TOML 就自动执行其中引用的脚本。
-   CLI 调用必须使用当前版本正式支持的参数。

## 15.5 审批

继续使用现有 ApprovalGate / AutoApprover。

Agent Catalog 不能绕过审批。

新 Agent 的首次启用、项目 Agent 信任、Profile
安全敏感变化，应有独立审核状态；这与单次 Task 的文件操作审批不是一回事。

------------------------------------------------------------------------

# 16. 实施阶段与交付验收

**建议按以下顺序实施。不要一次性并行重构所有阶段。**

## Phase 0：Repository Audit

工作：

1.  阅读现有文档和源码。
2.  运行测试。
3.  检查本地 Git 状态。
4.  核实 Codex CLI 版本与 Custom Agent 能力。
5.  核实 Jarvis Profile 公共 API。
6.  形成架构图和文件级变更清单。

交付：

``` text
docs/agent-organization/00-current-state-audit.md
docs/agent-organization/01-engine-capability-matrix.md
docs/agent-organization/03-implementation-plan.md
```

验收：

-   没有改动业务代码。
-   有基线测试报告。
-   已识别 Codex direct / delegated 能力差异。
-   已识别 Jarvis Profile API 是否可用。

## Phase 1：Agent Catalog + Discovery + Sync

工作：

1.  新增 `AgentDescriptor`。
2.  新增 Agent Catalog 持久化。
3.  新增 Agent Discovery Service。
4.  新增 Jarvis Discovery Provider。
5.  新增 Codex Discovery Provider。
6.  增加 Project Trust 基础校验。
7.  实现启动同步。
8.  实现手动同步 API。
9.  实现 Catalog 查询 API。
10. 实现发现变化、缺失和审核状态。

验收：

-   可以发现用户级 Jarvis Profiles。
-   可以发现 Codex 用户级 Custom Agent 文件。
-   可信项目中的项目级 Codex Agent 可发现。
-   不可信项目的 Agent 不被激活。
-   Discovery 不执行源配置。
-   重复同步幂等。
-   Agent 被删除后历史任务不丢失。
-   配置变化不会覆盖用户 Local Override。
-   Catalog 中能区分 direct / delegated_only。
-   旧功能和旧测试全部通过。

如果 Jarvis 尚未提供 `list_profiles()` 公共
API，应在报告中明确阻塞依赖，并提出对 Jarvis 仓库的最小 API 需求；不能
import Jarvis 内部模块作为临时捷径。

## Phase 2：Agent-based Task Routing

工作：

1.  Task 增加 Agent ID / Profile ID / Version / Hash / Snapshot。
2.  TaskManager 创建任务时解析 Catalog。
3.  增加旧 Engine 参数兼容。
4.  Jarvis Adapter 支持 Profile ID。
5.  Codex Adapter 按实际支持能力选择执行模式。
6.  Dispatcher 继续只按 Engine 选择 Adapter。
7.  Task Detail 返回 Agent 快照。

验收：

-   `jarvis:developer` 可以创建任务并调用 Jarvis developer Profile。
-   同一个 Profile 可以多次创建独立 Worker / Task。
-   Profile 变更不影响已创建 Task。
-   `codex:reviewer` 若为 delegated_only，不会被错误当成 direct Worker。
-   旧 `/api/tasks` 请求仍然工作。
-   旧 Task 可查询、追问、取消。
-   不破坏 Thread / Run / Checkpoint / Approval。

## Phase 3：Secretary Agent Catalog Integration

工作：

1.  Secretary 增加 `list_agents`。
2.  增加 `get_agent`。
3.  增加 `recommend_agent`。
4.  Prompt 从单纯 Engine Routing 改为 Agent-aware Routing。
5.  保留默认 Jarvis / 显式 Codex 规则。
6.  新增 `explicit` / `suggest` / `auto` 模式配置。
7.  前端增加 Agent 选择和目录页面。

验收：

-   Secretary 能正确列出可用 Agent。
-   用户指定 Agent 时可以准确选择。
-   用户指定 Engine 时仍能按旧规则工作。
-   suggest 模式会说明推荐理由。
-   auto 模式只选择 approved + enabled + available +
    invocation-compatible Agent。
-   pending / disabled / unavailable Agent 不会被调用。
-   任务委派仍然立即返回，不等待完成。

## Phase 4：Shared Context / Project Registry

工作：

1.  Project Registry。
2.  User Shared Memory 与 Secretary Memory 分离。
3.  Project Shared Memory。
4.  Context Bundle 生成与版本化。
5.  Jarvis Adapter Context Bundle 传递。
6.  Codex Adapter Context Bundle 传递。
7.  Memory Candidate Inbox（仅在 Engine 公共 API 支持时）。

验收：

-   同一个 Project Context 可以给 Jarvis 和 Codex 使用。
-   不同项目之间上下文隔离。
-   Context Bundle 有来源和版本。
-   不把敏感凭证注入 Context。
-   Worker 无权直接修改 Shared Memory。
-   现有 `memory.json` 数据和 Secretary 行为不丢失。
-   不直接读写 Jarvis 内部 Memory 文件。

## Phase 5：Task Planner / DAG / Artifacts

工作：

1.  Plan 数据模型。
2.  Task Dependency 表。
3.  DAG 环检测。
4.  Dispatcher 队列依赖判断。
5.  Artifact 注册和交接。
6.  Secretary 生成任务计划。
7.  UI 展示 Plan 和任务依赖。

验收：

-   多个无依赖 Task 可以并行。
-   有依赖的 Task 等待上游完成。
-   上游失败时下游默认不执行。
-   环形依赖被拒绝。
-   Artifact 路径不能逃出 Workspace。
-   任务计划可恢复。
-   不破坏现有 parent_task_id 语义。

## Phase 6：Agent Lifecycle / Observability

工作：

-   Agent 启用、禁用和健康检查。
-   Profile Version / Hash 追踪。
-   每个 Engine / Agent 的并发上限。
-   运行耗时和 Token / 成本统计（引擎能提供时）。
-   Agent 失败率和历史运行。
-   Agent Session Manager（仅在实际需要时）。
-   同步历史和配置变更审计。

验收：

-   能查看 Agent 当前可用状态。
-   能追踪每个 Task 使用的 Agent 版本。
-   Engine 故障不会污染其他 Engine。
-   Catalog 中失效 Agent 不会被新任务选择。

## Phase 7：Autonomous Organization / Proactive Assistant

后续长期阶段：

-   Secretary 自主拆解复杂目标。
-   自动选择多个 Agent。
-   动态创建临时 Worker。
-   并行执行和依赖调度。
-   Reviewer 独立验收。
-   失败后重规划。
-   定时与条件触发。
-   长期目标跟踪。
-   主动通知。

不要在 Phase 1 / 2 前提前实现这些能力。

------------------------------------------------------------------------

# 17. 测试计划

## 17.1 Catalog / Discovery

-   Jarvis API 不存在。
-   Jarvis API 正常返回。
-   Codex Agent 目录不存在。
-   Codex TOML 格式错误。
-   缺少 `name` / `description` / `developer_instructions`。
-   用户级和项目级同名 Agent。
-   CODEX_HOME 非默认路径。
-   项目不可信。
-   Agent 文件被删除。
-   Agent 配置 Hash 变化。
-   同步重复执行。
-   两个 Engine 出现同名 Agent。
-   Discovery 失败不影响旧 Catalog 读取。

## 17.2 Invocation Mode

-   Jarvis Profile direct invocation。
-   Codex default direct invocation。
-   Codex Custom Agent delegated_only。
-   delegated_only 被当成 direct 时必须拒绝。
-   Codex 当前版本不支持指定 Agent 时明确报错。
-   不得向 CLI 传入未验证参数。

## 17.3 Task / Migration

-   旧 tasks.db 自动迁移。
-   新字段默认值正确。
-   旧 API 请求不变。
-   新 API 指定 Agent。
-   Agent 与 Engine 冲突。
-   Agent 不存在。
-   Agent pending review。
-   Profile Hash 更新后旧任务快照不变。
-   Follow-up 复用原 Thread。
-   新 Task 使用新 Thread。
-   Run 历史保留。

## 17.4 Router / Secretary

-   显式指定 Agent。
-   显式指定 Engine。
-   默认 Jarvis。
-   suggest 模式。
-   auto 模式。
-   无匹配 Agent。
-   Agent 不可用。
-   多个候选优先级。
-   LLM 返回不存在的 agent_id。
-   LLM 请求使用 disabled Agent。
-   Secretary 仍立即返回 Task ID。

## 17.5 Project Trust / Security

-   项目级 `.codex/agents` 在 trusted project 中可发现。
-   untrusted project 不可激活。
-   symlink 越界。
-   `..` 路径穿越。
-   盘符和 UNC 路径。
-   修改配置后重新审核。
-   项目 Agent 不能扩大全局权限。
-   Context Bundle 不包含凭证。

## 17.6 Shared Context

-   同一项目 Context Bundle 可供两个 Engine 使用。
-   不同项目互相隔离。
-   Task Workspace 与 Project Root 不混淆。
-   删除 Task Workspace 不删除 Project Memory。
-   Secretary Memory 不自动复制给所有 Agent。
-   Agent Memory 不被 Local Dots 直接覆盖。

## 17.7 DAG

-   无依赖任务并行。
-   依赖任务等待上游。
-   多个依赖全部满足后入队。
-   上游失败。
-   环形依赖。
-   任务取消。
-   服务重启恢复。
-   Artifact 文件丢失。
-   Artifact 路径越界。

------------------------------------------------------------------------

# 18. 可观测性与日志

每次 Agent 选择至少记录：

``` text
task_id
agent_id
engine
engine_profile_id
profile_version
profile_hash
invocation_mode
routing_mode
routing_reason
project_id
workspace
created_at
```

每次 Discovery Sync 记录：

``` text
sync_id
engine
source_scope
started_at
finished_at
discovered_count
changed_count
missing_count
pending_review_count
errors
```

每次 Run 继续记录：

-   run_id
-   Task 状态变化
-   Engine Thread 绑定
-   副作用台账
-   审批请求和决策
-   Engine 错误
-   取消 / 恢复状态

日志不能输出：

-   API Token。
-   Codex 凭证。
-   模型网关 Key。
-   完整敏感 Prompt。
-   未经授权的 Memory 内容。

------------------------------------------------------------------------

# 19. 配置项建议

新增配置遵循 `LD_` 前缀并集中在 `app/settings.py`。

建议：

``` text
LD_AGENT_CATALOG_ENABLED=true
LD_AGENT_SYNC_ON_STARTUP=true
LD_AGENT_SYNC_INTERVAL_MIN=0
LD_AGENT_ROUTING_MODE=explicit
LD_AGENT_CATALOG_PATH=data/agent_catalog.db
LD_AGENT_AUTO_ENABLE_USER_PROFILES=false
LD_AGENT_AUTO_ENABLE_PROJECT_PROFILES=false
LD_PROJECT_TRUST_REQUIRED=true
LD_AGENT_MAX_CONCURRENT_PER_PROFILE=...
```

说明：

-   `SYNC_INTERVAL_MIN=0` 表示第一版不做周期扫描。
-   用户级 Profile 默认发现后待审核或按明确策略启用。
-   项目级 Agent 默认不能自动启用。
-   并发配置不能超过全局 `LD_MAX_CONCURRENT_RUNS`。
-   具体配置名和默认值以现有 Settings 风格为准。
-   `.env.example` 必须同步更新，不要把秘密写入其中。

------------------------------------------------------------------------

# 20. 交付标准

每个 Phase 完成后，开发 Agent 必须汇报：

1.  修改文件清单。
2.  新增文件清单。
3.  数据库迁移情况。
4.  新增配置项。
5.  Adapter 能力变化。
6.  测试命令和测试结果。
7.  旧功能回归结果。
8.  尚未完成项。
9.  安全影响。
10. 回滚方式。

每个 Phase 独立提交，使用仓库现有 Conventional Commits 风格和中文描述。

不要把多个阶段的变更合并成一个无法定位问题的大提交。

------------------------------------------------------------------------

# 21. 第一批真正要落地的范围

不要试图一次性实现整份路线图。

**第一批只做 Phase 0、Phase 1、Phase 2 的必要部分。**

即：

1.  审计现有 Local Dots。
2.  确认 Jarvis Profile 公共 API 是否已完成。
3.  确认 Codex Custom Agent 当前调用模式。
4.  建立 Agent Catalog。
5.  建立 Jarvis / Codex Discovery。
6.  建立启动同步和手动同步。
7.  Task 增加 Agent 身份和 Profile 快照。
8.  Jarvis Adapter 支持传递 Profile ID。
9.  Codex Adapter 对 direct / delegated_only 做准确区分。
10. 保留现有默认路由和旧 API。

第一批暂不做：

-   完整 DAG。
-   自动创建大量 Agent。
-   跨引擎 Memory 自动写入。
-   复杂 Agent Session。
-   定时主动工作。
-   自主无限委派。

第一批完成后，用户应能做到：

1.  在 Jarvis 中增加一个新的长期 Profile。
2.  在 Codex 中增加一个新的 Custom Agent。
3.  Local Dots 执行 Agent Sync。
4.  新身份自动出现在 Agent Catalog。
5.  用户审核并启用它。
6.  Secretary 能看到它的描述和能力。
7.  如果该身份支持直接调用，Local Dots 能创建指定身份的 Task。
8.  如果它只支持内部委派，Local Dots
    能明确显示这一限制，而不是错误路由。
9.  Task 记录实际使用的 Agent 身份和 Profile Hash。
10. 原有 Codex / Jarvis 任务、审批、取消、恢复、追问全部正常。

------------------------------------------------------------------------

# 22. 给开发 Agent 的最终执行提示

请先完整阅读：

-   本方案。
-   `AGENTS.md`。
-   `CONTEXT.md`。
-   `README.md`。
-   原始 `###LocalDots｜Wi.txt`。
-   `docs/adr/`。
-   `.scratch/` 当前工作记录。

然后：

**第一步：只做审计和设计，不修改业务代码。**

重点回答：

1.  当前本地代码与公开 master 有哪些差异？
2.  当前 Agent / Engine / Task / Thread / Run 的真实调用链是什么？
3.  Jarvis 当前是否已提供公开 `list_profiles()` 和
    `create_jarvis(profile_id=...)`？
4.  当前 Codex CLI 是否允许直接启动自定义 Agent，还是只能通过 Codex
    主代理委派？
5.  当前 `EngineAdapter` 应如何最小扩展，才能支持 Discovery
    而不破坏现有测试？
6.  SQLite 迁移如何保证旧数据完整？
7.  当前 Secretary 默认 Jarvis / 显式 Codex 规则如何保持兼容？
8.  项目级 Agent 的 Trust 边界如何实现？
9.  哪些功能属于第一批，哪些必须延后？

**第二步：给出实际文件级实施计划。**

**第三步：先实施 Phase 1，再实施 Phase 2。**
每阶段结束运行完整测试并汇报，不能在失败测试未解释的情况下继续叠加改动。

如果 Jarvis 或 Codex 的某项能力当前不支持，请明确标记 `unsupported` /
`delegated_only`，不要通过 Prompt 假装实现。

------------------------------------------------------------------------

# 23. 最终目标

最终 Local Dots 应达到：

> Thomas 只需要向 Secretary 描述目标。Local Dots 能自动认识安装在本机的
> Jarvis、Codex 及未来其他 Engine 中的长期 Agent 身份；通过统一 Catalog
> 了解它们的职责、能力和调用模式；根据用户要求和任务特征选择合适
> Agent；通过 Adapter 创建隔离 Worker；使用统一 Task / Plan / Approval /
> Artifact
> 机制组织工作；为各引擎提供经过授权的用户和项目上下文；最后统一验收和交付结果。

同时：

-   Jarvis 自己维护 Jarvis Profile、Skills、Agent Memory。
-   Codex 自己维护 Codex Custom Agents、Skills 和执行环境。
-   Local Dots 维护组织、目录、路由、任务、项目共享上下文和审批。
-   三者通过稳定、可测试、版本化的公开接口协作。

**核心不是让 Local Dots 变成另一个 Agent
Runtime，而是让它成为能够组织多个 Agent Runtime 的个人 AI
组织控制平面。**


---

# 附录 A：实施前参考的仓库与官方资料

以下链接用于帮助开发 Agent 快速定位公开基线。**本机 checkout 优先于公开分支；当前 CLI / Runtime 版本的实际行为优先于旧文档。**

## Local Dots 当前公开代码

- [仓库 README](https://github.com/newkicar/local_bots/blob/master/README.md)
- [AGENTS.md 工程纪律](https://github.com/newkicar/local_bots/blob/master/AGENTS.md)
- [CONTEXT.md 领域术语](https://github.com/newkicar/local_bots/blob/master/CONTEXT.md)
- [pyproject.toml](https://github.com/newkicar/local_bots/blob/master/pyproject.toml)
- [app/main.py](https://github.com/newkicar/local_bots/blob/master/app/main.py)
- [app/task_manager.py](https://github.com/newkicar/local_bots/blob/master/app/task_manager.py)
- [app/dispatcher.py](https://github.com/newkicar/local_bots/blob/master/app/dispatcher.py)
- [app/adapters/base.py](https://github.com/newkicar/local_bots/blob/master/app/adapters/base.py)
- [app/adapters/jarvis.py](https://github.com/newkicar/local_bots/blob/master/app/adapters/jarvis.py)
- [app/adapters/codex.py](https://github.com/newkicar/local_bots/blob/master/app/adapters/codex.py)
- [app/secretary.py](https://github.com/newkicar/local_bots/blob/master/app/secretary.py)
- [app/memory.py](https://github.com/newkicar/local_bots/blob/master/app/memory.py)
- [app/settings.py](https://github.com/newkicar/local_bots/blob/master/app/settings.py)

## Codex Agent / SubAgent 参考

- [Codex Subagents：自定义 Agent 定义与委派](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Codex Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex 配置文件基础与项目级配置](https://developers.openai.com/zh-Hans/docs/config-file/config-basic)

注意：开发时必须按本机 `codex --version` 对应的官方文档和实际 CLI 行为复核，不能只依赖本附录链接或搜索摘要。

## Truly-Jarvis

- [Truly-Jarvis 仓库](https://github.com/newkicar/truly-jarvis)

Jarvis Profile Discovery、Profile-aware Runtime、Memory Candidate 等接口应以 Jarvis 仓库实际合并的公开 API 为准。Local Dots 不得通过 import Jarvis 内部模块绕过 Adapter 边界。
