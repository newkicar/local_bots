"""票 02 验收：Agent 管理 API（HTTP 外部行为，假引擎驱动；不 import 任何真实引擎模块）。

锚点模式复用 tests/test_api_skeleton.py（_settings/_headers 自建 tmp_workspace，禁 tmp_path）。
"""
from __future__ import annotations

import os
import shutil

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.fakes import FakeEngine

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")


def _settings(name: str, **kw) -> Settings:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return Settings(
        ld_data_dir=os.path.join(d, "data"),
        ld_workspace_dir=os.path.join(d, "workspace"),
        ld_api_token="test-token",
        ld_agent_sync_on_startup=False,  # 票 07：这些测试不跑启动 sync（**kw 可覆盖）
        _env_file=None,
        **kw,
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _user_desc(agent_id: str = "codex:newone") -> dict:
    return {"agent_id": agent_id, "engine": "codex", "engine_profile_id": "newone",
            "name": "New One", "description": "user profile", "source_scope": "user",
            "source_id": "newone", "source_path": None, "capabilities": {},
            "invocation_mode": "delegated_only", "profile_version": "v1",
            "profile_hash": "h1", "metadata": {}}


def test_agent_api_auth_and_builtins():
    settings = _settings("agent_api")
    with TestClient(create_app(settings, {"fake": FakeEngine()})) as c:
        assert c.get("/api/agents").status_code == 401
        r = c.get("/api/agents", headers=_headers())
        assert r.status_code == 200, r.text
        agents = {a["agent_id"]: a for a in r.json()["agents"]}
        assert set(agents) == {"jarvis:default", "codex:default", "codex:worker", "codex:explorer"}
        assert agents["codex:worker"]["invocation_mode"] == "delegated_only"
        assert agents["jarvis:default"]["discovery_status"] == "available"
        got = c.get("/api/agents/codex:default", headers=_headers())
        assert got.status_code == 200 and got.json()["agent_id"] == "codex:default"
        assert c.get("/api/agents/ghost", headers=_headers()).status_code == 404
        assert c.get("/api/agents/ghost").status_code == 401


def test_agent_approve_reject_flow():
    settings = _settings("agent_flow")
    with TestClient(create_app(settings, {"fake": FakeEngine()})) as c:
        cat = c.app.state.catalog
        c.portal.call(cat.upsert_agent, _user_desc())  # 模拟 discovery 落库 → pending_review
        a = c.get("/api/agents/codex:newone", headers=_headers()).json()
        assert a["discovery_status"] == "pending_review" and a["trust_state"] == "pending"

        r = c.post("/api/agents/codex:newone/approve", headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["discovery_status"] == "available" and body["trust_state"] == "trusted"
        # available 再 approve → 非法迁移 409
        assert c.post("/api/agents/codex:newone/approve", headers=_headers()).status_code == 409

        r = c.post("/api/agents/codex:newone/reject", headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["discovery_status"] == "rejected" and body["trust_state"] == "rejected"
        # rejected 再 reject → 409；未知 agent 一律 404
        assert c.post("/api/agents/codex:newone/reject", headers=_headers()).status_code == 409
        assert c.post("/api/agents/ghost/approve", headers=_headers()).status_code == 404
        assert c.post("/api/agents/ghost/reject", headers=_headers()).status_code == 404


def test_agent_enable_disable_override_flow():
    settings = _settings("agent_override")
    with TestClient(create_app(settings, {"fake": FakeEngine()})) as c:
        r = c.post("/api/agents/codex:worker/disable", headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["effective_status"] == "disabled"
        assert body["discovery_status"] == "available"  # 快照不变
        assert body["override"]["enabled"] is False

        r = c.post("/api/agents/codex:worker/enable", headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["effective_status"] == "available" and body["override"]["enabled"] is True

        assert c.post("/api/agents/ghost/enable", headers=_headers()).status_code == 404
        assert c.post("/api/agents/ghost/disable", headers=_headers()).status_code == 404
