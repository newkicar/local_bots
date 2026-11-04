"""票 09 验收①③：Cloudflare 隧道配置守卫——隧道只允许映射本机回环 8000，
不得映射/暴露 8788（mimo2codex）、8788 凭据或任何密钥类字段；
且现有 app 代码保持"仅 127.0.0.1 监听"不变量（uvicorn 启动参数与自启脚本一致）。

设计不变量（docs/cloudflare-blueprint.md §2.1/§2.2）：
- 隧道建在 Cloudflare 侧，本机不新增监听、不开放入站端口；
- ingress 目标只能是 127.0.0.1:8000（FastAPI 回环端口）；
- 8788 与任何密钥/凭据字符串不得出现在隧道配置中。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TUNNEL_CONFIG = REPO_ROOT / "scripts" / "cloudflare_tunnel.json"
APP_MAIN = REPO_ROOT / "app" / "main.py"
AUTOSTART = REPO_ROOT / "scripts" / "localdots_autostart.ps1"
CLOUDFLARED_BIN = REPO_ROOT / "cloudflared.exe"

LOOPBACK_HOST = "127.0.0.1"
FASTAPI_PORT = 8000
MIMO2CODEX_PORT = 8788


def _load_config() -> dict:
    assert TUNNEL_CONFIG.is_file(), f"缺少隧道配置 {TUNNEL_CONFIG}"
    return json.loads(TUNNEL_CONFIG.read_text(encoding="utf-8"))


def test_tunnel_config_exists_with_ingress():
    cfg = _load_config()
    assert isinstance(cfg.get("ingress"), list) and len(cfg["ingress"]) >= 1, (
        "隧道配置必须声明至少一条 ingress 规则"
    )


def test_ingress_services_only_loopback_fastapi():
    """验收①：所有 ingress service 只能指向 127.0.0.1:8000（不改 app 监听、不新增监听）。"""
    for rule in _load_config()["ingress"]:
        svc = str(rule.get("service", ""))
        m = re.match(r"^http://([^:/]+):(\d+)$", svc)
        assert m, f"ingress service 必须显式为 http://host:port 的本地回环目标，实际: {svc!r}"
        host, port = m.group(1), m.group(2)
        assert host == LOOPBACK_HOST, f"隧道目标必须为本机回环 {LOOPBACK_HOST}，实际: {host}"
        assert int(port) == FASTAPI_PORT, f"隧道只能映射 FastAPI 回环端口 {FASTAPI_PORT}，实际: {port}"


def test_mimo2codex_port_not_routed():
    """验收③：mimo2codex 8788 不得写进任何隧道/Access 规则。"""
    raw = TUNNEL_CONFIG.read_text(encoding="utf-8")
    assert "8788" not in raw, "mimo2codex 端口 8788 对公网必须不可达（蓝图 §2.2 第3条）"


def test_no_secrets_or_credentials_in_config():
    """验收③：隧道配置内不得出现 API 密钥 / Codex 凭据类字段。"""
    raw = TUNNEL_CONFIG.read_text(encoding="utf-8").lower()
    for marker in (
        "api_key",
        "apikey",
        "secret",
        "token",
        "password",
        "bearer",
        "credential",
        ".codex",
        ".env",
    ):
        assert marker not in raw, f"隧道配置出现敏感字段 {marker!r}，违反蓝图 §2.2"


def test_app_binding_invariant_unchanged():
    """验收①（不变量）：app 侧与自启侧都保持仅 127.0.0.1 监听，隧道只映射 8000。"""
    main_src = APP_MAIN.read_text(encoding="utf-8")
    autostart_src = AUTOSTART.read_text(encoding="utf-8")
    assert "0.0.0.0" not in main_src and "0.0.0.0" not in autostart_src, (
        "FastAPI 保持仅回环监听（不新增公网直连端口）"
    )
    assert "127.0.0.1:8000" in autostart_src or "--host 127.0.0.1 --port 8000" in autostart_src


def test_cloudflared_binary_present_and_gitignored():
    """实施记录前置：本地 cloudflared.exe 就位，且二进制不入库。"""
    assert CLOUDFLARED_BIN.is_file(), "cloudflared.exe 应在仓库根（gitignore 之外由本机提供）"
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/cloudflared.exe" in gitignore, "cloudflared.exe 必须保持 gitignore（二进制不入库）"