# Local Dots V1.0 — 从手机给本机安排任务

> 给小白用户的使用指南：安装一次 → 每天只要“开服务 + 登网页”。
> 设计方案文档：`###LocalDots｜Wi.txt`（仓库根目录）。

别怕，全部操作都在本机回环（127.0.0.1），服务不会被外部网络直接访问。

---

## 1. 这是什么

它住在你的 Windows 电脑里：
- 你在手机上发任务（比如“把 D 盘某个多媒体目录打包”），电脑在后台干活、自动回复结果；
- 任务由两个“引擎”完成：**codex**（命令行 CLI）和 **jarvis**（agent 框架）；
- 还有个“秘书”：可以和它闲聊，它能帮你查文件（只读）、帮你发任务。

## 2. 一次性安装（只做一次）

## ① 安装本项目（需 Python 3.12+，终端里 `python` 命令可用）

```powershell
cd <local_dots 仓库所在目录>
pip install -e .
```

## ② 安装 Jarvis 依赖（codex 引擎不需要这一步）

```powershell
pip install "https://github.com/newkicar/truly-jarvis/archive/refs/heads/main.zip"
```
（Jarvis 是独立仓库 `newkicar/truly-jarvis`，直接从 GitHub 安装，无需自行 clone；前提是该仓库为公开仓库。）

## ③ 创建配置文件

把 `.env.example` 复制为 `.env` （同目录），用任何编辑器打开：
- **必改**：`LD_API_TOKEN` — 访问网页/接口的“密码”，换成一个够长的随机字符串；
- 秘书模型三行（`LD_SECRETARY_MODEL` / `LD_SECRETARY_BASE_URL` / `LD_SECRETARY_API_KEY`）改成你自己模型网关的值；
- 其他项保持默认即可（并发上限、审批超时等），`LD_SECRETARY_AUTHORIZED_DIRS` 按你本机实际放文件的目录调整。

## ④ 检查两个“引擎”是否就位

在 **cmd** 里分别输入：
```
codex --version
```
- 输出版本号 → codex 就位（此外还要跑着一个可给它用的 OpenAI 兼容模型网关；作者机器上是 mimo2codex @ 127.0.0.1:8788）；
- jarvis 需要它的模型服务（vLLM 或你自建的 OpenAI 兼容端点）可达；不可达时 jarvis 任务会失败，检查跑模型的那台机器开机且网络通畅即可（或暂时改用 codex 引擎）。

一次性安装完了。

## 3. 每天怎么启动

### 方式 A：手动启动（一次开一个终端窗口）

```powershell
cd <local_dots 仓库所在目录>
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
看到 `Uvicorn running on http://127.0.0.1:8000` 就是启动好了。这个窗口不能关；关了就是停服务。

### 方式 B：登录自启（推荐，只需注册一次）

```powershell
powershell -File scripts\localdots_autostart.ps1 register
```
之后每次登录 Windows 都会自动起服务（启动时若 8000 已被占用则自动跳过）。管理命令：
```powershell
powershell -File scripts\localdots_autostart.ps1 status   # 看自启及服务是否在跑
powershell -File scripts\localdots_autostart.ps1 remove   # 移除自启功能
```
注意：自启脚本找 Python 的顺序是 脚本 `-Python` 参数 > `LD_PYTHON` 环境变量 > PATH 上的 `python`。如果你电脑上直接敲 `python` 能进 Python，无需任何配置；否则注册时显式传参：
```powershell
powershell -File scripts\localdots_autostart.ps1 register -Python "C:\你的路径\python.exe"
```

检测服务是否在跑（任何终端）：
```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health -Headers @{ Authorization = "Bearer <你的LD_API_TOKEN>" }
```
返回 `ok` 就行。

## 4. 网页怎么用

浏览器打开 **http://127.0.0.1:8000/** → 输入 Token （就是 `.env` 里的 `LD_API_TOKEN` ）→ 进去。页面上就是全部功能：

| 功能 | 怎么用 |
| --- | --- |
| **秘书闲聊** | 直接输入问题，它只读你授权的目录，会查文件、帮你发任务 |
| **新建任务** | 写指令（中文就行）、选引擎（codex / jarvis），需要写文件时勾选“授权修改” |
| **任务列表 / 详情** | 看状态、结果、完整副作用台账 |
| **追问** | 任务完成后可在同一会话里继续聊，上下文会被记住 |
| **取消** | 对运行中的任务点“取消” |
| **审批** | 任务触发敏感操作会挂在“等待审批”，你批了才继续；也可驳回并给出理由 |

