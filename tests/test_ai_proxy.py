import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts import ai_insights

GOOD = {"impact_dir": "neg", "priority": 4, "lines": ["inv"], "why": "競品降費", "impact_text": "影響投資型商品的費用比較", "owner": "商品開發", "actions": ["比較費用"]}


class Fake(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append((self.headers.get("x-access-code"), body))
        if self.headers.get("x-access-code") != "secret":
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"unauthorized"}'); return
        text = json.dumps(GOOD, ensure_ascii=False)
        self.send_response(200); self.send_header("content-type", "application/x-ndjson"); self.end_headers()
        for i in range(0, len(text), 20):   # 串流：一行一個片段
            self.wfile.write((json.dumps({"t": text[i:i + 20]}, ensure_ascii=False) + "\n").encode())
        self.wfile.write(b'{"done":true,"stop":"end_turn"}\n')

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    s = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    Fake.seen.clear()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def test_ask_proxy_streams_and_validates(server):
    r = ai_insights.ask_proxy(server, "secret", "deepseek-v4-pro:0813", "請判讀")
    assert r["impact_dir"] == "neg" and r["priority"] == 4 and r["owner"] == "商品開發"
    code, body = Fake.seen[0]
    assert code == "secret" and body["model"] == "deepseek-v4-pro:0813" and body["messages"][0]["content"] == "請判讀"


def test_ask_proxy_wrong_password_returns_none_without_retry(server):
    assert ai_insights.ask_proxy(server, "wrong", "m", "x") is None
    assert len(Fake.seen) == 1   # 密碼錯不重試


def test_proxy_needs_both_url_and_code(monkeypatch):
    monkeypatch.setattr(ai_insights, "_env", lambda n: {"AI_PROXY_URL": "http://x/"}.get(n))
    assert ai_insights.proxy() is None
    monkeypatch.setattr(ai_insights, "_env", lambda n: {"AI_PROXY_URL": "http://x/", "AI_ACCESS_CODE": "c"}.get(n))
    assert ai_insights.proxy() == ("http://x", "c")
