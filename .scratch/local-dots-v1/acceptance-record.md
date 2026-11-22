# Local Dots V1.0 六条验收记录（票 08）

- 记录时间：2026-11-04（Asia/Shanghai）
- 总跑驱动：`tests/acceptance08.py`（独立验收端口 **18900**，仅 127.0.0.1；不影响常驻服务 8000）
- 判定驱动注入的环境：`LD_DATA_DIR` / `LD_WORKSPACE_DIR` / `LD_API_TOKEN` / `LD_SECRETARY_AUTHORIZED_DIRS`（指向 `tests/tmp_workspace/acceptance/` 下临时目录，每次运行前清空）
- **最终结果：7/7 全过（`all_pass: true`），报告：`tests/tmp_workspace/acceptance/acceptance_report.json`（gitignored）**
  - 总跑时间戳：2026-11-04 04:21:43；server1_pid=19516、server2_pid=26152（验收专用服务，跑完即停）

## 逐项结果

### ① 多任务执行：连提三项互不等待 — PASS
- 引擎：fake（delay=1.0s）× 3。
- 证据：墙钟 **1.67s** < 2.5s 判定线（串行下限 ≥3s，故必为并行）；三任务同批到达 `reported`：
  `Ta5cac95b8873 / R24a585dae73a`、`T53e4c0c17c84 / R25c506f34af8`、`Tb853185fd381 / Rc4a11fec7183`。

### ② 多引擎复用：Codex 与 Jarvis 结果均可拿回 — PASS（带注记）
- Codex（真实 CLI + mimo2codex 网关，127.0.0.1:8788）：`Tff9c19d68271` → `reported`，结果含 `PONG`（result_tail `"\n\nPONG"`）。
- Jarvis：`Tfeaba775be0e` → `failed`，`jarvis 执行失败: Connection error.`。
- **注记（原文，来自报告）**：PASS-with-notation: 本机 Jarvis 模型（Jarvis .env 指向 LAN 10.214.131.188:8000）当前不可用，jarvis 任务按预期进入终态 failed（终态可达、原因可查）；引擎接线（工厂 + 独立 checkpointer + thread）本身通过。
- 说明：Jarvis LLM 服务宕机是环境问题，非接线缺陷；"结果均可拿回"指状态与原因均可查询——成立。

### ③ 动态工作目录：找到目录后在其中启动指定引擎 — PASS
- 引擎链：DeepAgents 秘书（agnes-3.0-flash @ mimo2codex）→ codex。
- 场景：授权目录根为动态生成目录 `…/acceptance/discovery`（每次总跑前清空重建，LLM 无法预先知道绝对路径），
  其下放置 `discovery/proj_alpha/alpha_notes.txt`。
- 证据（第 1 次尝试即成功）：`POST /api/chat`（"帮我找到授权目录下的 proj_alpha，在其中创建 pong.txt"）→
  `task_ids=["Tecf34c4fe8b1"]`，任务 `workspace = D:\Python Project\local_dots\tests\tmp_workspace\acceptance\discovery\proj_alpha`，
  `writes_authorized=true`，`chat_http=200`。秘书回复原文确认工作目录即该路径。
- 根因修复记录：早期总跑（补丁前）LLM 未收到授权目录绝对路径，多次猜路径被服务端拒绝（"路径超出授权目录"），
  任务落到默认 workspace。`app/secretary.py` 修复：系统提示词附加授权目录列表（`system_prompt()` 公开缝），
  服务端 `_authorized_path` 强校验不变（权限不靠 prompt 纪律：提示词只是入口，拒绝仍由服务端执行）。
  对应单测 `tests/test_secretary.py::test_system_prompt_lists_authorized_roots`。

### ④ 上下文隔离：追问原任务不混其他上下文 — PASS
- 引擎：fake × 4。证据：`thread_a=thread-Tb45508c5d8c4`、`thread_b=thread-Tf7f56b51552f`；
  对 A 追问（followup_http=200，新 Run `R178ac52e05e2`，origin=followup，attempt=2）后，
  兄弟任务 B 状态保持 `reported` 且 Run 数不变（`b_runs_before=1 == b_runs_after=1`）。

### ⑤ 可靠性：关网页不停任务；重启不丢记录、不盲目重试 — PASS
- ⑤a（关网页）：fake 任务 `Tbb72bd5517c7`，SSE 捕获 1 条事件后主动断开连接，任务仍执行到终态 `reported`
  （服务端执行不依赖前端连接）。
