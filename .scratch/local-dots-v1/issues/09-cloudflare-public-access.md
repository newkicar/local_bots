# 09: Cloudflare 公网通道（最后一步，须 IT 批准）

**What to build:** 手机 → Cloudflare Access（MFA，仅指定身份）→ Cloudflare Tunnel →
localhost:8000。前置双门禁：本地六条验收全过 + 公司 IT 明确批准远程访问与 Agent 执行方式
（授权范围比当前用户能访问的更明确）。未获批前不动手；获批后以非敏感目录验证。

**Blocked by:** 08 (登录自启 + 六条验收), 外部门禁：公司 IT 书面批准

**Status:** deferred（G1 本地六条验收已过；G2 公司 IT 书面批准未获得，按门禁不实施。蓝图与门禁记录见 ../../../docs/cloudflare-blueprint.md，commit 44744f3；服务仍仅监听 127.0.0.1）

- [ ] FastAPI 仍仅监听 127.0.0.1，无公网直连端口
- [ ] Access 仅放行指定身份且启用 MFA；后端仍校验用户身份与操作权限
- [ ] API 密钥、Codex 凭据、mimo2codex 端口（8788）对公网不可达
- [ ] 手机端走隧道完成"查任务、发指令、审批、收结果"全链路
- [ ] 实施记录归档，标注 IT 批准依据