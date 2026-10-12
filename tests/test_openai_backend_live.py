"""The OpenAI backend against a real socket.

Every other test of this adapter replaces `_request`, so urllib, the URL
construction, the Authorization header and the JSON handling were never
executed — and this is the fallback for anyone without a ChatGPT subscription.
A local HTTP server stands in for the API so the real request path runs.

It also covers the `patches` builder, which is how an agent with no tools of
its own writes files: the only other coverage was the mock adapter, which never
goes near the network.
"""

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class LocalServer(HTTPServer):
    """An HTTPServer that does not resolve the host's FQDN when binding.

    `HTTPServer.server_bind` calls socket.getfqdn to fill in server_name, and on
    a machine with slow reverse DNS that blocks for tens of seconds — it cost 35
    seconds per test here, which is how a useful test becomes one people skip.
    Nothing needs server_name, so bind the socket and move on.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

import pytest

from duet.config import AgentSpec, Config
from duet.orchestrator import Orchestrator

GREET = (
    "def greet(name):\n"
    "    if not name or not name.strip():\n"
    "        raise ValueError('name must not be empty')\n"
    "    return 'Hello, %s!' % name.strip()\n"
)


def _handler_for(turns, seen):
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def _json(self, payload, code=200):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.endswith("/models"):
                return self._json({"data": [{"id": "gpt-5"}]})
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            request = json.loads(raw)
            # Recorded so the test can assert the shape from the receiving end,
            # which is the half a monkeypatched _request cannot check.
            seen.append({"model": request.get("model"),
                         "messages": request.get("messages"),
                         "auth": self.headers.get("Authorization", "")})
            index = min(Handler.calls, len(turns) - 1)
            Handler.calls += 1
            envelope = turns[index]
            content = "%s\n\n```json\n%s\n```" % (envelope["message"], json.dumps(envelope))
            self._json({"model": request.get("model"),
                        "choices": [{"message": {"content": content}}]})

        def log_message(self, *args):
            pass

    return Handler


@pytest.fixture
def fake_api():
    """A real HTTP server on a port the OS picks, torn down after the test."""
    state = {"seen": [], "turns": []}

    def start(turns):
        state["turns"] = turns
        server = LocalServer(("127.0.0.1", 0), _handler_for(turns, state["seen"]))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state["server"] = server
        return "http://127.0.0.1:%d/v1" % server.server_address[1]

    yield start, state
    server = state.get("server")
    if server is not None:
        server.shutdown()
        server.server_close()


def test_the_api_backend_builds_a_file_and_signs_off(tmp_path, fake_api, monkeypatch):
    start, state = fake_api
    base = start([
        {"message": "wrote it", "verdict": "CONTINUE",
         "patches": [{"path": "greet.py", "action": "write", "content": GREET}]},
        {"message": "still happy", "verdict": "DONE", "confidence": 0.9},
    ])
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("duet.adapters.openai_api.DEFAULT_BASE_URL", base)

    cfg = Config(task="write greet.py", root=str(tmp_path), max_rounds=4,
                 agents=[AgentSpec("gpt", "openai-api"), AgentSpec("peer", "mock")])
    from duet.adapters.mock import MockAdapter, envelope

    orch = Orchestrator(cfg, adapters={
        "gpt": __import__("duet.adapters.openai_api", fromlist=["OpenAIAdapter"])
               .OpenAIAdapter(name="gpt", cwd=str(tmp_path), config={"base_url": base}),
        "peer": MockAdapter(name="peer", cwd=str(tmp_path),
                            config={"script": [envelope("looks right", "DONE", confidence=0.9)]}),
    })
    result = orch.run()

    # the file exists because the harness applied a patch from a real HTTP reply
    assert (tmp_path / "greet.py").read_text(encoding="utf-8") == GREET
    assert result.status == "consensus"

    # and the request duet sent was well formed, checked at the server
    assert state["seen"], "the adapter never reached the server"
    first = state["seen"][0]
    assert first["auth"] == "Bearer test-key"
    assert first["model"]
    assert first["messages"] and first["messages"][-1]["role"] == "user"


def test_the_api_backend_reports_a_server_error_honestly(tmp_path, fake_api, monkeypatch):
    start, _ = fake_api

    class Failing(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(500)
            self.end_headers()

        def do_POST(self):
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = LocalServer(("127.0.0.1", 0), Failing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/v1" % server.server_address[1]
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        from duet.adapters.openai_api import OpenAIAdapter

        agent = OpenAIAdapter(name="gpt", cwd=str(tmp_path),
                              config={"base_url": base, "max_retries": 1, "model": "gpt-5"})
        reply = agent.send("anything")
        assert not reply.ok
        assert "500" in reply.error
    finally:
        server.shutdown()
        server.server_close()
