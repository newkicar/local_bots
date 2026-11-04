# 11: Codex 长 JSON 事件行越限崩溃——分块 read + 异常路径杀进程树（bug）

**What to build:** 修复 `app/adapters/codex.py` 的事件流读取：`stdout.readline()` 受
asyncio StreamReader 默认 64 KiB 行长按制，codex `--json` 的单个事件行超限（如大段
SVG/Markdown 的 agent_message）即抛 `ValueError: Separator is not found, and chunk exceed
the limit`，被 Dispatcher 泛化为 `engine crash` 落 failed（同任务 Jarvis 引擎正常）。
改为 `stdout.read(65536)` 分块 + 自组行缓冲（按换行切分，EOF 后残留行补消费），
对行长按制免疫；同时补 `except BaseException` 处理器：EngineError/crash 路径同样先终止
子进程树再抛出（与取消路径一致，防孤儿 CLI 残留）。逐行 JSON 语义逐字保留
（thread.started / item.completed / turn.failed 处理不变）。

**Blocked by:** 无

**Status:** done

- [x] 128 KiB 级 agent_message 事件经真实子进程管道（默认 64 KiB StreamReader）执行成功、结果完整
- [x] 既有事件语义不变：thread 绑定 / Side-Effect Ledger / 失败退出码 / 取消杀进程（既有用例保持全绿）
- [x] EngineError 与异常路径杀进程树（注入 spawn 不 taskkill；真实 spawn taskkill /T 兜底）
- [x] 适配器 docstring 鸭子类型注释同步为 stdout.read(n)
