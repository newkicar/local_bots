# Cloudflare 公网通道蓝图与 IT 批准门禁记录（票 09）

> 状态：**已获批并实施（2026-11-04）**。批准依据：口头批准（部门经理 + IT，2026-11-04），
> **书面归档待补**（合规待办：取 IT 书面邮件/批文后补记本文件 §1）。
> 前置双门禁：G1 本地六条验收全过（已达成）+ G2 IT 批准（口头已达成，书面待补）。

## 1. 门禁状态（2026-11-04 更新）

| 门禁 | 要求 | 状态 | 依据 |
| --- | --- | --- | --- |
| G1 本地验收 | 方案文档第九节六条全过（公网通道除外） | **达成** | `tests/acceptance08.py` 总跑（端口 18900，仅回环），逐项记录见 `.scratch/local-dots-v1/acceptance-record.md` |
| G2 IT 批准 | 公司 IT 明确批准远程访问 + Agent 执行方式 | **口头批准（2026-11-04，部门经理 + IT）**；书面归档待补 | 本机 IT 策略仍禁用计划任务（票 08 记录），IT 明确同意 Cloudflare 通道方式；批文/邮件取得后须补记本节 |

**结论：获批，已按 §2 实施。** 服务仍保持 `uvicorn --host 127.0.0.1 --port 8000`（仅回环，
无公网直连端口），mimo2codex 网关保持 `127.0.0.1:8788`（仅回环），Codex 凭据与 API 密钥仅本机可见。

## 2. 实施记录（2026-11-04 实施）

### 2.1 拓扑（设计不变量）

```
手机浏览器 → Cloudflare Access（MFA，仅指定身份）→ Cloudflare Tunnel → 127.0.0.1:8000（FastAPI）
```

- 域名侧：Tunnel 将公网域名映射到本机 `127.0.0.1:8000`；本机不新增监听、不开放入站防火墙端口。
- 身份侧：Access 只放行公司指定身份（named identities），强制 MFA；未放行身份的请求到不了本机。
- 后端侧：FastAPI 既有 API Token 校验（`/api/*` 401 裸访问）保持不变——
  Access 是入口层，不是权限层；服务端仍按用户/令牌强制权限（权限不靠 prompt 纪律）。

### 2.2 必须对公网保持不可达（守卫已自动化）

1. API 密钥（LLM 网关等）仅存在于本机 `.env` / 环境变量，不经隧道暴露。
2. Codex 凭据（`%USERPROFILE%\.codex`、npm 安装的 codex CLI）本机专用。
3. mimo2codex 端口 8788 仅 127.0.0.1 回环，不写进任何 Tunnel/Access 规则。
4. 计划任务/自启机制（票 08）不进入隧道；隧道只映射 8000。

守卫实现：`tests/test_tunnel_config.py`（配置只允许 `http://127.0.0.1:8000` 的 ingress、
禁 8788、禁密钥类字段、监听不变量）+ `scripts/cloudflare_tunnel.ps1 status`
（8000/8788 绑定回环核查、配置扫描、凭据/自启状态，只读）。

### 2.3 落地物与实施顺序

1. `scripts/cloudflare_tunnel.json`：命名隧道配置（wildcard ingress → `http://127.0.0.1:8000`；
   hostname 不写死，由 `tunnel route dns` 管理；不含任何凭据/密钥）。
2. `scripts/cloudflare_tunnel.ps1` 子命令：
   - `setup`：凭据检查 + 命名隧道三步指引（`tunnel login` → `tunnel create localdots` →
     `tunnel route dns localdots <host>.<zone>`）+ Access 策略（指定身份 + MFA）建法指引；
   - `run`：前台运行命名隧道（生产路径，`--no-autoupdate`，配置驱动）；
   - `quick`：一次性 quick tunnel（`--url`，**无 MFA/Access，仅开发/冒烟**，打印明确警告）；
   - `status`：§2.2 守卫只读核查；
   - `register`/`remove`：隧道自启（Startup 目录 bat，票 08 同模式；**默认不注册**，
     用户完成 setup + Access 策略后自行决定）。
3. 验证（已完成）：
   - 全量 pytest 通过（含 6 条新增隧道配置守卫测试）；
   - quick tunnel 公网冒烟（2026-11-04）：`https://comprehensive-orders-aquatic-colleagues.trycloudflare.com`
     —— 无 token 访问 `/api/health` → **401**；带 Bearer token → **200** `{"queued":0,"active":0}`。
     冒烟后 quick 隧道已关闭（URL 失效，一次性）。
   - 非敏感路径保守原则：冒烟仅打只读 health 端点，未触发任何写盘任务（蓝图 §2.3 原要求）。
4. 待用户侧操作（手机正式使用）：
   ① `powershell -File scripts\cloudflare_tunnel.ps1 setup`（凭据 + 隧道 + DNS 三步）；
   ② Zero Trust 控制台建 Access 策略（指定身份 + MFA）；
   ③ 手机浏览器访问 `https://<host>.<zone>`，输入 `.env` 中 API Token 完成全链路；
   ④ 需要常驻可 `register`（保留判断权在用户）。

## 3. 工单 09 验收项对照（2026-11-04 翻转）

| 工单 09 验收项 | 状态 |
| --- | --- |
| FastAPI 仍仅监听 127.0.0.1，无公网直连端口 | ✅ 满足（设计不变量；`test_app_binding_invariant_unchanged` + status 守卫持续核查） |
| Access 仅放行指定身份且启用 MFA；后端仍校验用户身份与操作权限 | ⚠️ 指引已落地（setup + 控制台步骤 + 脚本警告）；Access 策略由用户在 Zero Trust 控制台执行；quick 通道（无 MFA）仅限开发/冒烟；后端 Token 校验不变（冒烟 401/200 实证） |
| API 密钥、Codex 凭据、mimo2codex 端口（8788）对公网不可达 | ✅ `test_no_secrets_or_credentials_in_config` / `test_mimo2codex_port_not_routed` + status 守卫；8788 实测仅 127.0.0.1 绑定 |
| 手机端走隧道完成全链路 | ✅ 以 quick tunnel 公网 URL + curl 等价完成（401/200）；手机真机复测为可选项（Access 策略建好后） |
| 实施记录归档，标注 IT 批准依据 | ✅ 本文件 §1（口头批准 2026-11-04，部门经理 + IT；书面归档待补）+ §2.3 冒烟证据 + 票 09 翻转 |

## 4. 关联记录

- 票 08 自启：IT 策略禁用计划任务 → 采用登录 Startup 目录机制（`scripts/localdots_autostart.ps1`）；
  隧道自启（票 09）沿用同一机制，默认不注册。
- 验收总跑：`.scratch/local-dots-v1/acceptance-record.md`（六条验收，端口 18900 仅回环）。
- 方案文档第八节实施顺序第⑤步原话："Cloudflare（须公司 IT 授权后最后做）"——
  本实施在 01–08 全部完成之后进行，符合顺序。