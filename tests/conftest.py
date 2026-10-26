import gzip
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from packetshot.cli import send_request, parse_request  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if self.path == "/plain":
            body = b"hello packetshot"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/gzip":
            body = gzip.compress(b'{"msg":"gzip body readable","n":42}')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
            self.close_connection = True
        elif self.path == "/slow":
            time.sleep(5)
            body = b"late"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/binary":
            body = b"\x00\x01\x02PNG\xff\xfe\x00data"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    do_POST = do_GET


@pytest.fixture(scope="session")
def browser():
    """确认 Playwright/Chromium 可用，并复用同一个浏览器实例。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="session")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture(scope="session")
def base_port(server):
    return server.server_address[1]


class _Proxy(threading.Thread):
    """极简 HTTP 转发代理，记录收到的请求行，用于验证 absolute-form。"""

    def __init__(self, target_host, target_port):
        super().__init__(daemon=True)
        self.target_host = target_host
        self.target_port = target_port
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.request_line = None
        self.error = None

    def run(self):
        try:
            conn, _ = self.sock.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            self.request_line = lines[0].decode()
            method, url, version = self.request_line.split()
            from urllib.parse import urlparse
            u = urlparse(url)
            path = (u.path or "/") + (("?" + u.query) if u.query else "")
            lines[0] = f"{method} {path} {version}".encode()

            up = socket.create_connection((self.target_host, self.target_port), timeout=10)
            up.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + body)
            while True:
                chunk = up.recv(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
            up.close()
            conn.close()
        except Exception as e:  # pragma: no cover
            self.error = e
        finally:
            self.sock.close()


@pytest.fixture()
def proxy():
    return _Proxy
