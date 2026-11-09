#!/usr/bin/env python3
"""
Tests for the `spidermap` command (spidermap_cli.py) and the installer's menu helpers.

    python3 tests/test_cli.py          (nothing is installed or changed on the system; openssl is used if present)

Run it before every release. It must end with "N/N passed".
"""
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ZABBIX_TOKEN", "test")
import spidermap_cli as cli  # noqa: E402

RESULTS = []


def chk(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("OK    " if cond else "FAIL  ") + name + ("" if cond else f"   -> {detail!r}"))


def out_of(fn, *a):
    buf = io.StringIO()
    code = None
    with redirect_stdout(buf):
        try:
            fn(*a)
        except SystemExit as e:
            code = e.code
    return buf.getvalue(), code


def test_env():
    text = '# comment\nexport ZABBIX_URL="http://a"\nexport ZABBIX_TOKEN="old"\n# export PVE_URL="x"\nPVE_FINGERPRINT="AA"\n'
    new = cli.rewrite_env(text, {"ZABBIX_TOKEN": "new", "PVE_FINGERPRINT": None, "PVE_URL": "https://p:8006"})
    chk("env: value changed in place", 'export ZABBIX_TOKEN="new"' in new and "old" not in new, new)
    chk("env: comments and other lines kept", "# comment" in new and '# export PVE_URL="x"' in new
        and 'export ZABBIX_URL="http://a"' in new, new)
    chk("env: removed key gone, new key added at the end", "PVE_FINGERPRINT" not in new
        and new.rstrip().endswith('export PVE_URL="https://p:8006"'), new)
    chk("env: unsafe values refused", not cli.SAFE_VALUE.match('a"b') and not cli.SAFE_VALUE.match("a$(id)")
        and not cli.SAFE_VALUE.match("a`id`") and not cli.SAFE_VALUE.match("a\nb") and cli.SAFE_VALUE.match("abc-123_!@"))

    if os.geteuid() == 0:
        d = tempfile.mkdtemp()
        path = os.path.join(d, "zabbix.env")
        with open(path, "w") as f:
            f.write(text)
        m = cli.sm()
        old = m.CONF_DIR
        m.CONF_DIR = d
        os.environ["SMSET_ZABBIX_TOKEN"] = "fresh"
        try:
            out, _ = out_of(cli.cmd_setenv, type("A", (), {"unset": ["PVE_FINGERPRINT", "NOT_THERE"]})())
        finally:
            del os.environ["SMSET_ZABBIX_TOKEN"]
            m.CONF_DIR = old
        body = open(path).read()
        mode = os.stat(path).st_mode & 0o777
        chk("_setenv: written, private (640), only existing keys reported as removed",
            'export ZABBIX_TOKEN="fresh"' in body and mode == 0o640 and "NOT_THERE" not in out
            and "PVE_FINGERPRINT: removed" in out, (body, oct(mode), out))
        os.environ["SMSET_ZABBIX_TOKEN"] = 'x"; rm -rf /'
        m.CONF_DIR = d
        try:
            _, code = out_of(cli.cmd_setenv, type("A", (), {"unset": None})())
        finally:
            del os.environ["SMSET_ZABBIX_TOKEN"]
            m.CONF_DIR = old
        chk("_setenv: a value with quotes is refused and nothing is written", code and "rm -rf" not in open(path).read())
        shutil.rmtree(d)


def test_names():
    chk("cert names: exact IP, wildcard DNS", cli.name_matches(["10.20.0.46"], "10.20.0.46")
        and cli.name_matches(["*.lan"], "zabbix.lan") and not cli.name_matches(["*.lan"], "a.b.lan")
        and not cli.name_matches(["10.20.0.46"], "10.20.0.47"))
    chk("short fingerprint", cli.short_fp("3A:7C:21:D9:" + "00:" * 26 + "E0:4B") == "3A7C21D9…E04B")
    chk("ago()", cli.ago(30) == "30 s" and cli.ago(600) == "10 min" and cli.ago(7200) == "2 h" and cli.ago(None) == "?")


def tls_server(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)

    def run():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            try:
                ctx.wrap_socket(c, server_side=True).close()
            except Exception:  # noqa: BLE001
                c.close()
    threading.Thread(target=run, daemon=True).start()
    return s


def test_cert():
    if not shutil.which("openssl"):
        print("(skipped certificate tests: no openssl)")
        return
    d = tempfile.mkdtemp()
    cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                    "-days", "20", "-subj", "/CN=zabbix", "-addext", "subjectAltName=IP:192.0.2.46,DNS:zabbix.lan"],
                   capture_output=True, check=True)
    ref = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-fingerprint", "-sha256"],
                         capture_output=True, text=True).stdout.strip().split("=", 1)[1]
    s = tls_server(cert, key)
    port = s.getsockname()[1]
    info = cli.cert_summary("127.0.0.1", port)
    chk("cert: fingerprint equals openssl's", info and info["fingerprint"] == ref, (info, ref))
    chk("cert: names, issuer, self-signed, days left", info and info["names"] == ["192.0.2.46", "zabbix.lan"]
        and info["self_signed"] and info["subject"] == "zabbix" and 18 <= info["days_left"] <= 20, info)
    trusted, why = cli.cert_trusted("127.0.0.1", port)
    chk("cert: a self-signed certificate is not trusted", not trusted and why, why)
    out, _ = out_of(cli.check_cert_info, info, "Certificate", "192.0.2.99")
    chk("doctor: warns on expiry within 30 days and on a missing name", "expires in" in out and "not issued for 192.0.2.99" in out, out)
    chk("cert: nothing on a closed port", cli.cert_summary("127.0.0.1", 1) is None)
    s.close()
    shutil.rmtree(d)


