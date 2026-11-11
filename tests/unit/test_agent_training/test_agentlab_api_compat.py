import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.request import ProxyHandler as DirectProxyHandler, Request, build_opener

import pytest

from everos.agent_training.agentlab_api_compat import (
    translate_completion_request,
    validate_proxy_endpoints,
    validate_proxy_request,
    ProxyHandler,
)


def test_completion_limit_is_renamed_without_mutating_request():
    request = {
        "model": "qwen",
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 256,
        "temperature": 0,
    }
    original = copy.deepcopy(request)

    translated = translate_completion_request(request)

    assert translated == {
        "model": "qwen",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 256,
        "temperature": 0,
    }
    assert request == original


def test_existing_max_tokens_takes_precedence():
    assert translate_completion_request(
        {"max_tokens": 64, "max_completion_tokens": 256}
    ) == {"max_tokens": 64}


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/v1/models", None),
        ("POST", "/v1/chat/completions", {"stream": False}),
    ],
)
def test_only_supported_nonstreaming_routes_are_forwarded(method, path, payload):
    assert validate_proxy_request(method, path, payload) == payload


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/admin", None),
        ("POST", "/v1/completions", {}),
        ("POST", "/v1/chat/completions", {"stream": True}),
    ],
)
def test_proxy_rejects_unsupported_routes_and_streaming(method, path, payload):
    with pytest.raises(ValueError):
        validate_proxy_request(method, path, payload)


def test_proxy_requires_loopback_bind_and_upstream():
    validate_proxy_endpoints("127.0.0.1", "http://127.0.0.1:18081")
    with pytest.raises(ValueError):
        validate_proxy_endpoints("0.0.0.0", "http://127.0.0.1:18081")
    with pytest.raises(ValueError):
        validate_proxy_endpoints("127.0.0.1", "https://example.org/api")


def test_live_proxy_forwards_translated_payload_to_loopback_upstream():
    seen = {}

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["payload"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    proxy.upstream = f"http://127.0.0.1:{upstream.server_port}"
    proxy.upstream_timeout = 5
    proxy_thread = Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
            data=json.dumps({"model": "qwen", "max_completion_tokens": 32}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with build_opener(DirectProxyHandler({})).open(request, timeout=5) as response:
            assert json.loads(response.read()) == {"ok": True}
        assert seen == {
            "path": "/v1/chat/completions",
            "payload": {"model": "qwen", "max_tokens": 32},
        }
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
