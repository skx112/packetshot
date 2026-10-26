"""
packetshot.cli —— 把 HTTP 请求/响应数据包渲染成截图（渗透报告用）。

思路提取自「全链哨兵」的 Agent 渲染发包截图功能：
  1. 不依赖 Burp/Yakit 等客户端，用内置 Web 页面渲染数据包；
  2. 无头浏览器（Playwright + Chromium）加载页面并截图；
  3. 可选真实重放请求（--send），截图里是真实响应，不是臆想数据；
  4. 支持高亮 payload、自动带北京时间水印。

退出码：0 成功；1 数据包/网络/渲染错误；2 参数错误。
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import socket
import ssl
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import __version__

__all__ = ["main"]

BEIJING = timezone(timedelta(hours=8))
VIEWER = Path(__file__).with_name("viewer.html")
DEFAULT_MAX_LINES = 500


# ─────────────────────────── 基础工具 ───────────────────────────

def beijing_now() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S") + " (UTC+8)"


def read_bytes(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    return Path(path).read_bytes()


def read_text(path: str) -> str:
    return read_bytes(path).decode("utf-8", errors="replace")


def normalize_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


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


@dataclass
class SendResult:
    raw: str               # 用于展示的响应（压缩/分块/二进制已转为可读形式）
    byte_count: int        # 实际收到的原始字节数
    elapsed_ms: float | None
    headers: dict = field(default_factory=dict)
    note: str | None = None
    incomplete: bool = False


# ─────────────────────────── 解析 ───────────────────────────

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


def split_host_port(host_header: str) -> tuple[str, int | None]:
    """解析 Host 头。支持 example.com、example.com:8443、[::1]:8080、[::1]、::1。"""
    h = host_header.strip()
    if not h:
        return "", None
    if h.startswith("["):
        end = h.find("]")
        if end < 0:
            return h.strip("[]"), None
        host = h[1:end]
        rest = h[end + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, None
    if h.count(":") > 1:          # 裸 IPv6，无端口
        return h, None
    if ":" in h:
        host, _, port = h.rpartition(":")
        if port.isdigit():
            return host, int(port)
    return h, None


def parse_headers(head: bytes) -> dict:
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        k, sep, v = line.partition(b":")
        if not sep:
            continue
        headers[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
    return headers


def parse_status_line(raw_resp: str) -> tuple[str, str]:
    first = raw_resp.replace("\r\n", "\n").split("\n", 1)[0]
    m = re.match(r"^HTTP/[\d.]+\s+(\d{3}.*)$", first.strip())
    return (m.group(1).strip(), first.strip()) if m else ("-", first.strip())


# ─────────────────────── 响应体可读化处理 ───────────────────────

def _chunked_complete(data: bytes) -> bool:
    i = 0
    while True:
        j = data.find(b"\r\n", i)
        if j < 0:
            return False
        try:
            size = int(data[i:j].split(b";")[0].strip(), 16)
        except ValueError:
            return False
        i = j + 2
        if size == 0:
            return len(data) >= i + 2
        if len(data) < i + size + 2:
            return False
        i += size + 2


def dechunk(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while True:
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(data[i:j].split(b";")[0].strip(), 16)
        except ValueError:
            break
        i = j + 2
        if size == 0:
            break
        out += data[i:i + size]
        i += size + 2
    return bytes(out)


def hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hexs = " ".join(f"{b:02x}" for b in chunk).ljust(width * 3 - 1)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08x}  {hexs}  |{asc}|")
    return "\n".join(lines)


def _looks_binary(data: bytes) -> bool:
    sample = data[:4096]
    if not sample:
        return False
    ctrl = sum(1 for b in sample if b < 9 or (13 < b < 32) or b == 127)
    return ctrl / len(sample) > 0.10


def render_body(headers: dict, body: bytes, chunked: bool) -> tuple[str, str | None]:
    """把响应体转成适合截图展示的文本。返回 (文本, 备注)。"""
    notes = []
    data = body
    if chunked:
        data = dechunk(data)
        if data or b"0\r\n" in body:
            notes.append("chunked 已解码")
    enc = headers.get("content-encoding", "").lower().strip()
    if enc:
        try:
            if enc in ("gzip", "x-gzip"):
                data = gzip.decompress(data)
                notes.append("gzip 已解压")
            elif enc == "deflate":
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    data = zlib.decompress(data, -15)
                notes.append("deflate 已解压")
            elif enc in ("br", "brotli"):
                import brotli  # 可选依赖
                data = brotli.decompress(data)
                notes.append("br 已解压")
        except Exception:
            notes.append(f"{enc} 解压失败，按原始字节展示")
    if data and _looks_binary(data):
        text = (f"[二进制响应：{len(data)} 字节，显示前 {min(len(data), 512)} 字节]\n"
                + hexdump(data[:512]))
        notes.append("二进制已转 hexdump")
    else:
        text = data.decode("utf-8", errors="replace")
    return text, (" · ".join(notes) if notes else None)


def prepare_static_response(raw_bytes: bytes) -> tuple[str, str | None]:
    if not raw_bytes.strip():
        raise ValueError("响应文件为空")
    # 头部/正文分隔兼容 CRLF 与 LF；只在头部段落内归一化，绝不动 body 字节
    idx_crlf = raw_bytes.find(b"\r\n\r\n")
    idx_lf = raw_bytes.find(b"\n\n")
    if idx_crlf >= 0 and (idx_lf < 0 or idx_crlf < idx_lf):
        head, body = raw_bytes[:idx_crlf], raw_bytes[idx_crlf + 4:]
    elif idx_lf >= 0:
        head, body = raw_bytes[:idx_lf], raw_bytes[idx_lf + 2:]
    else:
        raise ValueError("响应文件缺少头部/正文分隔（空行）")
    head = head.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    headers = parse_headers(head)
    chunked = "chunked" in headers.get("transfer-encoding", "").lower()
    text, note = render_body(headers, body, chunked)
    display = head + b"\r\n\r\n" + text.encode("utf-8", errors="replace")
    return display.decode("utf-8", errors="replace"), note


# ─────────────────────────── 发送 ───────────────────────────

def _proxy_parts(proxy: str) -> tuple[str, int, str | None]:
    url = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    host = url.hostname or ""
    if not host:
        raise ValueError(f"代理地址无效: {proxy!r}（示例：http://127.0.0.1:8080）")
    port = url.port or 8080
    auth = None
    if url.username:
        token = f"{url.username}:{url.password or ''}".encode()
        auth = "Basic " + base64.b64encode(token).decode()
    return host, port, auth


def _recv_some(sock: socket.socket) -> bytes:
    """超时/对端关闭都按 EOF 处理，便于返回已收到的部分数据。"""
    try:
        return sock.recv(65536)
    except (socket.timeout, ssl.SSLError, ConnectionResetError):
        return b""


def _read_http_response(sock: socket.socket) -> tuple[bytes, dict, bool]:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = _recv_some(sock)
        if not chunk:
            break
        buf += chunk
    if b"\r\n\r\n" not in buf:
        return bytes(buf), {}, False

    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    headers = parse_headers(head)
    te = headers.get("transfer-encoding", "").lower()
    is_chunked = "chunked" in te
    cl = headers.get("content-length", "").strip()
    body = bytearray(rest)

    if is_chunked:
        while not _chunked_complete(bytes(body)):
            chunk = _recv_some(sock)
            if not chunk:
                break
            body += chunk
    elif cl.isdigit():
        need = int(cl)
        while len(body) < need:
            chunk = _recv_some(sock)
            if not chunk:
                break
            body += chunk
        del body[need:]           # 丢弃 keep-alive 连接上多读到的字节
    else:
        while True:
            chunk = _recv_some(sock)
            if not chunk:
                break
            body += chunk
    return head + b"\r\n\r\n" + bytes(body), headers, is_chunked


def _connect(host: str, port: int, scheme: str, timeout: float,
             proxy: str | None) -> socket.socket:
    if proxy:
        phost, pport, pauth = _proxy_parts(proxy)
        sock = socket.create_connection((phost, pport), timeout=timeout)
        if scheme == "https":
            req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if pauth:
                req += f"Proxy-Authorization: {pauth}\r\n"
            sock.sendall((req + "\r\n").encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = _recv_some(sock)
                if not chunk:
                    sock.close()
                    raise ValueError("代理 CONNECT 失败：连接被关闭")
                resp += chunk
            first = resp.split(b"\r\n", 1)[0].decode("latin-1")
            if not re.match(r"HTTP/[\d.]+\s+2\d\d", first):
                sock.close()
                raise ValueError(f"代理 CONNECT 失败：{first}")
        return sock
    return socket.create_connection((host, port), timeout=timeout)


def send_request(req: HttpRequest, scheme: str, timeout: float,
                 verify_tls: bool, proxy: str | None) -> SendResult:
    """原样重放请求。强制 Connection: close 以便确定响应边界。"""
    host_header = req.host_header
    if not host_header:
        raise ValueError("请求缺少 Host 头，无法重放")
    host, host_port = split_host_port(host_header)
    if not host:
        raise ValueError(f"无法解析 Host 头: {host_header!r}")
    port = host_port if host_port else (443 if scheme == "https" else 80)

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

    if proxy and scheme == "http":
        # 走代理时请求行使用 absolute-form；有凭据则带上 Proxy-Authorization
        first = re.match(r"^([A-Z]+)\s+(.+?)\s+(HTTP/[\d.]+)\s*$", out_lines[0])
        if first and not first.group(2).startswith(("http://", "https://")):
            out_lines[0] = f"{first.group(1)} http://{host_header}{first.group(2)} {first.group(3)}"
        _, _, pauth = _proxy_parts(proxy)
        if pauth:
            out_lines.append(f"Proxy-Authorization: {pauth}")

    wire = "\r\n".join(out_lines) + "\r\n\r\n" + body

    t0 = time.perf_counter()
    sock = _connect(host, port, scheme, timeout, proxy)
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            if not verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        sock.sendall(wire.encode("utf-8", errors="replace"))
        raw_bytes, headers, chunked = _read_http_response(sock)
    finally:
        sock.close()
    elapsed = (time.perf_counter() - t0) * 1000

    if not raw_bytes.strip():
        raise ValueError(f"未在 {timeout:g}s 内收到任何响应（超时或目标不可达）")
    status, status_line = parse_status_line(raw_bytes.decode("utf-8", errors="replace"))
    if status == "-":
        snippet = raw_bytes[:80].decode("utf-8", errors="replace")
        raise ValueError(f"响应不是有效的 HTTP 报文：{snippet!r}")

    head_b, _, body_b = raw_bytes.partition(b"\r\n\r\n")
    text, note = render_body(headers, body_b, chunked)
    display = (head_b + b"\r\n\r\n" + text.encode("utf-8", errors="replace"))
    incomplete = False
    cl = headers.get("content-length", "").strip()
    if cl.isdigit() and len(body_b) < int(cl):
        incomplete = True
        note = (note + " · " if note else "") + "响应不完整（超时中断）"
    elif chunked and not _chunked_complete(body_b):
        incomplete = True
        note = (note + " · " if note else "") + "响应不完整（超时中断）"
    return SendResult(display.decode("utf-8", errors="replace"),
                      len(raw_bytes), elapsed, headers, note, incomplete)


# ─────────────────────────── 载荷与渲染 ───────────────────────────

def truncate_raw(raw: str, max_lines: int) -> tuple[str, int]:
    if max_lines <= 0:
        return raw, 0
    norm = raw.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    lines = norm.split("\n")
    if len(lines) <= max_lines:
        return raw, 0
    omitted = len(lines) - max_lines
    marker = f"… 已省略 {omitted} 行（--max-lines 0 显示全部）"
    return "\n".join(lines[:max_lines]) + "\n" + marker, omitted


def build_payload(req: HttpRequest, scheme: str, resp: SendResult | None,
                  resp_note: str | None, resp_bytes: int,
                  highlights: list[dict], title: str | None,
                  max_lines: int, wrap: bool) -> dict:
    req_display, req_omitted = truncate_raw(req.raw, max_lines)
    data = {
        "title": title,
        "beijingTime": beijing_now(),
        "highlights": highlights,
        "wrap": wrap,
        "request": {
            "raw": req_display,
            "method": req.method,
            "host": req.host_header,
            "url": req.url(scheme),
            "omitted": req_omitted,
        },
    }
    if resp is not None:
        resp_display, resp_omitted = truncate_raw(resp.raw, max_lines)
        status, status_line = parse_status_line(resp.raw)
        note = resp.note if resp.note else resp_note
        if resp_omitted:
            note = (note + " · " if note else "") + f"已截断 {resp_omitted} 行"
        data["response"] = {
            "raw": resp_display,
            "status": status,
            "statusLine": status_line,
            "elapsedMs": None if resp.elapsed_ms is None else f"{resp.elapsed_ms:.0f}",
            "bytes": resp_bytes,
            "url": req.url(scheme),
            "note": note,
        }
    return data


def render_png(payload: dict, out_path: str, width: int) -> None:
    from playwright.sync_api import sync_playwright

    # 关键修复：转义 "<"，避免数据包内的 </script> 提前闭合脚本标签
    data_js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    html = VIEWER.read_text(encoding="utf-8").replace(
        "/*__PACKET_DATA__*/", "window.__PACKET__ = " + data_js + ";")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 600},
                                    device_scale_factor=2)
            page.set_content(html)
            page.wait_for_selector("#app .footer", timeout=10000)
            page.locator("#app").screenshot(path=out_path)
        except Exception as e:   # 页面脚本异常/渲染超时统一转为友好错误
            raise RuntimeError(f"渲染失败：{str(e).splitlines()[0]}") from e
        finally:
            browser.close()


# ─────────────────────────── CLI ───────────────────────────

def _resolve_schemes(req: HttpRequest, explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    _, port = split_host_port(req.host_header)
    if port is not None:
        return ["https" if port == 443 else "http"]
    return ["https", "http"]      # 无端口时先试 https，失败回退 http


def run(args: argparse.Namespace) -> int:
    raw_req = read_text(args.req).rstrip("\r\n") + "\r\n"
    req = parse_request(raw_req)

    highlights = []
    for group in args.hl:
        for item in group.split(","):
            item = item.strip()
            if not item:
                continue
            if args.hl_regex:
                try:
                    re.compile(item)
                except re.error as e:
                    raise ValueError(f"无效的正则表达式: {item!r}（{e}）")
            highlights.append({"text": item, "regex": args.hl_regex})

    resp: SendResult | None = None
    resp_note: str | None = None
    resp_bytes = 0
    scheme_used = args.scheme or ("https" if (
        split_host_port(req.host_header)[1] == 443) else "http")

    if args.send:
        if not req.host_header:
            raise ValueError("请求缺少 Host 头，无法重放")
        schemes = _resolve_schemes(req, args.scheme)
        last_err: Exception | None = None
        for scheme in schemes:
            print(f"[*] 重放 {req.method} {req.url(scheme)} ...", file=sys.stderr)
            try:
                resp = send_request(req, scheme, args.timeout,
                                    args.verify_tls, args.proxy)
            except (OSError, ssl.SSLError) as e:
                last_err = e
                if len(schemes) > 1:
                    print(f"[!] {scheme} 失败：{e}，尝试其他协议", file=sys.stderr)
                continue
            except ValueError as e:
                if str(e).startswith("未在") and len(schemes) > 1:
                    last_err = e
                    print(f"[!] {scheme} 失败：{e}，尝试其他协议", file=sys.stderr)
                    continue
                raise
            scheme_used = scheme
            break
        if resp is None:
            if isinstance(last_err, OSError):
                raise RuntimeError(f"网络错误：{last_err}")
            raise RuntimeError(str(last_err) if last_err else "重放失败")

        # http 收到 https 重定向时，自动按 https 重试一次。
        # 仅当 Host 未显式指定端口时重试，避免把 80 端口当 TLS 端口用。
        loc = resp.headers.get("location", "")
        if (args.scheme is None and scheme_used == "http"
                and split_host_port(req.host_header)[1] is None
                and re.match(r"https://", loc, re.I)):
            status = parse_status_line(resp.raw)[0]
            if status[:3] in ("301", "302", "307", "308"):
                print(f"[*] 跟随协议重定向，改用 https 重放 ...", file=sys.stderr)
                try:
                    resp = send_request(req, "https", args.timeout,
                                        args.verify_tls, args.proxy)
                    scheme_used = "https"
                except (OSError, ssl.SSLError, ValueError) as e:
                    print(f"[!] https 重放失败：{e}", file=sys.stderr)

        status, _ = parse_status_line(resp.raw)
        timing = "" if resp.elapsed_ms is None else f" · {resp.elapsed_ms:.0f} ms"
        print(f"[+] 响应 {status} · {resp.byte_count} B{timing}"
              + (f" · {resp.note}" if resp.note else ""), file=sys.stderr)
        resp_bytes = resp.byte_count
    elif args.resp:
        raw_bytes = read_bytes(args.resp)
        display, resp_note = prepare_static_response(raw_bytes)
        resp = SendResult(display, len(raw_bytes), None)
        resp_bytes = len(raw_bytes)

    payload = build_payload(req, scheme_used, resp, resp_note, resp_bytes,
                            highlights, args.title, args.max_lines, args.wrap)

    out = args.out or f"packetshot-{datetime.now(BEIJING):%Y%m%d-%H%M%S}.png"
    render_png(payload, out, args.width)
    print(f"[+] 截图已保存: {out}", file=sys.stderr)
    print(out)
    return 0


def _positive_int(value: str) -> int:
    v = int(value)
    if v <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return v


def _nonneg_int(value: str) -> int:
    v = int(value)
    if v < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return v


def _positive_float(value: str) -> float:
    v = float(value)
    if v <= 0:
        raise argparse.ArgumentTypeError("必须是正数")
    return v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把 HTTP 数据包渲染成带北京时间的报告截图")
    ap.add_argument("--version", action="version", version=f"packetshot {__version__}")
    ap.add_argument("--req", required=True,
                    help="原始请求文件（Burp Copy to file 格式），'-' 表示 stdin")
    ap.add_argument("--resp", help="原始响应文件；与 --send 二选一")
    ap.add_argument("--send", action="store_true", help="真实重放请求并用真实响应渲染")
    ap.add_argument("--scheme", choices=["http", "https"], default=None,
                    help="重放协议，默认按端口猜测（无端口时先试 https 再回退 http）")
    ap.add_argument("--proxy", default=None,
                    help="重放代理，如 http://127.0.0.1:8080（支持 user:pass@host:port）")
    ap.add_argument("--verify-tls", action="store_true",
                    help="校验 TLS 证书（默认不校验，方便自签/内网目标）")
    ap.add_argument("--hl", action="append", default=[],
                    help="要高亮的字符串，逗号分隔，可重复传入")
    ap.add_argument("--hl-regex", action="store_true",
                    help="把 --hl 的参数当作正则表达式")
    ap.add_argument("--title", help="截图左上角标题，默认 PacketShot")
    ap.add_argument("--max-lines", type=_nonneg_int, default=DEFAULT_MAX_LINES,
                    help=f"请求/响应各最多展示的行数，0 表示不限制（默认 {DEFAULT_MAX_LINES}）")
    ap.add_argument("--wrap", action="store_true", help="长行自动换行（默认保持原始行宽）")
    ap.add_argument("-o", "--out", default=None, help="输出 PNG 路径")
    ap.add_argument("--width", type=_positive_int, default=1180, help="渲染视口宽度（px）")
    ap.add_argument("--timeout", type=_positive_float, default=15, help="重放超时（秒）")
    args = ap.parse_args(argv)

    if args.resp and args.send:
        ap.error("--resp 与 --send 只能选一个")

    try:
        return run(args)
    except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
        print(f"[!] 文件错误：{e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1
    except (OSError, ssl.SSLError) as e:
        print(f"[!] 网络错误：{e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
