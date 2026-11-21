"""票 04 验收：Codex 用户级发现 reader（$CODEX_HOME/agents/*.toml）+ catalog 写入。

纪律：仅 tomllib 解析（不执行）；CODEX_HOME 尊重；坏文件/缺必填/内建名冲突
跳过并记 error、不阻塞同目录其余文件；自定义 agent 一律 delegated_only；
行为 hash（全文 sha256）变化 + 已路由 → 拉回 pending_review；非 user scope
如实报 discovery_unsupported（project scope 属票 06）。
本机 pytest WinError-5 中毒：自建 tests/tmp_workspace/，codex_home 注入注入目录，
绝不依赖真实 ~/.codex。
"""
from __future__ import annotations

import hashlib
import os
import shutil

from app.agents.catalog import AgentCatalog
from app.agents.codex_discovery import CodexUserDiscoveryProvider
from app.agents.discovery import (
    DISCOVERY_OK,
    DISCOVERY_UNSUPPORTED,
    AgentDiscoveryService,
)

BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace"
)

_VALID = (
    "name = \"research\"\n"
    "description = \"Deep research agent\"\n"
    "developer_instructions = \"Always verify sources.\"\n"
)


def _db(name: str) -> str:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return os.path.join(d, "tasks.db")


def _home(name: str) -> str:
    d = os.path.join(BASE, f"codex_home_{name}")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


def _agents_dir(home: str) -> str:
    d = os.path.join(home, "agents")
    os.makedirs(d, exist_ok=True)
    return d


def _write(adir: str, fname: str, content: str) -> None:
    with open(os.path.join(adir, fname), "w", encoding="utf-8", newline="") as f:  # 不转 CRLF，hash 字节精确
        f.write(content)


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------- reader 本体 ----------
async def test_missing_agents_dir_is_not_error():
    home = _home("missing")  # 只有 home，没有 home/agents
    res = await CodexUserDiscoveryProvider(codex_home=home).discover("user")
    assert res.status == DISCOVERY_OK
    assert res.agents == [] and res.errors == []


async def test_valid_toml_produces_descriptor():
    home = _home("valid")
    _write(_agents_dir(home), "research.toml", _VALID)
    res = await CodexUserDiscoveryProvider(codex_home=home).discover("user")
    assert res.status == DISCOVERY_OK and res.errors == [] and res.detail == ""
    assert len(res.agents) == 1
    d = res.agents[0]
    assert d["agent_id"] == "codex:research"
    assert d["engine"] == "codex" and d["engine_profile_id"] == "research"
    assert d["name"] == "research" and d["description"] == "Deep research agent"
    assert d["source_scope"] == "user" and d["source_id"] == "research"
    assert d["invocation_mode"] == "delegated_only"
    assert d["profile_hash"] == _hash(_VALID)
    assert os.path.basename(d["source_path"]) == "research.toml"
    assert d["metadata"] == {"source_file": "research.toml", "has_developer_instructions": True}
    assert res.change_kind == "behavior"


async def test_bad_toml_skipped_with_error_other_files_still_found():
    home = _home("bad")
    adir = _agents_dir(home)
    _write(adir, "broken.toml", "name = [unclosed")
    _write(adir, "ok.toml", _VALID.replace("research", "alpha"))
    res = await CodexUserDiscoveryProvider(codex_home=home).discover("user")
    assert res.status == DISCOVERY_OK
    assert [a["agent_id"] for a in res.agents] == ["codex:alpha"]
    assert len(res.errors) == 1
    assert res.errors[0]["agent"] == "broken.toml"
    assert "TOMLDecodeError" in res.errors[0]["detail"]


async def test_missing_required_keys_skipped():
    home = _home("missingkeys")
    _write(_agents_dir(home), "noidx.toml", "name = \"x\"\ndescription = \"d\"\n")
    res = await CodexUserDiscoveryProvider(codex_home=home).discover("user")
    assert res.status == DISCOVERY_OK
    assert res.agents == []
    assert any("developer_instructions" in e["detail"] for e in res.errors)


async def test_builtin_name_conflict_skipped():
    home = _home("conflict")
    _write(_agents_dir(home), "default.toml", _VALID.replace("research", "default"))
    res = await CodexUserDiscoveryProvider(codex_home=home).discover("user")
    assert res.agents == []
    assert len(res.errors) == 1 and "default" in res.errors[0]["detail"]


async def test_project_scope_reported_unsupported():
    home = _home("scope")
    res = await CodexUserDiscoveryProvider(codex_home=home).discover("project")
    assert res.status == DISCOVERY_UNSUPPORTED


def test_default_home_respects_codex_env(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "C:/fake_cx")
    prov = CodexUserDiscoveryProvider()  # 未注入 → 走 env
    assert prov._agents_dir().as_posix() == "C:/fake_cx/agents"


# ---------- 结果 → catalog 写入（AgentDiscoveryService 链路） ----------
async def test_discovered_agent_pending_review_by_default():
    db = _db("cx_default")
    home = _home("cat_default")
    _write(_agents_dir(home), "research.toml", _VALID)
    cat = AgentCatalog(db)  # auto_enable_user_profiles 默认 False
    await cat.open()
    svc = AgentDiscoveryService(cat)
    svc.register_provider(CodexUserDiscoveryProvider(codex_home=home))
    report = await svc.discover("user")
    assert report["upserted"] == 1 and report["errors"] == [] and report["unsupported"] == []
    assert report["engines"] == [
        {"engine": "codex", "status": "ok", "count": 1, "file_errors": 0}
    ]
    a = await cat.get_agent("codex:research")
    assert a["discovery_status"] == "pending_review"
    await cat.close()


async def test_auto_enable_user_profiles_initially_available():
    db = _db("cx_auto")
    home = _home("cat_auto")
    _write(_agents_dir(home), "research.toml", _VALID)
    cat = AgentCatalog(db, auto_enable_user_profiles=True)
    await cat.open()
    svc = AgentDiscoveryService(cat)
    svc.register_provider(CodexUserDiscoveryProvider(codex_home=home))
    await svc.discover("user")
    assert (await cat.get_agent("codex:research"))["discovery_status"] == "available"
    await cat.close()


async def test_behavior_change_pulls_back_to_pending_review():
    db = _db("cx_behavior")
    home = _home("cat_behavior")
    adir = _agents_dir(home)
    _write(adir, "research.toml", _VALID)
    cat = AgentCatalog(db)
    await cat.open()
    svc = AgentDiscoveryService(cat)
    svc.register_provider(CodexUserDiscoveryProvider(codex_home=home))
    await svc.discover("user")
    assert (await cat.get_agent("codex:research"))["discovery_status"] == "pending_review"
    await cat.transition_agent("codex:research", "available")  # 审批通过
    _write(adir, "research.toml", _VALID + "\n# instructions tweaked\n")  # hash 变
    await svc.discover("user")  # change_kind=behavior + hash 变化 → 拉回
    assert (await cat.get_agent("codex:research"))["discovery_status"] == "pending_review"
    await cat.close()
