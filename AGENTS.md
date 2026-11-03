# Repository Guidelines

## 项目说明
Local Dots V1.0：基于 DeepAgents 的个人秘书 + 复用现有 Codex CLI（mimo2codex）与 Jarvis，通过 FastAPI Web UI 从手机/远端给本机 Windows 安排异步任务。完整设计基线见仓库根 `###LocalDots｜Wi.txt`（下称"方案文档"），架构改动先读它。

## 项目结构（目标布局，随阶段落地）
```
local_dots/
├── app/            # main.py(FastAPI 入口) secretary.py task_manager.py
│   │               # dispatcher.py session_manager.py permissions.py
│   └── adapters/   # base.py / codex.py / jarvis.py —— 唯一引擎接口
├── web/            # 原生 HTML/CSS/JS 响应式前端（无框架）
├── data/           # tasks.db（任务队列） checkpoints.db（LangGraph）；gitignore 实际数据
├── workspace/      # 任务输出与临时文件
├── tests/
├── .env.example    # API 密钥、mimo2codex 端口等只进 .env，.env 不入库
└── pyproject.toml
```
模块落位严格按方案文档第七节；新增顶层目录需在 PR 说明理由。

## 构建 / 测试 / 运行
- `pip install -e .` 安装本项目；Jarvis 依赖在 `D:\Python Project\truly-jarvis-main` 以 `pip install -e .` 装入同一环境。
- 运行：`uvicorn app.main:app --host 127.0.0.1 --port 8000`（仅监听回环，永不直接暴露公网）。
- 测试：`python -m pytest`（pytest `asyncio_mode = "auto"`，异步测试直接写 `async def`）。

## 强制纪律（防腐化，评审必查）
- `adapters/jarvis.py` **只允许**调用 Jarvis 工厂 `create_jarvis(workspace, checkpointer)` + `.ainvoke(...)`，**禁止** import Jarvis 内部模块；adapter 是唯一接口。
- **禁止 `os.chdir` 切全局进程目录**；Jarvis 文件后端与 shell 工具一律显式传任务级 workspace 参数。
- checkpointer 由本项目注入（独立 `data/checkpoints.db`），不用模块级单例；支撑按 task 独立 thread + 重启恢复核对。
- Task / Thread / Run 三层分离：`task_id`（业务）≠ `engine_thread_id`（引擎会话）≠ `run_id`（单次执行）；engine_thread_id 在创建后立即写库；同 thread 的并发写与执行必须串行化。

## 代码风格
- Python 3.12+，4 空格；`async/await` 优先；模块/函数 `snake_case`，类 `PascalCase`；数据库列与 API 字段 `snake_case`。
- 路径用 `pathlib`；Windows 环境，避免依赖 POSIX 专属行为。

## 测试约定
- 测试文件 `tests/test_<模块>.py`，函数 `test_<行为>`。
- **本机 pytest 临时目录 WinError 5 中毒：禁用 `tmp_path` / `tempfile.mkdtemp`**，用 `os.makedirs` 自建目录（如 `tests/tmp_workspace/`，gitignore 之）。

## 提交 / PR
- Conventional Commits + 中文描述，参照 Jarvis 仓库历史：`feat(task_manager): 落地 SQLite 任务队列与状态机`、`fix(dispatcher): 重启恢复核对 Thread 实际状态`。
- PR 说明：属于五阶段中的哪一步、对应验收标准（方案文档第九节六条）；安全改动（permissions、目录授权）须附测试。

## 安全基线
- 权限由 Python 服务端 + Windows 账户强制，**不靠 prompt**；FilesystemBackend / `virtual_mode` 均非 OS 沙箱。
- 秘书默认只读、仅授权目录内发现；Codex/Jarvis 仅在指定 workspace 内按任务授权读写/执行。
- 默认沙箱保守（如 Codex `read_only`），需要修改才切 `workspace_write`；API 密钥、Codex 凭据、mimo2codex 端口一律不回环外。
- 实施顺序（方案文档第八节）：① Jarvis Core/TUI 分离 + Codex SDK/mimo2codex 连通性验证 → ② 后台任务管理 → ③ 秘书 → ④ Web UI + 登录自启 → ⑤ Cloudflare（须公司 IT 授权后最后做）。
## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/<feature-slug>/` (no git remote yet; same convention as the sibling Jarvis repo). See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical role strings, label string equals role name: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root, created lazily by `/domain-modeling`. See `docs/agents/domain.md`.
