import gzip
import zlib

import pytest

from packetshot.cli import (
    _chunked_complete,
    _proxy_parts,
    _resolve_schemes,
    build_payload,
    dechunk,
    hexdump,
    parse_request,
    parse_status_line,
    prepare_static_response,
    render_body,
    split_host_port,
    truncate_raw,
)


class TestParseRequest:
    def test_basic(self):
        req = parse_request(
            "GET /a HTTP/1.1\r\nHost: x.com\r\nAccept: */*\r\n\r\n")
        assert (req.method, req.target, req.version) == ("GET", "/a", "HTTP/1.1")
        assert req.host_header == "x.com"
        assert req.body == ""

    def test_payload_with_spaces_and_body(self):
        raw = ("POST /q HTTP/1.1\r\nHost: x.com\r\n\r\n"
               "u=admin' OR '1'='1'-- -&p=1")
        req = parse_request(raw)
        assert req.target == "/q"
        assert "OR '1'='1'" in req.body

    def test_target_with_unencoded_spaces(self):
        req = parse_request(
            "GET /q?id=1 UNION SELECT 1 HTTP/1.1\r\nHost: x.com\r\n\r\n")
        assert req.target == "/q?id=1 UNION SELECT 1"

    def test_no_version(self):
        req = parse_request("GET /nover\nHost: x.com\n\n")
        assert req.version == "HTTP/1.1"
        assert req.target == "/nover"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="请求文件为空"):
            parse_request("\r\n\r\n")

    def test_garbage_line_raises(self):
        with pytest.raises(ValueError, match="无法解析请求行"):
            parse_request("garbage\r\n\r\n")


class TestSplitHostPort:
    @pytest.mark.parametrize("host,expected", [
        ("example.com", ("example.com", None)),
        ("example.com:8443", ("example.com", 8443)),
        ("example.com:443", ("example.com", 443)),
        ("[::1]:8080", ("::1", 8080)),
        ("[::1]", ("::1", None)),
        ("::1", ("::1", None)),
        ("", ("", None)),
    ])
    def test_cases(self, host, expected):
        assert split_host_port(host) == expected


class TestResponseBody:
    def test_gzip(self):
        body = gzip.compress(b'{"a":1}')
        text, note = render_body({"content-encoding": "gzip"}, body, False)
        assert text == '{"a":1}'
        assert "gzip 已解压" in note

    def test_deflate(self):
        body = zlib.compress(b"deflate-body")
        text, note = render_body({"content-encoding": "deflate"}, body, False)
        assert text == "deflate-body"
        assert "deflate 已解压" in note

    def test_chunked(self):
        raw = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        text, note = render_body({}, raw, True)
        assert text == "hello world"
        assert "chunked 已解码" in note

    def test_binary_hexdump(self):
        text, note = render_body({}, b"\x00\x01\x02PNG\xff\xfe\x00data", False)
        assert "二进制响应" in text
        assert "00000000" in text
        assert "二进制已转 hexdump" in note

    def test_chunked_complete(self):
        assert _chunked_complete(b"5\r\nhello\r\n0\r\n\r\n")
        assert not _chunked_complete(b"5\r\nhel")
        assert dechunk(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n") == b"hello world"


class TestStaticResponse:
    def test_gzip_file(self):
        body = gzip.compress(b"gzip content readable")
        raw = (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        display, note = prepare_static_response(raw)
        assert "gzip content readable" in display
        assert "gzip 已解压" in note

    def test_lf_only_headers(self):
        """LF 换行的响应文件（手写/示例文件）也必须能解析。"""
        raw = (b"HTTP/1.1 200 OK\nContent-Type: text/plain\n"
               b"Content-Length: 11\n\nhello world")
        display, note = prepare_static_response(raw)
        assert "HTTP/1.1 200 OK" in display
        assert "hello world" in display
        assert note is None

    def test_lf_headers_do_not_corrupt_crlf_body(self):
        raw = b"HTTP/1.1 200 OK\nContent-Type: text/plain\n\nline1\r\nline2"
        display, _ = prepare_static_response(raw)
        assert "line1\r\nline2" in display

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="响应文件为空"):
            prepare_static_response(b"   ")

    def test_missing_separator_raises(self):
        with pytest.raises(ValueError, match="缺少头部/正文分隔"):
            prepare_static_response(b"HTTP/1.1 200 OK\r\nContent-Length: 0")


class TestTruncate:
    def test_under_limit_keeps_raw(self):
        raw = "a\nb\nc"
        assert truncate_raw(raw, 5) == (raw, 0)

    def test_over_limit(self):
        display, omitted = truncate_raw("l1\r\nl2\r\nl3\r\nl4", 2)
        assert omitted == 2
        assert display.startswith("l1\nl2\n")
        assert "已省略 2 行" in display

    def test_zero_means_unlimited(self):
        raw = "a\nb"
        assert truncate_raw(raw, 0) == (raw, 0)


class TestStatusAndScheme:
    def test_status_line(self):
        assert parse_status_line("HTTP/1.1 200 OK\r\nX: y") == ("200 OK", "HTTP/1.1 200 OK")
        assert parse_status_line("garbage")[0] == "-"

    def test_scheme_resolution(self):
        assert _resolve_schemes(parse_request("GET / HTTP/1.1\r\nHost: a:443\r\n\r\n"),
                                None) == ["https"]
        assert _resolve_schemes(parse_request("GET / HTTP/1.1\r\nHost: a:8080\r\n\r\n"),
                                None) == ["http"]
        assert _resolve_schemes(parse_request("GET / HTTP/1.1\r\nHost: a\r\n\r\n"),
                                None) == ["https", "http"]
        assert _resolve_schemes(parse_request("GET / HTTP/1.1\r\nHost: a\r\n\r\n"),
                                "http") == ["http"]


class TestProxyParts:
    def test_plain(self):
        assert _proxy_parts("127.0.0.1:8080") == ("127.0.0.1", 8080, None)

    def test_invalid(self):
        with pytest.raises(ValueError, match="代理地址无效"):
            _proxy_parts("http://")

    def test_with_auth(self):
        host, port, auth = _proxy_parts("http://u:p@127.0.0.1:3128")
        assert (host, port) == ("127.0.0.1", 3128)
        assert auth == "Basic dTpw"


class TestPayload:
    def test_truncation_note_and_wrap(self):
        req = parse_request("GET / HTTP/1.1\r\nHost: a\r\n\r\n")
        big = "x\n" * 800
        from packetshot.cli import SendResult
        resp = SendResult("HTTP/1.1 200 OK\n\n" + big, 10, 5.0)
        data = build_payload(req, "http", resp, None, 10, [], None, 100, True)
        assert data["wrap"] is True
        assert data["request"]["omitted"] == 0
        assert "已截断" in data["response"]["note"]
        assert data["response"]["elapsedMs"] == "5"

    def test_hexdump_format(self):
        out = hexdump(b"AB")
        assert out.startswith("00000000  41 42")
        assert "|AB" in out
