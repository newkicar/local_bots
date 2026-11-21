"""票 11 验收：AgentRouter 路由 + 校验 + 路由决策落库 + 配置项 + 秘书接线。

锚点模式沿用 tests/test_task_agent_snapshot.py（假引擎 + 自建 tmp_workspace，
禁 tmp_path）；router 文案逐字匹配 app/agents/router.py 固定检查顺序。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.agents.catalog import AgentCatalog
from app.agents.project_trust import ProjectTrustService
from app.agents.router import AgentRouter
from app.main import create_app
from app.secretary import Secretary
from app.settings import Settings
from app.task_manager import TaskManager
from tests.fakes import FakeEngine
from tests.test_task_agent_snapshot import _dir, _headers, _old_db, _settings

DECISION_KEYS = {"selected_agent_id", "engine", "engine_profile_id", "routing_mode",
                 "reason", "alternatives", "user_explicit_choice"}


def _app3(name: str, **kw):
    return create_app(_settings(name, **kw),
                      {"fake": FakeEngine(), "codex": FakeEngine(), "jarvis": FakeEngine()})


def test_router_explicit_engine_and_agent():
    with TestClient(_app3("r11_api")) as c:
        h = _headers()
        # ① 缺省 → jarvis:default
        r = c.post("/api/tasks", json={"instruction": "x"}, headers=h)
        assert r.status_code == 201, r.text
        b = r.json()
        assert b["engine"] == "jarvis" and b["agent_id"] == "jarvis:default"
        assert set(b["routing_decision"]) == DECISION_KEYS
        assert b["routing_decision"]["user_explicit_choice"] is False
        assert "缺省路由" in b["routing_decision"]["reason"]
        assert b["agent_snapshot"]["engine_profile_hash"] == "builtin"
        # ② 显式 engine=codex
        r = c.post("/api/tasks", json={"instruction": "x", "engine": "codex"}, headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["agent_id"] == "codex:default"
        assert r.json()["routing_decision"]["user_explicit_choice"] is True
        assert "显式引擎" in r.json()["routing_decision"]["reason"]
        # ③ 显式 agent
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "codex:default"}, headers=h)
        assert r.status_code == 201 and r.json()["agent_snapshot"]["engine_profile_hash"] == "builtin", r.text
        # ④ delegated_only 拒绝（alternatives 带 codex:default）
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "codex:worker"}, headers=h)
        assert r.status_code == 422 and "delegated_only" in r.json()["detail"], r.text
        assert "codex:default" in r.json()["detail"]
        # ⑤ not found
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "codex:ghost"}, headers=h)
        assert r.status_code == 422 and "agent not found" in r.json()["detail"], r.text
        # ⑥ 引擎不匹配
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "jarvis:default",
                                       "engine": "codex"}, headers=h)
        assert r.status_code == 422 and "不匹配" in r.json()["detail"], r.text


def test_router_legacy_unregistered_engine():
    with TestClient(create_app(_settings("r11_legacy"), {"fake": FakeEngine()})) as c:
        r = c.post("/api/tasks", json={"instruction": "x", "engine": "fake"}, headers=_headers())
        assert r.status_code == 201, r.text
        b = r.json()
        assert b["agent_id"] is None and b["engine"] == "fake"
        assert "未登记" in b["routing_decision"]["reason"]
        assert set(b["routing_decision"]) == DECISION_KEYS


def test_router_catalog_disabled():
    with TestClient(_app3("r11_off", ld_agent_catalog_enabled=False)) as c:
        h = _headers()
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": "jarvis:default"}, headers=h)
        assert r.status_code == 422 and "禁用" in r.json()["detail"], r.text
        r = c.post("/api/tasks", json={"instruction": "y"}, headers=h)
        assert r.status_code == 201, r.text
        assert "目录禁用" in r.json()["routing_decision"]["reason"]


def _setup_project_agent(c) -> tuple[str, str]:
    cat = c.app.state.catalog
    root = _dir("r11_trust_root")
    pid = c.portal.call(cat.register_project, "gamma", root)["project_id"]
    aid = f"codex:project:{pid}:gamma"
    c.portal.call(cat.upsert_agent, {
        "agent_id": aid, "engine": "codex", "engine_profile_id": "gamma",
        "name": "Gamma", "source_scope": "project", "source_id": f"{pid}:gamma",
        "metadata": {"project_id": pid}, "profile_hash": "hG"})
    c.portal.call(cat.transition_agent, aid, "available")
    return pid, aid


def test_router_trust_gate():
    with TestClient(_app3("r11_trust")) as c:
        pid, aid = _setup_project_agent(c)
        h = _headers()
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": aid, "project_id": pid}, headers=h)
        assert r.status_code == 422 and "信任" in r.json()["detail"], r.text
        c.portal.call(c.app.state.trust.trust_project, pid)
        r = c.post("/api/tasks", json={"instruction": "y", "agent_id": aid}, headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["project_id"] == pid and r.json()["agent_id"] == aid


def test_router_trust_not_required():
    with TestClient(_app3("r11_trust_off", ld_project_trust_required=False)) as c:
        pid, aid = _setup_project_agent(c)
        r = c.post("/api/tasks", json={"instruction": "x", "agent_id": aid}, headers=_headers())
        assert r.status_code == 201, r.text
        assert r.json()["project_id"] == pid


async def test_router_routing_decision_db():
    path = os.path.join(_dir("r11_db"), "unit_tm.db")
    await _old_db(path)
    tm = TaskManager(path)
    await tm.open()
    async with tm.db.execute("PRAGMA table_info(tasks)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert "routing_decision_json" in cols
    old = await tm.get_task("Told00000001")
    assert old is not None and old["routing_decision"] is None
    await tm.close()


def test_router_settings_defaults_and_env_example():
    s = Settings(_env_file=None)
    assert s.ld_agent_catalog_enabled is True
    assert s.ld_agent_routing_mode == "explicit"
    assert s.ld_agent_auto_enable_user_profiles is False
    assert s.ld_agent_auto_enable_project_profiles is False
    assert s.ld_project_trust_required is True
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    lines = text.splitlines()
    for line in ("LD_AGENT_CATALOG_ENABLED=true", "LD_AGENT_SYNC_ON_STARTUP=true",
                 "LD_AGENT_ROUTING_MODE=explicit", "LD_AGENT_AUTO_ENABLE_USER_PROFILES=false",
                 "LD_AGENT_AUTO_ENABLE_PROJECT_PROFILES=false", "LD_PROJECT_TRUST_REQUIRED=true"):
        assert line in lines, line


async def test_secretary_router_wiring():
    settings = _settings("r11_sec")
    d = _dir("r11_sec2")
    tm = TaskManager(os.path.join(d, "unit_tm.db"))
    await tm.open()

    class _StubDispatcher:
        _engines = {"fake": object(), "codex": object(), "jarvis": object()}

    sec = Secretary(tm, _StubDispatcher(), settings, router=None)
    out = await sec._submit("显式 agent", None, None, False, session=False, agent_id="jarvis:default")
    assert "router 未接线" in out and json.loads(out)["error"]

    catalog = AgentCatalog(os.path.join(d, "agents.db"))
    await catalog.open()
    router = AgentRouter(catalog, ProjectTrustService(catalog),
                         available_engines={"fake", "codex", "jarvis"})
    sec2 = Secretary(tm, _StubDispatcher(), settings, router=router)
    out2 = await sec2._submit("接线 agent", None, None, False, session=False, agent_id="codex:default")
    assert "error" not in out2, out2
    row = await tm.get_task(json.loads(out2)["task_id"])
    assert row["routing_decision"]["selected_agent_id"] == "codex:default"
    assert row["agent_id"] == "codex:default"
    await catalog.close()
    await tm.close()
