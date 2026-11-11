"""Loopback-only proxy adapting OpenHands request limits to LLaMA-Factory 0.9.5."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def translate_completion_request(payload):
    if not isinstance(payload, dict):
        raise ValueError('chat completion request must be a JSON object')
    translated = dict(payload)
    completion_limit = translated.pop('max_completion_tokens', None)
    if translated.get('max_tokens') is None and completion_limit is not None:
        translated['max_tokens'] = completion_limit
    return translated


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = 'OpenHandsLlamaFactoryCompat/1.0'

    def do_GET(self):
        if self.path != '/v1/models':
            self.send_error(404, 'unsupported proxy path')
            return
        self._forward('GET', self.path, None)

    def do_POST(self):
        if self.path != '/v1/chat/completions':
            self.send_error(404, 'unsupported proxy path')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 32 * 1024 * 1024:
                raise ValueError('invalid request body size')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('chat completion request must be a JSON object')
            if payload.get('stream'):
                self.send_error(501, 'streaming is not supported by this compatibility proxy')
                return
            body = json.dumps(translate_completion_request(payload)).encode('utf-8')
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_error(400, str(exc))
            return
        self._forward('POST', self.path, body)

    def _forward(self, method, path, body):
        headers = {'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        authorization = self.headers.get('Authorization')
        if authorization:
            headers['Authorization'] = authorization
        request = Request(self.server.upstream + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.server.upstream_timeout) as response:
                status, content_type, data = response.status, response.headers.get('Content-Type', 'application/json'), response.read()
        except HTTPError as exc:
            status, content_type, data = exc.code, exc.headers.get('Content-Type', 'application/json'), exc.read()
        except (TimeoutError, URLError) as exc:
            self.send_error(502, f'upstream request failed: {exc}')
            return
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', default='http://127.0.0.1:18081')
    parser.add_argument('--listen-host', default='127.0.0.1')
    parser.add_argument('--listen-port', type=int, default=18082)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    upstream = urlsplit(args.upstream)
    if (upstream.scheme != 'http' or upstream.hostname != '127.0.0.1'
            or upstream.path not in ('', '/') or upstream.query or upstream.fragment):
        raise SystemExit('--upstream must be an http://127.0.0.1[:port] URL')
    if args.listen_host != '127.0.0.1' or not 1 <= args.listen_port <= 65535 or args.timeout <= 0:
        raise SystemExit('proxy must bind 127.0.0.1 with a valid port and positive timeout')
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ProxyHandler)
    server.daemon_threads = True
    server.upstream = args.upstream.rstrip('/')
    server.upstream_timeout = args.timeout
    print(f'OpenHands API compatibility proxy: http://{args.listen_host}:{args.listen_port}/v1 -> {server.upstream}/v1', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