class FakeApi:
    def __init__(self, url):
        self.url = url

    def call(self, method, params):
        if method == "dashboard.get":
            return [{"pages": [{"widgets": [{"fields": [{"name": "url", "value": self.url}]}]}]}]
        return []


def test_widget():
    m = cli.sm()
    real = cli.cert_summary, cli.primary_ip
    cli.primary_ip = lambda: "192.0.2.46"
    try:
        cli.cert_summary = lambda h, p=443: None                                # plain http server
        for url, expect in (("spidermap.php", "follows Zabbix"), ("http://192.0.2.46/zabbix/spidermap.php", "tip")):
            T = cli.Tally()
            out, _ = out_of(cli.doctor_https, m, FakeApi(url), T)
            chk(f"widget on http Zabbix: {url}", expect in out, out)
        cli.cert_summary = lambda h, p=443: {"names": ["192.0.2.46"], "days_left": 300, "not_after": 2e9,
                                              "fingerprint": "AA:" * 31 + "AA"}
        T = cli.Tally()
        m_req = m.requests.get
        m.requests.get = lambda *a, **k: (_ for _ in ()).throw(OSError("no web server here"))
        try:
            out, _ = out_of(cli.doctor_https, m, FakeApi("http://192.0.2.46/zabbix/spidermap.php"), T)
            chk("widget: http:// URL on https Zabbix is flagged, with the relative fix", "browsers block" in out
                and "URL: spidermap.php" in out and T.warn >= 1, out)
            T = cli.Tally()
            out, _ = out_of(cli.doctor_https, m, FakeApi("https://zabbix.example/zabbix/spidermap.php"), T)
            chk("widget: https URL with a name the certificate does not cover is flagged", "does not cover zabbix.example" in out, out)
        finally:
            m.requests.get = m_req
    finally:
        cli.cert_summary, cli.primary_ip = real


def test_snmp_hints():
    chk("snmp hint: timeout v2c mentions the community and UDP 161",
        "community" in cli.snmp_fix("Timeout: No Response from udp:10.0.0.1:161", "10.0.0.1", "2")
        and "UDP 161" in cli.snmp_fix("Timeout", "10.0.0.1", "2"))
    chk("snmp hint: v3 authentication", "SNMPv3" in cli.snmp_fix("Authentication failure (incorrect password)", "x", "3"))
    hosts = [{"name": "SW-Core", "host": "sw1", "interfaces": [{"ip": "10.0.0.2", "dns": ""}]}]
    chk("find host by name, technical name and IP", cli.find_host(hosts, "sw-core") and cli.find_host(hosts, "sw1")
        and cli.find_host(hosts, "10.0.0.2") and not cli.find_host(hosts, "10.0.0.3"))


def test_help():
    out, _ = out_of(cli.main, [])
    chk("help lists every command", all(w in out for w in ("status", "logs", "scan", "doctor", "test-snmp",
                                                            "check-ports", "cert", "update", "uninstall", "menu")), out)
    out, _ = out_of(cli.main, ["--version"])
    chk("version", out.startswith("SpiderMap " + cli.sm().__version__), out)


def test_installer():
    sh = os.path.join(ROOT, "install.sh")
    r = subprocess.run(["bash", "-n", sh], capture_output=True, text=True)
    chk("install.sh: syntax", r.returncode == 0, r.stderr)
    lib = open(sh).read().split('\ncase "${1:-}" in', 1)[0]
    lib = lib.replace('[ "$(id -u)" = 0 ] || fail "Please run as root."', "")
    script = lib + r'''
normalize_api_url 192.0.2.46/zabbix; echo
normalize_api_url https://z/zabbix/api_jsonrpc.php; echo
mask abcdef1234567890; echo
'''
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=ROOT)
    lines = r.stdout.split("\n")
    chk("menu: API address completed, token masked", lines[:3] == ["http://192.0.2.46/zabbix/api_jsonrpc.php",
                                                                   "https://z/zabbix/api_jsonrpc.php", "abcd…7890"], (r.stdout, r.stderr))
    src = open(sh).read()
    chk("install.sh: no token on a command line (curl -K -)", 'Bearer $ZABBIX_TOKEN"' not in src
        and src.count("curl -s -m 10 -K -") >= 1, "")
    chk("install.sh: non-interactive plain run still installs", 'if [ -t 0 ] && [ -t 1 ]; then menu; else do_install; fi' in src)
    chk("install.sh: uninstall removes only our command", 'grep -qs "$CLI_MARK" "$CLI" && rm -f' in src)


def main():
    for t in (test_env, test_names, test_cert, test_widget, test_snmp_hints, test_help, test_installer):
        t()
    n, total = sum(RESULTS), len(RESULTS)
    print(f"\n{n}/{total} passed")
    return 0 if n == total else 1


if __name__ == "__main__":
    sys.exit(main())
