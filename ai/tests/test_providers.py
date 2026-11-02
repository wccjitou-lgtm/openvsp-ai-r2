"""providers.py 单元测试：本地 mock HTTP server 验证 /models 与 /chat/completions。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ai_design import providers
from ai_design.providers import ProviderError


class _MockAPIHandler(BaseHTTPRequestHandler):
    server_version = "MockLLM/1.0"
    fail_models = False
    captured_requests: list[dict] = []

    def log_message(self, *args):  # noqa: D401 - 静默
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            if _MockAPIHandler.fail_models:
                self._send_json({"error": {"message": "models not available"}}, 404)
                return
            self._send_json({"object": "list", "data": [
                {"id": "mock-model-a", "object": "model"},
                {"id": "mock-model-b", "object": "model"},
                {"id": "mock-model-a", "object": "model"},
            ]})
            return
        self._send_json({"error": {"message": "not found"}}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        authorization = self.headers.get("Authorization", "")
        _MockAPIHandler.captured_requests.append({
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "payload": payload,
        })
        if "unauthorized" in authorization:
            self._send_json({"error": {"message": "invalid api key"}}, 401)
            return
        if "badjson" in authorization:
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 模拟带工具调用的响应
        if payload.get("tools") and payload.get("messages", [{}])[-1].get("role") != "tool":
            self._send_json({
                "id": "chatcmpl-tool",
                "model": payload.get("model"),
                "choices": [{
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "openvsp_get_geometry",
                                "arguments": json.dumps({"foo": 1}),
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
            return
        self._send_json({
            "id": "chatcmpl-123",
            "model": payload.get("model"),
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "设计完成：L/D=12.5"},
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8},
        })


@pytest.fixture(scope="module")
def mock_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockAPIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield base_url
    server.shutdown()
    server.server_close()


def test_normalize_base_url():
    assert providers.normalize_base_url("https://api.deepseek.com/") == \
        "https://api.deepseek.com"
    assert providers.normalize_base_url("https://x.com/v1/chat/completions") == \
        "https://x.com/v1"
    assert providers.normalize_base_url("api.example.com/v1") == \
        "https://api.example.com/v1"


def test_list_models(mock_server):
    models = providers.list_models(mock_server, "sk-test")
    assert models == ["mock-model-a", "mock-model-b"]


def test_list_models_fallback(mock_server):
    _MockAPIHandler.fail_models = True
    try:
        models, source = providers.list_models_or_default(
            mock_server, "sk-test", fallback=["glm-5.3", "glm-5.2"])
        assert models == ["glm-5.3", "glm-5.2"]
        assert "builtin" in source
    finally:
        _MockAPIHandler.fail_models = False


def test_chat_plain(mock_server):
    response = providers.chat(mock_server, "sk-test", "mock-model-a",
                              [{"role": "user", "content": "hello"}])
    assert response["content"].startswith("设计完成")
    assert response["tool_calls"] == []
    assert response["finish_reason"] == "stop"
    # 请求头携带 Bearer Key
    request = _MockAPIHandler.captured_requests[-1]
    assert request["headers"]["authorization"] == "Bearer sk-test"
    assert request["payload"]["model"] == "mock-model-a"


def test_chat_tool_calls(mock_server):
    tools = [{"type": "function", "function": {"name": "openvsp_get_geometry",
                                                "parameters": {"type": "object"}}}]
    response = providers.chat(mock_server, "sk-test", "mock-model-a",
                              [{"role": "user", "content": "建模"}], tools=tools)
    assert response["finish_reason"] == "tool_calls"
    assert len(response["tool_calls"]) == 1
    call = response["tool_calls"][0]
    assert call["name"] == "openvsp_get_geometry"
    assert call["arguments"] == {"foo": 1}
    assert json.loads(call["arguments_raw"]) == {"foo": 1}
    assistant = providers.make_assistant_message(response)
    assert assistant["tool_calls"][0]["function"]["name"] == "openvsp_get_geometry"
    tool_message = providers.make_tool_message(call["id"], {"ok": True})
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_1"


def test_chat_http_error_and_redaction(mock_server):
    secret = "sk-SECRET-KEY-123456"
    with pytest.raises(ProviderError) as info:
        providers.chat(mock_server, secret + "-unauthorized", "mock-model-a",
                       [{"role": "user", "content": "x"}])
    error = info.value
    assert error.status_code == 401
    assert "invalid api key" in str(error)
    assert secret not in str(error)
    assert secret not in json.dumps(error.as_dict(), ensure_ascii=False)


def test_chat_bad_json(mock_server):
    with pytest.raises(ProviderError) as info:
        providers.chat(mock_server, "sk-badjson", "mock-model-a",
                       [{"role": "user", "content": "x"}])
    assert info.value.kind == "protocol"


def test_chat_empty_messages():
    with pytest.raises(ProviderError):
        providers.chat("http://127.0.0.1:1", "sk-test", "m", [])
