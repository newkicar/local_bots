"""验收总跑真服务器（票 08）：fake/fakelong 驱动状态机，codex/jarvis 为真实引擎。

本文件不以 test_ 开头，pytest 不收集。由 tests/acceptance08.py 通过子进程启动：
    python -m uvicorn tests.acceptance_server:app --host 127.0.0.1 --port 18900
LD_* 环境变量全部由驱动注入（独立数据目录 tests/tmp_workspace/acceptance/），
服务仅监听回环，永不暴露公网（公网通道 = 票 09，deferred）。
"""
from app.adapters.codex import CodexAdapter
from app.adapters.jarvis import JarvisAdapter
from app.main import create_app
from app.settings import Settings
from tests.fakes import FakeEngine

_settings = Settings(_env_file=None)
app = create_app(
    _settings,
    {
        "fake": FakeEngine(delay=1.0),
        "fakelong": FakeEngine(delay=45.0),
        "codex": CodexAdapter(cli=_settings.ld_codex_cli),
        "jarvis": JarvisAdapter(_settings.checkpoints_db_path),
    },
)