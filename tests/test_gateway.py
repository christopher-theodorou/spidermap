#!/usr/bin/env python3
"""
Security tests for spidermap.php, the only SpiderMap entry point in the Zabbix web folder.

Builds the gateway exactly as the installer does, runs it with PHP's built-in web server next to a fake
Zabbix API, and checks who may see the page and the data and who may make changes.

    python3 tests/test_gateway.py          (needs php-cli; nothing is installed or changed on the system)

Run it before every release. Every line must say OK.
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ZABBIX_TOKEN", "test")
import spidermap as sm  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    if not shutil.which("php"):
        sys.exit("php-cli is needed for these tests (apt install php-cli)")
    tmp = tempfile.mkdtemp(prefix="spidermap-test-")
    web, data, inbox = (os.path.join(tmp, x) for x in ("web", "data", "inbox"))
    for d in (web, data, os.path.join(inbox, "sess")):
        os.makedirs(d)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    code = (sm.GATEWAY_PHP.replace("__VIEW_MIN_TYPE__", "3").replace("__API_URL__", base + "/api_jsonrpc.php")
            .replace("'/var/lib/spidermap/www'", repr(data)).replace("'/var/lib/spidermap/inbox'", repr(inbox)))
    open(os.path.join(web, "spidermap.php"), "w").write(code)
    shutil.copy(os.path.join(ROOT, "tests", "fake_zabbix_api.php"), os.path.join(web, "api_jsonrpc.php"))
    shutil.copy(os.path.join(ROOT, "spidermap.html"), os.path.join(data, "spidermap.html"))
    json.dump({"nodes": [{"id": "inet"}]}, open(os.path.join(data, "spidermap.json"), "w"))
    open(os.path.join(inbox, "edits.json"), "w").write("{}")
    env = dict(os.environ, PHP_CLI_SERVER_WORKERS="4", SM_TEST_DIR=tmp)
    srv = subprocess.Popen(["php", "-S", f"127.0.0.1:{port}", "-t", web], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        ok, total = run(base), 0
        total = ok[1]
        print(f"\n{ok[0]}/{total} passed")
        return 0 if ok[0] == total else 1
    finally:
        srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


def run(base):
    G = base + "/spidermap.php"

    def cookie(sid, old=False):
        return ("zbx_sessionid=" + sid) if old else \
            "zbx_session=" + base64.b64encode(json.dumps({"sessionid": sid}).encode()).decode()

    def req(url, sid=None, body=None, headers=None, method=None, old=False):
        h = dict(headers or {})
        if sid:
            h["Cookie"] = cookie(sid, old)
        r = urllib.request.Request(url, data=body.encode() if body else None, headers=h,
                                   method=method or ("POST" if body else "GET"))
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)

    SA, AD, US, GU, XX = "a" * 32, "d" * 32, "b" * 32, "c" * 32, "f" * 32
    H = {"X-SpiderMap": "1"}
    edit = '{"mac":"aa:bb:cc:dd:ee:ff","name":"Camera","url":"http://{ip}:8080"}'
    cases = [
        ("no login: the page", req(G), 401),
        ("no login: the data", req(G + "?data=map"), 401),
        ("forged session", req(G + "?data=map", XX), 401),
        ("normal user: the page", req(G, US), 403),
        ("Admin (not Super admin): the page", req(G, AD), 403),
        ("guest account", req(G, GU), 403),
        ("Super admin: the page", req(G, SA), 200),
        ("Super admin: the data", req(G + "?data=map", SA), 200),
        ("Super admin, older Zabbix cookie", req(G + "?data=map", SA, old=True), 200),
        ("path trick ../../etc/passwd", req(G + "?data=../../etc/passwd", SA), 400),
        ("page file is not in the web folder", req(base + "/spidermap.html"), 404),
        ("edit without the header (CSRF)", req(G, SA, edit, {"Content-Type": "text/plain"}), 403),
        ("edit from another site", req(G, SA, edit, dict(H, Origin="https://evil.example")), 403),
        ("javascript: web address", req(G, SA, '{"mac":"aa:bb:cc:dd:ee:ff","url":"javascript:alert(1)"}', H), 400),
        ("PUT request", req(G, SA, edit, H, method="PUT"), 405),
        ("correct edit", req(G, SA, edit, dict(H, Origin=base)), 200),
        ("speed test request", req(G, SA, '{"action":"speedtest"}', H), 200),
    ]
    passed = 0
    for name, (code, _), want in cases:
        good = code == want
        passed += good
        print(f"{'OK ' if good else 'BAD'} {name:38} {code} (expected {want})")
    _, h = req(G, SA)
    for k, want in (("X-Frame-Options", "SAMEORIGIN"), ("X-Content-Type-Options", "nosniff")):
        good = h.get(k) == want
        passed += good
        print(f"{'OK ' if good else 'BAD'} {'header ' + k:38} {h.get(k)}")
    good = "frame-ancestors 'self'" in (h.get("Content-Security-Policy") or "")
    passed += good
    print(f"{'OK ' if good else 'BAD'} {'Content-Security-Policy':38} {'present' if good else 'missing'}")
    return passed, len(cases) + 3


if __name__ == "__main__":
    sys.exit(main())
