"""票 01 验收：全部走 HTTP API 外部行为，假引擎驱动状态机（不 import 任何真实引擎模块）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.fakes import FakeEngine

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "tmp_workspace")
TERMINAL = ("reported", "failed", "cancelled", "rejected")


def _settings(name: str, **kw) -> Settings:
    d = os.path.join(BASE, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return Settings(
        ld_data_dir=os.path.join(d, "data"),
        ld_workspace_dir=os.path.join(d, "workspace"),
        ld_api_token="test-token",
        _env_file=None,
        **kw,
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _submit(c: TestClient, instruction: str = "do the thing", engine: str | None = "fake") -> dict:
    body: dict = {"instruction": instruction}
    if engine is not None:
        body["engine"] = engine
    r = c.post("/api/tasks", json=body, headers=_headers())
    assert r.status_code == 201, r.text
    return r.json()


def _wait_terminal(c: TestClient, tid: str, want=TERMINAL, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
        if t["status"] in want:
            return t
        time.sleep(0.1)
    raise AssertionError(f"task {tid} 未在 {timeout}s 内到达 {want}: {t['status']}")


def test_auth_rejects_and_allows():
    settings = _settings("auth")
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/tasks").status_code == 401
        assert c.get("/api/tasks", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/tasks", headers=_headers()).status_code == 200
        assert c.get("/api/tasks", params={"token": "test-token"}).status_code == 200
        assert c.get("/api/health", headers=_headers()).status_code == 200
        r = c.post("/api/tasks", json={"instruction": "default engine"}, headers=_headers())
        assert r.status_code == 201
        assert r.json()["engine"] == "jarvis"  # 默认引擎（ADR-0001）


def test_submit_and_lifecycle_thread_binding():
    fake = FakeEngine()
    settings = _settings("lifecycle")
    with TestClient(create_app(settings, {"fake": fake})) as c:
        t = _submit(c)
        tid = t["task_id"]
        assert tid.startswith("T")
        assert t["status"] == "queued"
        assert t["engine"] == "fake"  # 显式提交引擎（假引擎驱动）
        assert t["engine_thread_id"] is None
        assert t["workspace"] is None
        assert t["parent_task_id"] is None
        t = _wait_terminal(c, tid)
        assert t["status"] == "reported"
        assert t["engine_thread_id"] == f"thread-{tid}"  # 建 Thread 后立即写库
        assert (t["result"] or "").startswith("fake result")
        assert len(t["runs"]) == 1 and t["runs"][0]["state"] == "completed"
        assert len(fake.executed) == 1 and fake.executed[0][0] == tid


def test_submit_fields_roundtrip():
    fake = FakeEngine()
    with TestClient(create_app(_settings("roundtrip"), {"fake": fake})) as c:
        r = c.post(
            "/api/tasks",
            json={"instruction": "i", "engine": "fake", "workspace": "D:\\ws", "parent_task_id": "TP"},
            headers=_headers(),
        )
        assert r.status_code == 201
        t = r.json()
        assert t["engine"] == "fake" and t["workspace"] == "D:\\ws" and t["parent_task_id"] == "TP"
        assert t["instruction"] == "i"


def test_followup_active_409_then_new_run():
    fake = FakeEngine(delay=2.0)
    with TestClient(create_app(_settings("followup"), {"fake": fake})) as c:
        t = _submit(c)
        tid = t["task_id"]
        first_run = t["runs"][0]["run_id"]
        # 活跃状态（queued/running 都算）追问 → 409
        assert c.post(f"/api/tasks/{tid}/followup", json={"instruction": "again"},
                      headers=_headers()).status_code == 409
        _wait_terminal(c, tid, timeout=30)
        r = c.post(f"/api/tasks/{tid}/followup", json={"instruction": "again"}, headers=_headers())
        assert r.status_code == 200, r.text
        runs = r.json()["runs"]
        assert [x["attempt"] for x in runs] == [1, 2]
        assert [x["origin"] for x in runs] == ["submitted", "followup"]
        assert runs[0]["run_id"] == first_run  # 旧 Run 不覆盖
        assert r.json()["status"] == "queued"
        assert _wait_terminal(c, tid, timeout=30)["status"] == "reported"
        assert len(fake.executed) == 2


def test_concurrency_capped_and_fulfilled():
    fake = FakeEngine(delay=0.3)
    settings = _settings("concurrent", ld_max_concurrent_runs=2)
    with TestClient(create_app(settings, {"fake": fake})) as c:
        tids = [_submit(c, f"task-{i}")["task_id"] for i in range(4)]
        for tid in tids:
            assert _wait_terminal(c, tid)["status"] == "reported"
        assert fake.high_water == 2  # 上限被遵守
        assert len(fake.executed) == 4  # 排队者随后被执行


def test_cancel_running_task():
    fake = FakeEngine(delay=3.0)
    with TestClient(create_app(_settings("cancel"), {"fake": fake})) as c:
        t = _submit(c)
        tid = t["task_id"]
        deadline = time.time() + 15
        while time.time() < deadline:
            cur = c.get(f"/api/tasks/{tid}", headers=_headers()).json()
            if cur["status"] == "running":
                break
            time.sleep(0.1)
        r = c.post(f"/api/tasks/{tid}/cancel", headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["task_id"] == tid and body["status"] == "cancelled"
        assert body["ledger"][0]["state"] == "cancelled"
        assert c.get(f"/api/tasks/{tid}", headers=_headers()).json()["status"] == "cancelled"
        assert c.post(f"/api/tasks/{tid}/cancel", headers=_headers()).status_code == 409


def test_failing_engine_marks_task_failed():
    fake = FakeEngine(fail=True)
    with TestClient(create_app(_settings("fail"), {"fake": fake})) as c:
        t = _submit(c)
        t = _wait_terminal(c, t["task_id"], want=("failed",))
        assert t["status"] == "failed"
        assert t["runs"][0]["state"] == "failed" and t["runs"][0]["error"]
        kinds = [n["kind"] for n in t["notifications"]]
        assert "task_failed" in kinds


def test_unknown_ids_404():
    fake = FakeEngine()
    with TestClient(create_app(_settings("notfound"), {"fake": fake})) as c:
        assert c.get("/api/tasks/Tnope", headers=_headers()).status_code == 404
        assert c.get("/api/runs/Rnope", headers=_headers()).status_code == 404
        assert c.post("/api/tasks/Tnope/followup", json={"instruction": "x"},
                      headers=_headers()).status_code == 404
        assert c.get("/api/notifications", params={"task_id": "Tnope"},
                     headers=_headers()).status_code == 404
        t = _submit(c)
        _wait_terminal(c, t["task_id"])
        rid = t["runs"][0]["run_id"]
        assert c.get(f"/api/runs/{rid}", headers=_headers()).status_code == 200
        assert c.get("/api/notifications", params={"task_id": t["task_id"]},
                     headers=_headers()).status_code == 200


def _start_uvicorn(settings: Settings, port: int) -> subprocess.Popen:
    """真子进程起 uvicorn（票 01 SSE 验收须走真网络流；TestClient 的 portal 会死锁无限流）。"""
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.fake_server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=os.getcwd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ,
             "LD_DATA_DIR": str(settings.ld_data_dir),
             "LD_WORKSPACE_DIR": str(settings.ld_workspace_dir),
             "LD_API_TOKEN": settings.ld_api_token,
             "LD_MAX_CONCURRENT_RUNS": str(settings.ld_max_concurrent_runs)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _wait_http(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/health",
                          headers=_headers(), timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise AssertionError(f"uvicorn 未在 {timeout}s 内就绪")


def test_sse_push_and_client_disconnect_does_not_block_state_machine():
    """SSE 推状态事件 + 客户端断开后状态机照常推进（真实回环端口，不锁线程）。"""
    fake = FakeEngine(delay=0.5)
    settings = _settings("sse")
    port = 18765
    proc = _start_uvicorn(settings, port)
    try:
        _wait_http(port)
        tid = None
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as hc:
            r = hc.post("/api/tasks", json={"instruction": "sse", "engine": "fake"},
                        headers=_headers())
            assert r.status_code == 201
            tid = r.json()["task_id"]
            # 连 SSE，读到 running 状态事件后断开
            got_running = False
            with hc.stream("GET", "/api/events", headers=_headers()) as resp:
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    if event.get("to") == "running":
                        got_running = True
                        break
            assert got_running
        # 断开后状态机照常推进到 reported
        deadline = time.time() + 20
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as hc:
            while time.time() < deadline:
                t = hc.get(f"/api/tasks/{tid}", headers=_headers()).json()
                if t["status"] in TERMINAL:
                    break
                time.sleep(0.1)
        assert t["status"] == "reported", t["status"]
        # 子进程内的假引擎产出的结果（本进程 fake 对象不可见子进程执行）
        assert (t["result"] or "").startswith("fake result")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
