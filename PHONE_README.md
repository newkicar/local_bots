# PHONE_README — 手机操作 Local Dots 完全指南

> 本文档只讲"手机远程连接"。电脑端的安装、日常启动见 `README.md`；
> 设计背景与验收记录见 `docs/cloudflare-blueprint.md`。
>
> 正式使用前必须先完成"路径 B"的 Access 身份核验 + MFA，在此之前 quick 链接仅限自己临时验证。
>
> 当前状态（2026-11-04 实测）：quick 路径已验证可用；命名隧道 `login` 的证书
> 下载在本机网络不稳（TLS reset/timeout，见 §3.1 故障③），修通前先用 quick 顶着。

## 0. 原理（一句话说清）

服务只监听本机回环 `127.0.0.1:8000`，外网打不进来；手机访问走 Cloudflare 隧道：

```
手机浏览器 → Cloudflare 网络（Access 身份核验 + MFA）→ 本机 cloudflared 隧道 → 127.0.0.1:8000
```

- **本机不新增任何入站端口**（防火墙不用开）。
- 即使有人进了隧道，后端仍会校验 API Token（`.env` 里的 `LD_API_TOKEN`）——双保险。
- 模型服务端口 8788（mimo2codex）永远只绑回环，公网不可达。

## 1. 手机端操作前的电脑端预检（每次都要做）

1. **服务在跑**：电脑浏览器打开 `http://127.0.0.1:8000` 能正常登录，
   或执行 `powershell -File scripts\cloudflare_tunnel.ps1 status` 看到 8000 回环 LISTEN。
2. **cloudflared 就位**：仓库根目录有 `cloudflared.exe`（二进制不入库，本机自备；
   缺了脚本会直接报红并提示）。

3. **清掉死代理残留**：本终端若被注入过失效代理（如 AI 代理 exec 会话留下的
   `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY=http://127.0.0.1:9`），cloudflared 直连会挂死。
   `scripts\cloudflare_tunnel.ps1 quick` 已内置清理；手动直调 `cloudflared.exe` 的，
   需自己在同一窗口先 `Remove-Item env:HTTP_PROXY,env:HTTPS_PROXY,env:ALL_PROXY` 再跑。

## 2. 路径 A：quick 临时链接（只用于验证连通，无 MFA）

1. 电脑端执行：

   ```powershell
   powershell -File scripts\cloudflare_tunnel.ps1 quick
   ```

2. 窗口打印一个 `https://xxxx.trycloudflare.com` 临时地址，**手机浏览器打开它**。
3. 网页输入框填入 `.env` 里的 `LD_API_TOKEN`，登录即用。

   ⚠️ 三条铁律：
   - 该链接**没有身份核验，谁拿到谁都能进**，只发给你自己；
   - Ctrl+C / 关窗口 / 电脑重启即停，URL 立即失效（每次 quick 都是新 URL）；
   - 仅限开发验证，**正式工作一律走路径 B**。

4. **预热期 30–90 秒**：刚打印的 URL 首访可能报 1033 / 一直转圈，属正常
   （Cloudflare 节点还在拉齐路由），等 1 分钟再试即可。

