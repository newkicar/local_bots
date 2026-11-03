"""测试用真服务器：注册假引擎的 uvicorn 入口（票 01 SSE 验收，需真回环网络流）。"""
from app.main import create_app
from app.settings import get_settings
from tests.fakes import FakeEngine

app = create_app(get_settings(), {"fake": FakeEngine(delay=0.5)})
