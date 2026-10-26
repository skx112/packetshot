import os
import subprocess
import sys
from pathlib import Path

from packetshot import __version__

REPO = Path(__file__).resolve().parents[1]
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def run_cli(*args, timeout=120):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [sys.executable, "-m", "packetshot", *args],
        capture_output=True, text=True, timeout=timeout, env=env, cwd=REPO)


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    return str(p)


def assert_png(path):
    data = Path(path).read_bytes()
    assert data.startswith(PNG_SIG)
    assert len(data) > 1000


def basic_req(tmp_path, body=""):
    payload = f"POST /x HTTP/1.1\r\nHost: example.com\r\n\r\n{body}"
    return write(tmp_path, "req.txt", payload)


def basic_resp(tmp_path):
    return write(tmp_path, "resp.txt",
                 "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nok")


class TestCliBasics:
    def test_help(self):
        r = run_cli("--help")
        assert r.returncode == 0
        assert "--req" in r.stdout

    def test_version(self):
        r = run_cli("--version")
        assert r.returncode == 0
        assert __version__ in r.stdout

    def test_render_static(self, tmp_path):
        out = tmp_path / "a.png"
        r = run_cli("--req", basic_req(tmp_path), "--resp", basic_resp(tmp_path),
                    "-o", str(out))
        assert r.returncode == 0, r.stderr
        assert_png(out)
        assert r.stdout.strip() == str(out)

    def test_script_tag_injection(self, tmp_path):
        """回归：数据包含 </script> 不得破坏渲染。"""
        req = basic_req(tmp_path, "p=</script><img src=x onerror=alert(1)>")
        out = tmp_path / "b.png"
        r = run_cli("--req", req, "--resp", basic_resp(tmp_path), "-o", str(out))
        assert r.returncode == 0, r.stderr
        assert_png(out)

    def test_empty_response_file(self, tmp_path):
        resp = write(tmp_path, "empty.txt", "")
        r = run_cli("--req", basic_req(tmp_path), "--resp", resp,
                    "-o", str(tmp_path / "c.png"))
        assert r.returncode == 1
        assert "响应文件为空" in r.stderr

    def test_missing_request_file(self, tmp_path):
        r = run_cli("--req", str(tmp_path / "nope.txt"))
        assert r.returncode == 1
        assert "文件错误" in r.stderr

    def test_garbage_request_line(self, tmp_path):
        req = write(tmp_path, "bad.txt", "garbage\r\n\r\n")
        r = run_cli("--req", req, "--send")
        assert r.returncode == 1
        assert "无法解析请求行" in r.stderr

    def test_both_resp_and_send(self, tmp_path):
        r = run_cli("--req", basic_req(tmp_path), "--resp", basic_resp(tmp_path),
                    "--send")
        assert r.returncode == 2