5. **后台长期挂着（可选）**：不想占一个终端时，分离进程启动：

   ```powershell
   Start-Process -WindowStyle Hidden -FilePath ".\cloudflared.exe" `
     -ArgumentList 'tunnel --url http://127.0.0.1:8000 --no-autoupdate --logfile cf_quick.log'
   ```

   - 日志路径必须 ASCII（cloudflared 对中文路径有 bug）；仓库根 `D:\Python Project\local_dots`
     是 ASCII，写到根目录即可（`cf_quick.log` 已 gitignore）。
   - 每次启动都是新 URL、旧的作废——重读日志拿新地址；停：`Stop-Process -Name cloudflared`。

## 3. 路径 B：正式命名隧道 + Access 身份 + MFA（一次性 setup）

> **重要**：`setup` 子命令只**打印**下面这些命令，不替你执行
> （`login` 是交互式浏览器授权、`route dns` 要用你自己的域名，都不能自动化）。
> 先在仓库根跑一遍 `powershell -File scripts\cloudflare_tunnel.ps1 setup` 看指引，
> 然后按本节逐条手动执行。

### 3.1 登录 Cloudflare（一次性）

```powershell
.\cloudflared.exe tunnel login
```

- 自动打开浏览器：登录你的 Cloudflare 账号（免费注册即可）→ 点 **Allow**；
- **点完 Allow 后终端什么都别按（不要 Ctrl+C）**：CLI 要等授权回调、
  自己下载凭据。成功标志是终端打印
  `Successfully generated new tunnel certificate` 并列出下一步命令；
- 凭据写入 `C:\Users\<你的用户名>\.cloudflared\`。

**常见故障 ①：终端报 `Failed to write the certificate ... context deadline exceeded`**
（公司网络很常见：浏览器走代理能通，CLI 直连被拦）：

1. 此时**浏览器会代你下载证书文件**（到下载目录，形如 `cert.pem`）；
2. 把它复制为 `C:\Users\<你的用户名>\.cloudflared\cert.pem`；
3. 若后续 `create` 还报找不到 credentials/token，说明账号凭据 JSON 也没写全，
   需要**带代理重登**（见下方"代理"小步）。

**常见故障 ②：终端一直停在 `Waiting for login...` 后被自己 Ctrl+C 了**
→ 等于凭据没写，重做本步即可；重做时同样**不要提前掐**。

**验证凭据已就位：**

```powershell
dir $env:USERPROFILE\.cloudflared\
```

应看到 `cert.pem` 和/或 `cloudflared-*.json`。

**代理小步（仅当上面出过 ① 且凭据不全时做）：**

```powershell
# 查公司代理地址（浏览器就是用这个出网的；形如 10.x.x.x:8080）
(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings').ProxyServer
```

```powershell
# 在同一个 PowerShell 窗口里设代理再重登
$env:HTTPS_PROXY="http://<代理地址>"
$env:HTTP_PROXY="http://<代理地址>"
.\cloudflared.exe tunnel login
```

（查出来为空 = 系统未配代理，那说明是公司 DNS/防火墙拦 CLI：换个干净网络
比如手机热点再试，成功一次即可，凭据落盘后以后不再需要。）

**常见故障 ③：浏览器点完 Allow，终端却一直 `Waiting for login...`，`~\.cloudflared\` 始终空**
（2026-11-04 本机实测命中）：浏览器授权能通，但 CLI 自己直连
`login.cloudflareaccess.org` 下载证书时被网络层 TLS 掐掉（reset/timeout），与代理无关。
处置：

1. 错峰重试（有随机成功窗口，换个干净网络更稳）；成功一次凭据即落盘，以后不再需要；
2. 备选（尚未实测）：**API Token 免交互**——Cloudflare 控制台 My Profile → API Tokens
   建 token，写入 `$env:USERPROFILE\.cloudflared\cfcli.json` 或设 `CLOUDFLARE_API_TOKEN`
   环境变量，之后 `tunnel create localdots` 等命令走 API、免浏览器。

### 3.2 创建命名隧道 + 绑定域名（一次性）

```powershell
.\cloudflared.exe tunnel create localdots
```

打印一个 UUID（记下来排障用）。

```powershell
.\cloudflared.exe tunnel route dns localdots <host>.<你的 Cloudflare 域名>
```

⚠️ **前提：名下要有一个托管在 Cloudflare 的域名**（免费 DNS 套餐即可），例如
域名是 `example.com` 就执行 `.\cloudflared.exe tunnel route dns localdots dots.example.com`。
**没有域名**：命名隧道这条路暂时走不通——先用路径 A 的 quick 顶着，
或确认公司 Cloudflare 账号下有没有可用 zone。

### 3.3 Zero Trust 控制台建 Access 策略（一次性）

按 `setup` 打印的指引（细节见 `docs/cloudflare-blueprint.md`）：

1. 应用安全与网络 → Access → 应用程序 → **Add an application → Self-hosted**
   → 选 Cloudflare Tunnel `localdots`（Service `http://127.0.0.1:8000`）；
2. 策略：放行**仅你的账号邮箱** + **强制 MFA**（TOTP/Passkey）；
3. 记住：Access 是**入口层**不是权限层——后端 API Token 校验照旧，双保险。

### 3.4 启动并使用

```powershell
powershell -File scripts\cloudflare_tunnel.ps1 run
```

- 前台运行：关终端即停；启动前脚本会打印**黄色提醒**——先确认 Access 策略已生效，
  否则链路只有 Token 一道门；
- 需要开机常驻才执行 `powershell -File scripts\cloudflare_tunnel.ps1 register`
  （**默认不注册**，反悔用 `remove`）。

手机：浏览器打开 `https://<host>.<你的域名>` → 先过 MFA → 再填 Token 登录。
不在放行名单或没做 MFA 的人进不来。

## 4. 体检与故障排查

随时体检（只读、无副作用）：

```powershell
powershell -File scripts\cloudflare_tunnel.ps1 status
```

它会报：cloudflared 是否在跑、8000/8788 是否仍仅回环、配置文件有无密钥类字段、
凭据与自启状态。

| 现象 | 原因 / 处理 |
|---|---|
| `login` 报 `Failed to write the certificate ... deadline exceeded` | 公司网络拦 CLI；浏览器已代下证书 → 复制到 `\.cloudflared\cert.pem`；凭据 JSON 缺就带代理重登（§3.1 代理小步） |
| 终端停在 `Waiting for login...` 被 Ctrl+C | 凭据没写全，重做 login，这次别掐 |
| `create` 报 `No file cert.pem ...` | 凭据没就位（目录空/缺 cert.pem）→ 按 §3.1 修复后再 create |
| 手机打不开 | 电脑端隧道没跑（status 看 cloudflared）；quick 的话那个终端还开着吗？ |
| 隧道通但 401 / Token 失效 | 手机填的 token 和 `.env` 的 `LD_API_TOKEN` 不一致，重新复制 |
| quick URL 突然失效 | 正常：quick 每次执行生成新 URL，Ctrl+C 后旧的作废 |
| 手机一直转圈 / 被拒 | 命名隧道：Access 策略是否放行你的邮箱、MFA 是否完成 |
| `Waiting for login...` 久等、`\.cloudflared` 空 | 故障③：CLI 直连下载证书被 TLS 掐 → 错峰重试或 API Token 备选（§3.1） |
| quick URL 首访 1033 / 转圈 | 预热期正常，等 1 分钟再试（§2 第 4 条） |
| 服务整个没起 | 先按 `README.md` 第 3 节把本机服务拉起来，再开隧道 |

## 5. 安全须知

- quick 链接不转发给他人；Token、cloudflared 凭据（`\.cloudflared\` 下全部文件）
  一律不进仓库、不贴聊天。
- Access 策略（指定身份 + MFA）启用之前，本远程通道按"内部验证"对待。
