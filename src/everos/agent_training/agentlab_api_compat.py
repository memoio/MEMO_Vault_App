"""Loopback-only OpenAI API compatibility shim for AgentLab and LLaMA-Factory."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler as UrllibProxyHandler, Request, build_opener


MAX_BODY_BYTES = 32 * 1024 * 1024
DIRECT_OPENER = build_opener(UrllibProxyHandler({}))


def translate_completion_request(payload):
    """Map AgentLab/OpenAI SDK's limit field to LLaMA-Factory's API field."""
    if not isinstance(payload, dict):
        raise ValueError("chat completion request must be a JSON object")
    translated = dict(payload)
    limit = translated.pop("max_completion_tokens", None)
    if translated.get("max_tokens") is None and limit is not None:
        translated["max_tokens"] = limit
    return translated


def validate_proxy_request(method, path, payload):
    if method == "GET" and path == "/v1/models":
        if payload is not None:
            raise ValueError("GET /v1/models does not accept a body")
        return None
    if method == "POST" and path == "/v1/chat/completions":
        if not isinstance(payload, dict):
            raise ValueError("chat completion request must be a JSON object")
        if payload.get("stream") is True:
            raise ValueError("streaming is not supported by this compatibility proxy")
        return translate_completion_request(payload)
    raise ValueError("unsupported proxy route")


def validate_proxy_endpoints(listen_host, upstream_url):
    upstream = urlsplit(upstream_url)
    if listen_host != "127.0.0.1":
        raise ValueError("proxy must bind to 127.0.0.1")
    if (
        upstream.scheme != "http"
        or upstream.hostname != "127.0.0.1"
        or upstream.port is None
        or upstream.path not in ("", "/")
        or upstream.query
        or upstream.fragment
        or upstream.username
        or upstream.password
    ):
        raise ValueError("upstream must be http://127.0.0.1:<port>")


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "AgentLabLlamaFactoryCompat/1.0"

    def do_GET(self):
        try:
            validate_proxy_request("GET", self.path, None)
        except ValueError:
            self.send_error(404, "unsupported proxy path")
            return
        self._forward("GET", self.path, None)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404, "unsupported proxy path")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request body size")
            payload = json.loads(self.rfile.read(length))
            translated = validate_proxy_request("POST", self.path, payload)
            body = json.dumps(translated, ensure_ascii=False).encode("utf-8")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_error(400, str(exc))
            return
        self._forward("POST", self.path, body)

    def _forward(self, method, path, body):
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        authorization = self.headers.get("Authorization")
        if authorization:
            headers["Authorization"] = authorization
        request = Request(self.server.upstream + path, data=body, headers=headers, method=method)
        try:
            with DIRECT_OPENER.open(request, timeout=self.server.upstream_timeout) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "application/json")
                data = response.read()
        except HTTPError as exc:
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json")
            data = exc.read()
        except (TimeoutError, URLError) as exc:
            self.send_error(502, f"upstream request failed: {exc}")
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        # Do not log request bodies or authorization tokens.
        super().log_message(fmt, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default="http://127.0.0.1:18081")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=18082)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    try:
        validate_proxy_endpoints(args.listen_host, args.upstream)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.listen_port <= 65535 or args.timeout <= 0:
        parser.error("port must be valid and timeout must be positive")
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ProxyHandler)
    server.daemon_threads = True
    server.upstream = args.upstream.rstrip("/")
    server.upstream_timeout = args.timeout
    print(
        f"AgentLab API proxy: http://127.0.0.1:{args.listen_port}/v1 -> {server.upstream}/v1",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
