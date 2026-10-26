"""
packetshot.cli —— 把 HTTP 请求/响应数据包渲染成截图（渗透报告用）。

思路提取自「全链哨兵」的 Agent 渲染发包截图功能：
  1. 不依赖 Burp/Yakit 等客户端，用内置 Web 页面渲染数据包；
  2. 无头浏览器（Playwright + Chromium）加载页面并截图；
  3. 可选真实重放请求（--send），截图里是真实响应，不是臆想数据；
  4. 支持高亮 payload、自动带北京时间水印。
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = ["main"]

BEIJING = timezone(timedelta(hours=8))
VIEWER = Path(__file__).with_name("viewer.html")


@dataclass
class HttpRequest:
    method: str
    target: str          # origin-form path 或 absolute URI
    version: str
    headers: list[tuple[str, str]]
    body: str
    raw: str

    @property
    def host_header(self) -> str:
        for k, v in self.headers:
            if k.lower() == "host":
                return v.strip()
        return ""

    def url(self, scheme: str) -> str:
        if self.target.startswith(("http://", "https://")):
            return self.target
        return f"{scheme}://{self.host_header}{self.target}"


def beijing_now() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S") + " (UTC+8)"


def read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_bytes().decode("utf-8", errors="replace")


def normalize_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def parse_request(raw: str) -> HttpRequest:
    norm = raw.replace("\r\n", "\n").replace("\r", "\n")
    head, sep, body = norm.partition("\n\n")
    lines = head.split("\n")
    if not lines or not lines[0].strip():
        raise ValueError("请求文件为空或缺少请求行")
    # payload 里常带未编码空格，target 用非贪婪匹配，尾部锚定 HTTP/x
    m = re.match(r"^([A-Z]+)\s+(.+?)\s+(HTTP/[\d.]+)\s*$", lines[0])
    if m:
        method, target, version = m.group(1), m.group(2), m.group(3)
    else:
        m2 = re.match(r"^([A-Z]+)\s+(.+)$", lines[0])  # 省略版本的写法: GET /path
        if not m2:
            raise ValueError(f"无法解析请求行: {lines[0]!r}")
        method, target, version = m2.group(1), m2.group(2), "HTTP/1.1"
    headers = []
    for line in lines[1:]:
        if not line.strip():
            continue
        hm = re.match(r"^([^:]+):\s*(.*)$", line)
        if hm:
            headers.append((hm.group(1).strip(), hm.group(2)))
    return HttpRequest(method, target, version, headers,
                       body if sep else "", raw)


def split_host_port(host_header: str, scheme: str) -> tuple[str, int]:
    if ":" in host_header:
        host, _, port = host_header.rpartition(":")
        if port.isdigit():
            return host, int(port)
    return host_header, 443 if scheme == "https" else 80


def send_request(req: HttpRequest, scheme: str, timeout: float,
                 insecure: bool) -> tuple[str, int, float]:
    """原样重放请求，返回 (raw响应, 字节数, 耗时ms)。强制 Connection: close 以便读完全文。"""
    host_header = req.host_header
    if not host_header:
        raise ValueError("请求缺少 Host 头，无法重放")
    host, port = split_host_port(host_header, scheme)

    head, sep, body = normalize_crlf(req.raw).partition("\r\n\r\n")
    body = body.rstrip("\r\n")
    lines = head.split("\r\n")
    out_lines, has_conn, has_cl = [], False, False
    cl_value = f"Content-Length: {len(body.encode('utf-8', errors='replace'))}"
    for i, line in enumerate(lines):
        lower = line.lower()
        if i > 0 and lower.startswith("connection:"):
            out_lines.append("Connection: close")
            has_conn = True
        elif i > 0 and lower.startswith("content-length:"):
            # 粘贴来的请求 Content-Length 常与实际不符，按真实 body 重算（同 Burp 行为）
            out_lines.append(cl_value)
            has_cl = True
        else:
            out_lines.append(line)
    if not has_conn:
        out_lines.append("Connection: close")
    if body and not has_cl:
        out_lines.append(cl_value)
    wire = "\r\n".join(out_lines) + "\r\n\r\n" + body

    t0 = time.perf_counter()
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        sock.sendall(wire.encode("utf-8", errors="replace"))
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        sock.close()
    elapsed = (time.perf_counter() - t0) * 1000
    blob = b"".join(chunks)
    return blob.decode("utf-8", errors="replace"), len(blob), elapsed


def parse_status_line(raw_resp: str) -> tuple[str, str]:
    first = raw_resp.replace("\r\n", "\n").split("\n", 1)[0]
    m = re.match(r"^HTTP/[\d.]+\s+(\d{3}.*)$", first.strip())
    return (m.group(1).strip(), first.strip()) if m else ("-", first.strip())


def build_payload(req: HttpRequest, scheme: str, resp_raw: str | None,
                  elapsed_ms: float | None, resp_bytes: int,
                  highlights: list[dict], title: str | None) -> dict:
    data = {
        "title": title,
        "beijingTime": beijing_now(),
        "highlights": highlights,
        "request": {
            "raw": req.raw,
            "method": req.method,
            "host": req.host_header,
            "url": req.url(scheme),
        },
    }
    if resp_raw is not None:
        status, status_line = parse_status_line(resp_raw)
        data["response"] = {
            "raw": resp_raw,
            "status": status,
            "statusLine": status_line,
            "elapsedMs": None if elapsed_ms is None else f"{elapsed_ms:.0f}",
            "bytes": resp_bytes,
            "url": req.url(scheme),
        }
    return data


def render_png(payload: dict, out_path: str, width: int) -> None:
    from playwright.sync_api import sync_playwright

    html = VIEWER.read_text(encoding="utf-8").replace(
        "/*__PACKET_DATA__*/",
        "window.__PACKET__ = " + json.dumps(payload, ensure_ascii=False) + ";",
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 600},
                                device_scale_factor=2)
        page.set_content(html)
        page.wait_for_timeout(120)
        page.locator("#app").screenshot(path=out_path)
        browser.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把 HTTP 数据包渲染成带北京时间的报告截图")
    ap.add_argument("--req", required=True, help="原始请求文件（Burp Copy to file 格式），'-' 表示 stdin")
    ap.add_argument("--resp", help="原始响应文件；与 --send 二选一")
    ap.add_argument("--send", action="store_true", help="真实重放请求并用真实响应渲染")
    ap.add_argument("--scheme", choices=["http", "https"], default=None,
                    help="重放协议，默认按端口猜测（443→https）")
    ap.add_argument("--hl", action="append", default=[],
                    help="要高亮的字符串，逗号分隔，可重复传入")
    ap.add_argument("--hl-regex", action="store_true",
                    help="把 --hl 的参数当作正则表达式")
    ap.add_argument("--title", help="截图左上角标题，默认 PacketShot")
    ap.add_argument("-o", "--out", default=None, help="输出 PNG 路径")
    ap.add_argument("--width", type=int, default=1180, help="渲染视口宽度（px）")
    ap.add_argument("--timeout", type=float, default=15, help="重放超时（秒）")
    ap.add_argument("--insecure", action="store_true", default=True,
                    help="重放时忽略 TLS 证书校验（默认开启）")
    args = ap.parse_args(argv)

    if args.resp and args.send:
        ap.error("--resp 与 --send 只能选一个")

    raw_req = read_text(args.req).rstrip("\r\n") + "\r\n"
    req = parse_request(raw_req)

    scheme = args.scheme
    if scheme is None:
        _, port = split_host_port(req.host_header, "http")
        scheme = "https" if port == 443 else "http"

    highlights = []
    for group in args.hl:
        for item in group.split(","):
            item = item.strip()
            if item:
                highlights.append({"text": item, "regex": args.hl_regex})

    resp_raw, resp_bytes = None, 0
    elapsed: float | None = None
    if args.send:
        print(f"[*] 重放 {req.method} {req.url(scheme)} ...", file=sys.stderr)
        resp_raw, resp_bytes, elapsed = send_request(
            req, scheme, args.timeout, args.insecure)
        status, _ = parse_status_line(resp_raw)
        print(f"[+] 响应 {status} · {resp_bytes} B · {elapsed:.0f} ms",
              file=sys.stderr)
    elif args.resp:
        resp_raw = read_text(args.resp)
        resp_bytes = len(resp_raw.encode("utf-8", errors="replace"))

    payload = build_payload(req, scheme, resp_raw, elapsed, resp_bytes,
                            highlights, args.title)

    out = args.out or f"packetshot-{datetime.now(BEIJING):%Y%m%d-%H%M%S}.png"
    render_png(payload, out, args.width)
    print(f"[+] 截图已保存: {out}", file=sys.stderr)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
