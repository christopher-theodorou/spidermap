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
    def gateway(settings):          # exactly what the installer writes, pointed at the test folders
        return (sm.gateway_code(settings, base + "/api_jsonrpc.php")
                .replace("'/var/lib/spidermap/www'", repr(data)).replace("'/var/lib/spidermap/inbox'", repr(inbox)))
    open(os.path.join(web, "spidermap.php"), "w").write(gateway({}))
    # a second gateway with other permissions: everybody may look, admins edit, Super admins start speed tests
    open(os.path.join(web, "open.php"), "w").write(gateway({"permissions": {"view": 1, "edit": 2, "speedtest": 3}}))
    shutil.copy(os.path.join(ROOT, "tests", "fake_zabbix_api.php"), os.path.join(web, "api_jsonrpc.php"))
    shutil.copy(os.path.join(ROOT, "spidermap.html"), os.path.join(data, "spidermap.html"))
    json.dump({"nodes": [{"id": "inet"}]}, open(os.path.join(data, "spidermap.json"), "w"))
    json.dump({"schema": [], "values": {}}, open(os.path.join(data, "spidermap_settings.json"), "w"))
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
        ok, total = run(base, inbox, data), 0
        total = ok[1]
        print(f"\n{ok[0]}/{total} passed")
        return 0 if ok[0] == total else 1
    finally:
        srv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