class TestLive:
    def test_timeout_is_error(self, tmp_path, browser, base_port):
        req = write(tmp_path, "slow.txt",
                    f"GET /slow HTTP/1.1\r\nHost: 127.0.0.1:{base_port}\r\n\r\n")
        r = run_cli("--req", req, "--send", "--scheme", "http",
                    "--timeout", "1", "-o", str(tmp_path / "t.png"))
        assert r.returncode == 1
        assert "未在" in r.stderr

    def test_connection_refused(self, tmp_path, browser):
        req = write(tmp_path, "ref.txt",
                    "GET / HTTP/1.1\r\nHost: 127.0.0.1:1\r\n\r\n")
        r = run_cli("--req", req, "--send", "--scheme", "http",
                    "--timeout", "2", "-o", str(tmp_path / "t.png"))
        assert r.returncode == 1
        assert "网络错误" in r.stderr

    def test_no_host(self, tmp_path):
        req = write(tmp_path, "nohost.txt", "GET / HTTP/1.1\r\n\r\n")
        r = run_cli("--req", req, "--send", "-o", str(tmp_path / "t.png"))
        assert r.returncode == 1
        assert "Host" in r.stderr

    def test_plain_live(self, tmp_path, browser, base_port):
        req = write(tmp_path, "get.txt",
                    f"GET /plain HTTP/1.1\r\nHost: 127.0.0.1:{base_port}\r\n\r\n")
        out = tmp_path / "p.png"
        r = run_cli("--req", req, "--send", "--scheme", "http", "-o", str(out))
        assert r.returncode == 0, r.stderr
        assert_png(out)

    def test_gzip_live(self, tmp_path, browser, base_port):
        req = write(tmp_path, "gz.txt",
                    f"GET /gzip HTTP/1.1\r\nHost: 127.0.0.1:{base_port}\r\n"
                    "Accept-Encoding: gzip\r\n\r\n")
        out = tmp_path / "g.png"
        r = run_cli("--req", req, "--send", "--scheme", "http", "-o", str(out))
        assert r.returncode == 0, r.stderr
        assert "gzip 已解压" in r.stderr
        assert_png(out)

    def test_chunked_live(self, tmp_path, browser, base_port):
        req = write(tmp_path, "ch.txt",
                    f"GET /chunked HTTP/1.1\r\nHost: 127.0.0.1:{base_port}\r\n\r\n")
        out = tmp_path / "ch.png"
        r = run_cli("--req", req, "--send", "--scheme", "http", "-o", str(out))
        assert r.returncode == 0, r.stderr
        assert "chunked 已解码" in r.stderr
        assert_png(out)

    def test_binary_live(self, tmp_path, browser, base_port):
        req = write(tmp_path, "bin.txt",
                    f"GET /binary HTTP/1.1\r\nHost: 127.0.0.1:{base_port}\r\n\r\n")
        out = tmp_path / "bin.png"
        r = run_cli("--req", req, "--send", "--scheme", "http", "-o", str(out))
        assert r.returncode == 0, r.stderr
        assert "hexdump" in r.stderr
        assert_png(out)

    def test_proxy_absolute_form(self, tmp_path, browser, base_port, proxy):
        p = proxy("127.0.0.1", base_port)
        p.start()
        req = write(tmp_path, "px.txt",
                    f"GET /plain HTTP/1.1\r\nHost: 127.0.0.1:{base_port}\r\n\r\n")
        out = tmp_path / "px.png"
        r = run_cli("--req", req, "--send", "--scheme", "http",
                    "--proxy", f"http://127.0.0.1:{p.port}", "-o", str(out))
        assert r.returncode == 0, r.stderr
        assert p.request_line is not None
        assert p.request_line.startswith(f"GET http://127.0.0.1:{base_port}/plain")
        assert_png(out)


class TestValidation:
    def test_invalid_regex(self, tmp_path):
        r = run_cli("--req", basic_req(tmp_path), "--resp", basic_resp(tmp_path),
                    "--hl-regex", "--hl", "[", "-o", str(tmp_path / "r.png"))
        assert r.returncode == 1
        assert "无效的正则表达式" in r.stderr
        assert "Call log" not in r.stderr

    def test_invalid_proxy(self, tmp_path):
        r = run_cli("--req", basic_req(tmp_path), "--send", "--scheme", "http",
                    "--proxy", "http://", "-o", str(tmp_path / "p.png"))
        assert r.returncode == 1
        assert "代理地址无效" in r.stderr

    def test_zero_width_rejected(self, tmp_path):
        r = run_cli("--req", basic_req(tmp_path), "--resp", basic_resp(tmp_path),
                    "--width", "0")
        assert r.returncode == 2

    def test_nonpositive_timeout_rejected(self, tmp_path):
        r = run_cli("--req", basic_req(tmp_path), "--send", "--timeout", "0")
        assert r.returncode == 2

    def test_negative_max_lines_rejected(self, tmp_path):
        r = run_cli("--req", basic_req(tmp_path), "--resp", basic_resp(tmp_path),
                    "--max-lines", "-1")
        assert r.returncode == 2


class TestOptions:
    def test_wrap_and_max_lines(self, tmp_path):
        long_line = "X" * 900
        req = write(tmp_path, "long.txt",
                    f"GET /l HTTP/1.1\r\nHost: example.com\r\nX-Long: {long_line}\r\n\r\n")
        out = tmp_path / "w.png"
        r = run_cli("--req", req, "--resp", basic_resp(tmp_path),
                    "--wrap", "--max-lines", "2", "-o", str(out))
        assert r.returncode == 0, r.stderr
        assert_png(out)

    def test_title_and_width(self, tmp_path):
        out = tmp_path / "t.png"
        r = run_cli("--req", basic_req(tmp_path), "--resp", basic_resp(tmp_path),
                    "--title", "SQL注入验证", "--width", "800", "-o", str(out))
        assert r.returncode == 0, r.stderr
        assert_png(out)