状态刷新是自动的（前台会推送状态），不需要手动刷新。

## 5. 任务从提交到完成：你会看到的状态

```
queued → running → reported（成功）/ failed（失败）/ cancelled（你取消）
                     └→ waiting-approval（等你审批，超时自动中断）
```
- 任务输出写在 `workspace\<任务号>\` 里，结果在网页详情的 `result` 里看；
- 所有任务/运行记录存在 `data\` 里（tasks.db 等），删掉也不影响服务运行；
- 同一任务在 jarvis 引擎里会在重启后从断点恢复；codex 引擎的子进程跨重启不存活，重启后会标为 `interrupted`，可以追问继续。

## 6. 常见问题（按顺序对症）

1. **网页说“连接失败”** → 服务没在跑：看第 3 节把服务起好，或 `status` 命令看 8000 是否被占用。
2. **输入了 Token 但说“Token 失效”（401）** → 你输入的和 `.env` 里的 `LD_API_TOKEN` 不一样，重新复制。
3. **codex 任务报“系统找不到路径 / os error 3”** → 已修复的路径 bug：保持 `18e97f4` 以后的代码，重启服务即可；或检查本机 codex CLI 是否存在。
4. **jarvis 任务失败（超时/连不上模型）** → jarvis 指向的模型服务不可达：检查跑模型的那台机器开机且网络通畅，或暂时改用 codex 引擎。
5. **登录出现黑色 cmd 窗口** → 自启的已知小限制，服务正常运行就可以留着，不想看就直接关窗（服务会随窗关掉，下次登录会重新起）。

## 7. 手机远程使用（Cloudflare 隧道，IT 已批准 2026-11-04）

背景：服务只监听本机回环（`127.0.0.1:8000`），公网打不进来。想用手机操作，就走
Cloudflare 隧道：手机 → Cloudflare 网络 → 本机的 `127.0.0.1:8000`。**本机不开任何新端口**，
正式使用只有被 Access 放行的人能进。

两步走：

**第 1 步（临时/验证连通）：一次性链接 quick**

```powershell
powershell -File scripts\cloudflare_tunnel.ps1 quick
```

窗口里会出现一个 `https://xxxx.trycloudflare.com` 链接：手机浏览器打开它，
在网页输入框填入 `.env` 里的 Token，就能用了。**注意：这个链接没有身份核验、
谁拿到谁都能访问，且你一停命令（Ctrl+C / 关窗）就失效——只用于开发验证，用完即停。**

**第 2 步（正式使用）：命名隧道 + Access 身份核验（MFA）**

```powershell
powershell -File scripts\cloudflare_tunnel.ps1 setup
```

`setup` 会打印：一次性三步命令（`login` / `create` / `route dns`，按提示做）+
Cloudflare Zero Trust 控制台的 Access 配置步骤（放行指定身份、强制 MFA）。
配好后前台运行隧道：

```powershell
powershell -File scripts\cloudflare_tunnel.ps1 run
```

手机访问 `https://<host>.<你的域名>`；不在放行名单或没做 MFA 的人进不来，
进来了后端还照旧校验 Token（双保险）。想开机常驻就 `register`
（默认不注册，保留与否你决定）。

随时体检：`powershell -File scripts\cloudflare_tunnel.ps1 status`（只读，无副作用）。

## 8. 安全须知（了解即可）

- 服务**只监听 127.0.0.1:8000**，外网打不进来；Token 泄了也只在本机可用。
  远程访问走第 7 节的 Cloudflare 通道（公司 IT 已批准 2026-11-04，口头、书面归档待补）；
  **Access 策略（指定身份 + MFA）启用之前，quick 链接不得用于正式工作**。
- **不要手动改 `~/.codex/config.toml`**：codex 的配置由 CLI 自己管，我们只是它的“调用方”。
- 任务默认沙箱是 **只读**，只有你勾选“授权修改”或在审批环节批准后才会写文件；审批可以忽略，但它是最后一道保险，别疏忽了。
