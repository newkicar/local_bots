# Phase A 任务清单：Jarvis 适配边界改造（truly-jarvis-main）

目标：Jarvis 仓 `pip install -e .` 之后，local_dots 可以
`create_jarvis(workspace, checkpointer)` 无头驱动 Jarvis，且 TUI 路径不受影响。

## 前置（已验证，不用再证）
- `build_agent(config, model=None, checkpointer=None, ...)`（src/agent.py ~L282）
  已支持 checkpointer 外置注入，缺省 InMemorySaver。
- main.py 懒导入 TUI；`load_config(project_root=...)` 已参数化，无 os.chdir。

## 任务
1. 薄工厂 `create_jarvis(workspace, checkpointer=None)` 并公开为包入口。
2. 工作目录参数化：文件后端与 shell 工具显式接收 workspace 参数；
   验证 `grep -r "os.chdir" src/` 为空。
3. TUI/Core 分离冒烟测试：以纯 Python import Jarvis 核心后，断言
   `"textual" not in sys.modules`。
4. checkpointer 注入文档：约定 task_id 作 thread_id；local_dots 侧传
   `SqliteSaver("data/checkpoints.db")`。
5. pyproject 公开入口 + `pip install -e .` 安装验证。

## 纪律
- local_dots 的 adapters/jarvis.py 零 import Jarvis 内部模块（只用公开入口）。
- Jarvis 仓的 ADR 用 0012+ 编号，不与本仓 0001-0005 混编。