def run(base, inbox, data):
    G, O = base + "/spidermap.php", base + "/open.php"

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
        # other permissions (open.php: view 1, edit 2, speed test 3)
        ("permissions: normal user may look", req(O, US), 200),
        ("permissions: normal user may not edit", req(O, US, edit, H), 403),
        ("permissions: Admin may edit", req(O, AD, edit, H), 200),
        ("permissions: Admin may not start a test", req(O, AD, '{"action":"speedtest"}', H), 403),
        ("permissions: guest still refused", req(O, GU), 403),
        ("'me' without login", req(G + "?data=me"), 401),
        # scans and settings (default: Zabbix Super admins)
        ("settings: Admin may not start a scan", req(O, AD, '{"action":"scan"}', H), 403),
        ("settings: Admin may not read settings", req(O + "?data=settings", AD), 403),
        ("settings: Super admin may read them", req(G + "?data=settings", SA), 200),
        ("settings: status for every viewer", req(O + "?data=status", US), 404),
        ("settings: Super admin starts a scan", req(G, SA, '{"action":"scan"}', H), 200),
        ("settings: change, with a request id", req(G, SA, '{"action":"settings","settings":{"scan_interval_min":10},"rid":"abc123def"}', H), 200),
        ("settings: not an object", req(G, SA, '{"action":"settings","settings":"x"}', H), 400),
        ("settings: restore a path", req(G, SA, '{"action":"restore","restore":"../../etc/passwd"}', H), 400),
        ("settings: request too large", req(G, SA, '{"action":"import","import":{"x":"' + "a" * 4300000 + '"}}', H), 413),
        ("approve: a device can be approved", req(G, SA, '{"mac":"aa:bb:cc:dd:ee:09","approved":true}', H), 200),
        ("reset: Admin may not reset the map", req(O, AD, '{"action":"reset","reset":{"keep_devices":true}}', H), 403),
        ("settings: without the header (CSRF)", req(G, SA, '{"action":"scan"}', {"Content-Type": "text/plain"}), 403),
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
    extra = 3

    def getj(url, sid):
        r = urllib.request.Request(url, headers={"Cookie": cookie(sid)})
        with urllib.request.urlopen(r, timeout=10) as resp:
            return json.loads(resp.read())
    me = getj(O + "?data=me", AD)
    good = me.get("name") == "netadmin" and me["can"] == {"edit": True, "speedtest": False, "settings": False} and me["need"]["speedtest"] == 3
    passed += good
    extra += 1
    print(f"{'OK ' if good else 'BAD'} {"'me' tells the page what Admin may do":38} {me.get('can')}")
    try:
        got = json.load(open(os.path.join(inbox, "speedtest.request")))
    except (OSError, ValueError):
        got = {}
    good = got.get("by") == "Admin"
    passed += good
    extra += 1
    print(f"{'OK ' if good else 'BAD'} {'speed test request says who asked':38} {got}")
    import stat
    def chk(name, good, detail=""):
        print(f"{'OK ' if good else 'BAD'} {name:38} {detail if not good else ''}")
        return int(bool(good))
    sreq = os.path.join(inbox, "scan.request")
    got = json.load(open(sreq)) if os.path.exists(sreq) else {}
    passed += chk("scan request: who asked, readable by the service group",
                  got.get("by") == "Admin" and oct(stat.S_IMODE(os.stat(sreq).st_mode)) == "0o660", (got, os.path.exists(sreq)))
    st = json.load(open(os.path.join(inbox, "settings.request")))
    passed += chk("settings request: values, user and request id",
                  st.get("settings") == {"scan_interval_min": 10} and st.get("by") == "Admin" and st.get("rid") == "abc123def", st)
    # a permission change from the Settings panel applies at once (access.json), without reinstalling spidermap.php
    json.dump({"view": 1, "edit": 2, "speedtest": 2, "settings": 3}, open(os.path.join(data, "access.json"), "w"))
    code, _ = req(G, US)
    passed += chk("access.json: normal user may now look", code == 200, code)
    json.dump({"view": 0, "edit": 1, "speedtest": 0, "settings": 1}, open(os.path.join(data, "access.json"), "w"))
    code_edit, _ = req(G, US, edit, H)
    code_set, _ = req(G, AD, '{"action":"scan"}', H)
    passed += chk("access.json: limits enforced (edit, settings >= admin)", code_edit == 403 and code_set == 200, (code_edit, code_set))
    os.remove(os.path.join(data, "access.json"))
    code, _ = req(G, US)
    passed += chk("access.json removed: defaults again", code == 403, code)
    code, _ = req(G, SA, '{"action":"reset","reset":{"keep_devices":false}}', H)
    rs = json.load(open(os.path.join(inbox, "settings.request")))
    passed += chk("reset: Super admin, written for the service", code == 200 and rs.get("reset") == {"keep_devices": False, "keep_speedtests": True}
                  and rs.get("by") == "Admin", (code, rs))
    c1, _ = req(O, US, '{"action":"place","place":{"key":"SW|Port 1","kind":"switch"}}', H)
    c2, _ = req(G, SA, '{"action":"place","place":"x"}', H)
    passed += chk("map edit: normal user refused, not an object refused", c1 == 403 and c2 == 400, (c1, c2))
    c3, _ = req(O, AD, '{"action":"place","place":{"key":"SW|Port 1","kind":"switch","evil":1}}', H)
    pr = json.load(open(os.path.join(inbox, "settings.request")))
    passed += chk("map edit: Admin may (edit permission), only known fields reach the service",
                  c3 == 200 and pr.get("place") == {"key": "SW|Port 1", "kind": "switch"} and pr.get("by") == "netadmin", (c3, pr))
    c4, _ = req(O, US, '{"action":"link","link":{"a":["SW","Port 1"],"b":["SW2","Port 2"],"state":"confirmed"}}', H)
    c5, _ = req(O, AD, '{"action":"link","link":{"a":["SW","Port 1"],"b":["SW2","Port 2"],"state":"confirmed","x":1}}', H)
    lr = json.load(open(os.path.join(inbox, "settings.request")))
    passed += chk("link: normal user refused, Admin allowed, only known fields passed", c4 == 403 and c5 == 200 and "x" not in lr.get("link", {}), (c4, c5, lr))
    extra += 9
    return passed, len(cases) + extra


if __name__ == "__main__":
    sys.exit(main())
