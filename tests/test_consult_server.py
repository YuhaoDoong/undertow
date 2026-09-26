"""Codex 008 G09：咨询 API 只能绑本机回环；Host 头校验；请求大小上限。"""
import http.client
import threading

import pytest

from undertow.consult.server import NonLoopbackBind, check_host, serve


@pytest.mark.parametrize("h", ["0.0.0.0", "192.168.1.5", "::", "example.com", ""])
def test_non_loopback_refused(h):
    with pytest.raises(NonLoopbackBind):
        serve(lambda q, p: {}, lambda: {}, host=h, port=0)


def test_loopback_names_accepted():
    for h in ("127.0.0.1", "localhost", "[::1]", "::1"):
        assert check_host(h) in ("127.0.0.1", "localhost", "::1")


@pytest.fixture()
def server():
    httpd = serve(lambda q, p: {"q": q, "prompt": "x"}, lambda: {"headline": "h", "groups": []},
                  host="127.0.0.1", port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
    yield httpd.server_address[1]
    httpd.shutdown(); httpd.server_close()


def _get(port, path, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("GET", path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{port}")
    c.endheaders()
    r = c.getresponse(); body = r.read(); c.close()
    return r.status, body


def test_host_header_checked(server):
    assert _get(server, "/health")[0] == 200
    assert _get(server, "/health", host=f"localhost:{server}")[0] == 200
    assert _get(server, "/positions", host="evil.example:80")[0] == 403, "DNS 重绑定：外部 Host 不得拿到持仓"


def test_request_limits(server):
    assert _get(server, "/consult?q=" + "a" * 2500)[0] == 413
    assert _get(server, "/consult?q=" + "a" * 5000)[0] == 414


def test_serve_cli_refuses_non_loopback(capsys):
    import argparse
    from undertow import cli
    rc = cli.cmd_serve(argparse.Namespace(host="0.0.0.0", port=0, no_cache=True))
    assert rc == 2 and "拒绝启动" in capsys.readouterr().err
