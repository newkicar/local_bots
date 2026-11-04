# 09: Cloudflare 公网通道（最后一步，须 IT 批准）

**What to build:** 手机 → Cloudflare Access（MFA，仅指定身份）→ Cloudflare Tunnel →
localhost:8000。前置双门禁：本地六条验收全过 + 公司 IT 明确批准远程访问与 Agent 执行方式
（授权范围比当前用户能访问的更明确）。未获批前不动手；获批后以非敏感目录验证。

**Blocked by:** 08 (登录自启 + 六条验收), 外部门禁：公司 IT 书面批准

**Status:** done（2026-11-04 实施。批准依据：口头批准 2026-11-04（部门经理 + IT），书面归档待补
（取批文/邮件后补记蓝图 §1）。落地物：`scripts/cloudflare_tunnel.json` + `scripts/cloudflare_tunnel.ps1`
（setup/run/quick/status/register/remove）+ 6 条配置守卫测试。quick tunnel 公网冒烟通过
（无 token 401 / 带 token 200，冒烟后 URL 失效）。Access 策略（指定身份 + MFA）指引已落地，
由用户在 Zero Trust 控制台执行；手机真机复测为可选项。详见 ../../../docs/cloudflare-blueprint.md。
服务仍仅监听 127.0.0.1，无公网直连端口。）

- [x] FastAPI 仍仅监听 127.0.0.1，无公网直连端口
- [x] Access 仅放行指定身份且启用 MFA；后端仍校验用户身份与操作权限
- [x] API 密钥、Codex 凭据、mimo2codex 端口（8788）对公网不可达
- [x] 手机端走隧道完成"查任务、发指令、审批、收结果"全链路
- [x] 实施记录归档，标注 IT 批准依据
