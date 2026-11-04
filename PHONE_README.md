# PHONE_README — 手机操作 Local Dots 完全指南

> 本文档只讲"手机远程连接"。电脑端的安装、日常启动见 `README.md`；
> 设计背景与验收记录见 `docs/cloudflare-blueprint.md`。
>
> 前提：公司 IT 已批准 Cloudflare 隧道方案（口头批准 2026-11-04，书面归档待补）。
> 正式使用前必须先完成"路径 B"的 Access 身份核验 + MFA，在此之前 quick 链接仅限自己临时验证。

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

## 2. 路径 A：quick 临时链接（只用于验证连通，无 MFA）

1. 电脑端执行：

   ```powershell
   powershell -File scripts\cloudflare_tunnel.ps1 quick
   ```

2. 窗口打印一个 `https://xxxx.trycloudflare.com` 临时地址，**手机浏览器打开它**。
3. 网页输入框填入 `.env` 里的 `LD_API_TOKEN`，登录即用。

   ⚠️ 三条铁律：
   - 该链接**没有身份核验，谁拿到谁都能进**，只发给你自己；
   - Ctrl+C / 关窗口即停，URL 立即失效（每次 quick 都是新 URL）；
   - 仅限开发验证，**正式工作一律走路径 B**。

## 3. 路径 B：正式命名隧道 + Access 身份 + MFA（一次性 setup）

**第 1 步：setup（只做一次）**

```powershell
powershell -File scripts\cloudflare_tunnel.ps1 setup
```

按 setup 打印的提示依次做（凭据写入 `%USERPROFILE%\.cloudflared\`，不进仓库）：

1. `.\cloudflared.exe tunnel login` —— 浏览器里登录你的 Cloudflare 账号并授权；
2. `.\cloudflared.exe tunnel create localdots` —— 创建命名隧道（拿到隧道 UUID）；
3. `.\cloudflared.exe tunnel route dns localdots <host>.<你的域名>` —— DNS 解析到隧道。

**第 2 步：Zero Trust 控制台建 Access 策略（只做一次）**

- 目标选第 1 步的 host，放行**仅你的账号邮箱**，并勾选**强制 MFA**；
- 控制台操作指引以 setup 打印内容为准（细节见 `docs/cloudflare-blueprint.md`）。

**第 3 步：启动命名隧道**

```powershell
powershell -File scripts\cloudflare_tunnel.ps1 run
```

- 前台运行：关终端即停；启动前脚本会打印黄色提醒，**先确认 Access 策略已生效**，
  否则链路只有 Token 一道门。
- 想让隧道开机常驻才执行 `... cloudflare_tunnel.ps1 register`（**默认不注册**，
  后悔药是 `remove`）。

**手机侧**：浏览器打开 `https://<host>.<你的域名>` → 先过 MFA → 再填 Token 登录。
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
| 手机打不开 | 电脑端隧道没跑（status 看 cloudflared）；quick 的话那个终端还开着吗？ |
| 隧道通但 401 / Token 失效 | 手机填的 token 和 `.env` 的 `LD_API_TOKEN` 不一致，重新复制 |
| quick URL 突然失效 | 正常现象：quick 每次执行生成新 URL，Ctrl+C 后旧的作废 |
| 手机一直转圈 / 被拒 | 命名隧道：Access 策略是否放行你的邮箱、MFA 是否完成 |
| 服务整个没起 | 先按 `README.md` 第 3 节把本机服务拉起来，再开隧道 |

## 5. 安全须知

- quick 链接不转发给他人；Token 与 cloudflared 凭据一律不进仓库、不贴聊天。
- Access 策略（指定身份 + MFA）启用之前，本远程通道按"内部验证"对待。
- 公司书面批文下来后，补记到 `docs/cloudflare-blueprint.md` §1（目前为口头批准挂账）。