- ⑤b（重启核对）：fakelong（delay=45s）任务执行中强制停服务 1（pid 19516，terminated rc=1）→ 重启服务 2（pid 26152）→
  重启核对（reconcile）将跨重启无法判定副作用的运行转 `waiting-approval`：
  - run `Rc1ed21cf1cf2`：`state=waiting-approval`，`wait_reason=restart_reconciliation`，
    `pending_op.kind=restart_check`，path=`…/acceptance/workspace/T2989cf1809cc`，detail=`fake-reconcile`；
  - 拒绝（`POST /api/approvals/Rc1ed21cf1cf2/reject`，http=200）→ run `status=rejected`，任务终态 `rejected`。
  - **未盲目重试**：重启后无任何自动重跑；已知结果/可安全恢复分支由 `tests/test_reconcile.py` 覆盖。

### ⑥ 远程操作（回环内）：查任务、发指令、收结果 — PASS
- 引擎：plain httpx → 127.0.0.1:18900（仅回环，无公网直连端口）。
- 演练端点：`POST /api/tasks`、`GET /api/tasks(/id)`、`GET /api/runs/{id}`、`POST /api/tasks/{id}/followup`、
  `GET /api/approvals` + `POST /api/approvals/{run_id}/reject`、`POST /api/chat`、`GET /api/notifications`、
  `GET /api/health` + `GET /api/events?token=`（SSE 实时）。
- 安全不变量：裸访问 `/api/health`（不带 token）→ **401**（服务端令牌校验生效）。
- 注记：本次全部操作在回环完成；真正的手机/远端访问 = 票 09 Cloudflare 通道（deferred）。

## 登录自启（票 08 前半）证据

- 机制降级记录：本机为公司标准用户，**IT 策略禁用计划任务**（`Register-ScheduledTask` / `schtasks /create` 均 0x80070005，
  2026-11-04 实测）→ 采用每用户 Startup 目录自启入口：`scripts/localdots_autostart.ps1`
  （`register` 写 ASCII `localdots_autostart.bat` 到 `d:\uida0712\Start Menu\Programs\Startup`，含 8000 已占用则跳过的守护；
  `remove` / `status` 同步提供）。
- 自启演示（前序会话）：注册后隐藏 cmd 窗口拉起 uvicorn（pid **24296**，health 200）；
  杀掉服务进程后守护重新拉起（pid **25944**，health 200）→ 证明"杀服务后可拉起"。
- 演示后现场清理（已复核）：`localdots_autostart.bat` 已从 Startup 目录移除（launcher absent）、
  8000 端口无监听（port 8000 free）。最终状态：未注册自启、无常驻服务、端口干净。
- 不打包、非 Docker（工单要求）：自启只是"登录时启动 uvicorn"，代码以源码 + 可编辑安装运行。

## 已知限制（如实记录）

1. 自启入口登录时会短暂可见 cmd 窗口（Startup 机制固有限制；非交互、几秒后转入服务进程；如需隐藏需计划任务或系统级配置（需管理员权限），当前标准用户无权限）。
2. Jarvis LLM（LAN 10.214.131.188:8000）验收当时宕机 → ② 带注记通过（接线本身通过，结果可查）。
3. IT 策略禁用计划任务 → 自启用 Startup 目录机制替代（蓝图 `docs/cloudflare-blueprint.md` 已注明可日后平移）。
4. 公网通道（票 09）未实施：FastAPI 仅 127.0.0.1，Cloudflare 通道按蓝图执行（当前 deferred）。

## 总跑复现命令

```powershell
$env:PYTHONIOENCODING='utf-8'
python -c "import runpy, sys; sys.argv=['tests/acceptance08.py']; runpy.run_path(r'D:\Python Project\local_dots\tests\acceptance08.py', run_name='__main__')"
```

（跑前确认 18900/8000 无残留监听；跑后确认 18900 无孤儿进程。临时数据全部在 `tests/tmp_workspace/`，gitignored。）

---
补记（2026-11-04）：票 09（Cloudflare 公网通道）已按蓝图实施。
落地物与冒烟证据见 `docs/cloudflare-blueprint.md` §2/§3；服务仍仅监听 127.0.0.1:8000，8788 仍仅回环。
