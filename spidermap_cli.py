#!/usr/bin/env python3
"""The `spidermap` command (installed as /usr/local/bin/spidermap by install.sh).

    spidermap status | logs | scan | doctor | test-snmp <switch> | check-ports | cert | update | uninstall | menu

Everything here only reads, except: scan (asks the service for a scan), fix-permissions, update and uninstall.
Commands that talk to switches drop to the service user first, like spidermap.py does, so root never leaves
files behind that the service cannot write. See docs/COMMANDS.md.
"""
import argparse
import datetime
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_sm = None


def sm():
    """spidermap.py, imported on first use (help and version work even when it cannot load)."""
    global _sm
    if _sm is None:
        import spidermap as m
        _sm = m
    return _sm


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(code, text):
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def ok(text):
    print(f"  {c('32', '✓')} {text}")


def warn(text, fix=None):
    print(f"  {c('33', '!')} {text}")
    if fix:
        print(f"      {c('2', '→ ' + fix)}")


def bad(text, fix=None):
    print(f"  {c('31', '✗')} {text}")
    if fix:
        print(f"      {c('2', '→ ' + fix)}")


def head(text):
    print("\n" + c("1;34", text))


def when(t):
    if not t:
        return "never"
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def ago(seconds):
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def need_root():
    if os.geteuid() != 0:
        raise SystemExit("Run as root:  sudo spidermap " + " ".join(sys.argv[1:]))


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def service_state():
    try:
        r = subprocess.run(["systemctl", "is-active", "spidermap"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def primary_ip():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()
        return next((a for a in out if ":" not in a), out[0] if out else "127.0.0.1")
    except (OSError, subprocess.TimeoutExpired):
        return "127.0.0.1"


def web_path():
    """'/zabbix' from ZABBIX_URL (http://127.0.0.1/zabbix/api_jsonrpc.php)."""
    from urllib.parse import urlparse
    return urlparse(sm().at.ZABBIX_URL).path.rsplit("/", 1)[0]


# --------------------------------------------------------------------------- #
# Certificates (no extra packages: Python's ssl module only)
# --------------------------------------------------------------------------- #
def fetch_cert(host, port=443, timeout=5):
    """The certificate a TLS server presents, as DER bytes, without checking it. None if there is no TLS there."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=None if _is_ip(host) else host) as t:
                return t.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None


def cert_trusted(host, port=443, name=None, timeout=5):
    """(True, '') if this system trusts the certificate for that name, else (False, reason)."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=name or host):
                return True, ""
    except ssl.SSLCertVerificationError as e:
        return False, e.verify_message or str(e)
    except (OSError, ssl.SSLError) as e:
        return False, str(e)


def _is_ip(text):
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in text else socket.AF_INET, text)
        return True
    except OSError:
        return False


def fingerprint(der):
    import hashlib
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, 64, 2))


def short_fp(fp):
    h = fp.replace(":", "")
    return f"{h[:8]}…{h[-4:]}"


def decode_cert(der):
    """Subject, issuer, names (SAN), validity of a DER certificate: {} if it cannot be read."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        try:
            d = ssl._ssl._test_decode_cert(path)       # noqa: SLF001 - CPython's own parser; stable for years
        except Exception:  # noqa: BLE001
            return {}
    finally:
        os.remove(path)

    def flat(rdns):
        return {k: v for rdn in rdns or () for k, v in rdn}
    subj, iss = flat(d.get("subject")), flat(d.get("issuer"))
    names = [v for k, v in d.get("subjectAltName", ()) if k in ("DNS", "IP Address")]
    nb = ssl.cert_time_to_seconds(d["notBefore"]) if d.get("notBefore") else None
    na = ssl.cert_time_to_seconds(d["notAfter"]) if d.get("notAfter") else None
    return {"subject": subj.get("commonName", ""), "issuer": iss.get("commonName", "") or iss.get("organizationName", ""),
            "self_signed": subj == iss, "names": names, "not_before": nb, "not_after": na}


def name_matches(names, host):
    host = host.lower()
    for n in names:
        n = n.lower().strip()
        if n == host or (n.startswith("*.") and "." in host and host.split(".", 1)[1] == n[2:]):
            return True
    return False


def cert_summary(host, port=443):
    """What a TLS server presents, in one dict, or None if no TLS answers there."""
    der = fetch_cert(host, port)
    if not der:
        return None
    info = decode_cert(der)
    info["fingerprint"] = fingerprint(der)
    info["days_left"] = None if not info.get("not_after") else int((info["not_after"] - time.time()) // 86400)
    return info


def print_cert(info, host, port):
    print(f"  Server:        {host}:{port}")
    print(f"  Issued to:     {info.get('subject') or '-'}")
    print(f"  Issued by:     {info.get('issuer') or '-'}{'  (self-signed)' if info.get('self_signed') else ''}")
    print(f"  Valid for:     {', '.join(info.get('names') or []) or '- (no names: browsers will refuse it)'}")
    if info.get("not_after"):
        print(f"  Valid until:   {when(info['not_after'])}  ({info['days_left']} days left)")
    print(f"  SHA-256:       {info['fingerprint']}")
    print(f"  Short:         {short_fp(info['fingerprint'])}   (compare this when you install it on a device)")


def check_cert_info(info, label, expect_name=None):
    """Doctor lines for one certificate."""
    d = info.get("days_left")
    if d is None:
        warn(f"{label}: could not read the validity dates")
    elif d < 0:
        bad(f"{label}: expired {-d} days ago", "renew it, then reload the web server")
    elif d < 30:
        warn(f"{label}: expires in {d} days ({when(info['not_after'])})", "renew it soon")
    else:
        ok(f"{label}: valid until {when(info['not_after'])} ({d} days), SHA-256 {short_fp(info['fingerprint'])}")
    if expect_name and info.get("names") is not None and not name_matches(info["names"], expect_name):
        warn(f"{label}: not issued for {expect_name} (it covers: {', '.join(info['names']) or 'no names'})",
             f"browsers opening https://{expect_name}/ will warn; issue the certificate for that name or IP")


# --------------------------------------------------------------------------- #
# zabbix.env (written by install.sh's menu through `_setenv`: values come in the environment, never as arguments)
# --------------------------------------------------------------------------- #
SAFE_VALUE = re.compile(r'^[^"\\$`\n\r]*$')


def rewrite_env(text, changes):
    """zabbix.env with KEY=value changed (value None: removed), other lines and comments kept."""
    out, seen = [], set()
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in changes:
            k = m.group(1)
            if k in seen:
                continue
            seen.add(k)
            if changes[k] is not None:
                out.append(f'export {k}="{changes[k]}"')
            continue
        out.append(line)
    for k, v in changes.items():
        if k not in seen and v is not None:
            out.append(f'export {k}="{v}"')
    return "\n".join(out).rstrip("\n") + "\n"


def cmd_setenv(args):
    need_root()
    changes = {}
    for k, v in os.environ.items():
        if k.startswith("SMSET_"):
            key = k[6:]
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
                raise SystemExit(f"bad name: {key}")
            if not SAFE_VALUE.match(v):
                raise SystemExit(f"{key}: the value may not contain quotes, backslashes, $ or backticks")
            changes[key] = v
    for key in args.unset or []:
        changes[key] = None
    if not changes:
        return
    path = os.path.join(sm().CONF_DIR, "zabbix.env")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    new = rewrite_env(text, changes)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    gid = 0
    u = sm().service_user()
    if u:
        gid = u.pw_gid
    os.chown(tmp, 0, gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
    had = {m_.group(1) for m_ in re.finditer(r"(?m)^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=", text)}
    for k, v in changes.items():
        if v is not None:
            print(f"  {k}: saved")
        elif k in had:
            print(f"  {k}: removed")


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def cmd_status(args):
    need_root()
    m = sm()
    st = load_json(os.path.join(m.DATA_DIR, m.STATUS), {})
    jobs = load_json(m.JOBS_FILE, {}).get("jobs", {})
    rep = load_json(m.SCAN_REPORT, {})
    svc = service_state()
    print(c("1", f"SpiderMap {m.__version__}"))
    since = f", since {when(st.get('service_since'))}" if st.get("service_since") else ""
    user = f", as {st['user']}" if st.get("user") else ""
    line = f"{svc}{user}{since}"
    print(f"  Service:     {c('32', line) if svc == 'active' else c('31', line)}")
    if st.get("t") and svc == "active" and time.time() - st["t"] > 120:
        print(f"               {c('33', 'its status is ' + ago(time.time() - st['t']) + ' old - the service may be stuck')}")
    sj = jobs.get("scan", {})
    if sj.get("running"):
        pr = load_json(m.SCAN_PROGRESS, {})
        print(f"  Scan:        running ({pr.get('pct', '?')}%, {pr.get('phase', '')})")
    else:
        res = sj.get("last_result", "-")
        secs = f", {sj['last_seconds']:.0f} s" if sj.get("last_seconds") else ""
        print(f"  Last scan:   {when(sj.get('last_end'))}{secs}, {c('32', res) if res == 'ok' else c('33', res)}")
    nxt = (st.get("jobs") or {}).get("scan", {}).get("next")
    if st.get("paused"):
        print("  Next scan:   automatic scans are paused (Settings > Scans)")
    elif nxt:
        print(f"  Next scan:   {when(nxt)}")
    scan = (st.get("health") or {}).get("scan") or {}
    if scan.get("nodes") is not None:
        print(f"  Map:         {scan.get('nodes')} devices, {scan.get('online', '?')} online"
              f"{', partial' if scan.get('partial') else ''}")
    sw = rep.get("switches") or {}
    if sw:
        print("  Switches:")
        for x in sorted(sw.values(), key=lambda x: x.get("name", "")):
            if x.get("ok"):
                print(f"    {c('32', '✓')} {x.get('name')}  ({x.get('ports', '?')} ports)")
            else:
                print(f"    {c('31', '✗')} {x.get('name')}  {x.get('error', '')}")
    api = (st.get("health") or {}).get("api") or {}
    if api:
        print(f"  Zabbix API:  {c('32', 'ok') if api.get('ok') else c('31', api.get('error', 'error'))}")
    disk = (st.get("health") or {}).get("disk") or {}
    if disk.get("free"):
        print(f"  Disk:        {disk['free'] / 1e9:.1f} GB free")
    https = cert_summary("127.0.0.1", 443) is not None
    print(f"  Open:        {'https' if https else 'http'}://{primary_ip()}{web_path()}/spidermap.php")
    print(f"\n  More:  spidermap logs   ·   spidermap doctor")


# --------------------------------------------------------------------------- #
# logs
# --------------------------------------------------------------------------- #
def cmd_logs(args):
    need_root()
    if args.service:
        cmd = ["journalctl", "-u", "spidermap", "--no-pager", "-n", str(args.lines)] + (["-f"] if args.follow else [])
        os.execvp(cmd[0], cmd)
    log = sm().JOB_LOG
    if not os.path.exists(log):
        raise SystemExit(f"No log yet: {log}  (the service writes it at the first scan; try: spidermap logs --service)")
    if args.errors:
        with open(log, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f if re.search(r"(?i)error|fail|timeout|refused|denied|traceback|!", ln)
                     or ln.startswith("--- ")]
        for ln in lines[-args.lines:]:
            print(ln)
        return
    cmd = ["tail", "-n", str(args.lines)] + (["-F"] if args.follow else []) + [log]
    os.execvp(cmd[0], cmd)


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def cmd_scan(args):
    need_root()
    m = sm()
    if service_state() != "active":
        print("The service is not running: scanning here instead (this takes a minute or two).")
        r = subprocess.run([sys.executable, os.path.join(HERE, "spidermap.py")], cwd=HERE)
        raise SystemExit(r.returncode)
    before = load_json(m.JOBS_FILE, {}).get("jobs", {}).get("scan", {})
    if before.get("running"):
        print("A scan is already running.")
    else:
        os.makedirs(m.INBOX, exist_ok=True)
        req = m.SCAN_REQ
        with open(req + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"by": "command line"}, f)
        u = m.web_user()
        if u:
            try:
                os.chown(req + ".tmp", u.pw_uid, -1)
            except OSError:
                pass
        os.chmod(req + ".tmp", 0o660)
        os.replace(req + ".tmp", req)
        print("Scan requested.")
    if args.no_wait:
        return
    t0, started, last = time.time(), before.get("running", False), ""
    try:
        while time.time() - t0 < 960:
            time.sleep(2)
            j = load_json(m.JOBS_FILE, {}).get("jobs", {}).get("scan", {})
            if not started:
                if j.get("running") or (j.get("last_start", 0) > before.get("last_start", 0)):
                    started = True
                elif time.time() - t0 > 30:
                    raise SystemExit("The service did not pick up the request in 30 s - see: spidermap logs --service")
                continue
            if j.get("running"):
                pr = load_json(m.SCAN_PROGRESS, {})
                now_ = f"  {pr.get('pct', 0):>3}%  {pr.get('phase', '')}"
                if now_ != last:
                    print(now_, flush=True)
                    last = now_
                continue
            res = j.get("last_result", "?")
            print(f"Scan finished in {j.get('last_seconds', 0):.0f} s: {res}")
            if res != "ok":
                print("  See: spidermap logs --errors")
            raise SystemExit(0 if res == "ok" else 1)
    except KeyboardInterrupt:
        print("\nStopped watching; the scan goes on in the service.")


# --------------------------------------------------------------------------- #
# SNMP
# --------------------------------------------------------------------------- #
OID = {"sysName": "1.3.6.1.2.1.1.5.0", "sysDescr": "1.3.6.1.2.1.1.1.0", "sysObjectID": "1.3.6.1.2.1.1.2.0",
       "sysUpTime": "1.3.6.1.2.1.1.3.0", "ifNumber": "1.3.6.1.2.1.2.1.0"}
WALKS = [("LLDP neighbours", "1.0.8802.1.1.2.1.4.1.1.9"),            # lldpRemSysName
         ("MAC table (Q-BRIDGE)", "1.3.6.1.2.1.17.7.1.2.2.1.2"),     # dot1qTpFdbPort
         ("MAC table (BRIDGE)", "1.3.6.1.2.1.17.4.3.1.2"),           # dot1dTpFdbPort
         ("Model, serial, firmware (ENTITY-MIB, optional)", "1.3.6.1.2.1.47.1.1.1.1.13")]        # entPhysicalModelName


def snmp_text(auth, target, oids, timeout=2, retries=1):
    """{oid: text} for snmpget, and the error text if the device did not answer."""
    r = subprocess.run(["snmpget", "-t", str(timeout), "-r", str(retries), "-On", "-Oq", "-Ot", *auth, target, *oids],
                       capture_output=True, text=True, timeout=30, env=getattr(auth, "env", None))
    out = {}
    for line in r.stdout.splitlines():
        oid, _, val = line.partition(" ")
        val = val.strip().strip('"')
        if val and not val.startswith("No Such"):
            out[oid.lstrip(".")] = val
    return out, (r.stderr.strip().splitlines() or [""])[-1] if not out else ""


def snmp_fix(err, ip, version):
    e = err.lower()
    me = primary_ip()
    if "timeout" in e or not e:
        what = "the community" if version != "3" else "the SNMPv3 user and passwords"
        return (f"no answer: check that SNMP is enabled on the switch, that it allows {me} (this server), "
                f"{what}, and that UDP 161 is not blocked")
    if "authentication" in e or "unknown user" in e or "usm" in e:
        return "SNMPv3 user, passwords or protocols do not match the switch - compare the host's SNMP interface in Zabbix"
    if "unknown host" in e:
        return f"cannot resolve {ip}: use an IP address in the host's SNMP interface"
    return err


def snmp_hosts(api):
    return [h for h in sm().at.load_hosts(api) if h["_snmp"] and str(h["status"]) == "0"]


def find_host(hosts, key):
    k = key.lower()
    for h in hosts:
        ips = {i.get("ip") for i in h["interfaces"]} | {i.get("dns") for i in h["interfaces"]}
        if k in (h["name"].lower(), h["host"].lower()) or key in ips:
            return h
    return None


def cmd_test_snmp(args):
    need_root()
    m = sm()
    at = m.at
    h = None
    if at.ZABBIX_TOKEN:
        try:
            api = at.Zabbix(at.ZABBIX_URL, at.ZABBIX_TOKEN, at.VERIFY_TLS)
            h = find_host(snmp_hosts(api), args.switch)
        except Exception as e:  # noqa: BLE001
            print(f"(Zabbix not reachable: {e} - testing with --community only)")
    m.drop_privileges()
    if h:
        auth, target = at.snmp_auth(h["_snmp"], h["_macros"], args.community)
        how = "community from --community" if args.community else m.snmp_summary(h)
        print(f"{h['name']}  ({target.split(':')[1]}, {how})")
    else:
        if not _is_ip(args.switch) and not re.fullmatch(r"[A-Za-z0-9.-]+", args.switch):
            raise SystemExit("Give a Zabbix host name or an IP address.")
        if not args.community:
            raise SystemExit(f"{args.switch} is not an SNMP host in Zabbix. To test it anyway (SNMPv2c):\n"
                             f"  spidermap test-snmp {args.switch} --community <community>")
        iface = {"ip": args.switch, "useip": "1", "port": "161", "details": {"version": "2", "community": args.community}}
        auth, target = at.snmp_auth(iface, {}, args.community)
        print(f"{args.switch}  (not in Zabbix, SNMPv2c)")
    vals, err = snmp_text(auth, target, list(OID.values()))
    if not vals:
        bad("does not answer SNMP", snmp_fix(err, target.split(":")[1], getattr(auth, "version", "2")))
        raise SystemExit(1)
    ok(f"answers: {vals.get(OID['sysName'], '?')}")
    descr = vals.get(OID["sysDescr"], "")
    if descr:
        print(f"      {descr[:110]}")
    up = vals.get(OID["sysUpTime"], "")
    up = ago(int(up) / 100) if up.isdigit() else (up or "?")
    print(f"      interfaces: {vals.get(OID['ifNumber'], '?')}, up for: {up}")
    found = {}
    for label, base in WALKS:
        try:
            rows = at.snmpwalk(auth, target, base)
            n = len(rows) if hasattr(rows, "__len__") else sum(1 for _ in rows)
        except Exception:  # noqa: BLE001
            n = 0
        found[label] = n
        (ok if n else warn)(f"{label}: {n if n else 'none'}")
    if not found["LLDP neighbours"]:
        print(f"\n  {c('2', 'No LLDP neighbours: enable LLDP on the switch (and its neighbours). SpiderMap still finds links')}")
        print(f"  {c('2', 'from the other side, or from the MAC tables - see docs/COMMANDS.md > test-snmp')}")
    q, b = found["MAC table (Q-BRIDGE)"], found["MAC table (BRIDGE)"]
    if q and b and q != b:
        print(f"  {c('2', f'The two MAC tables differ (Q-BRIDGE {q}, BRIDGE {b}): SpiderMap reads both and merges them.')}")
    if not (found["MAC table (Q-BRIDGE)"] or found["MAC table (BRIDGE)"]):
        print(f"  {c('2', 'No MAC table over SNMP: devices behind this switch cannot be placed on its ports.')}")


def cmd_check_ports(args):
    need_root()
    cmd = [sys.executable, os.path.join(HERE, "spidermap.py"), "--check-ports"]
    if args.community:
        cmd += ["--community", args.community]
    os.execv(sys.executable, cmd)


# --------------------------------------------------------------------------- #
# cert
# --------------------------------------------------------------------------- #
def cmd_cert(args):
    host, port = args.server or "127.0.0.1", 443
    if ":" in host and not host.startswith("["):
        host, _, p = host.rpartition(":")
        port = int(p)
    info = cert_summary(host, port)
    if not info:
        if args.fingerprint or args.check_trust:
            raise SystemExit(1)
        if args.server:
            raise SystemExit(f"No TLS (https) answer from {host}:{port}.")
        print("This server does not answer https (port 443): SpiderMap and Zabbix are on http only.")
        print("That works, but the Zabbix login travels unencrypted on your network. See docs/COMMANDS.md > cert.")
        return
    if args.fingerprint:
        print(info["fingerprint"])
        return
    if args.check_trust:
        trusted, why = cert_trusted(host, port)
        print("trusted by this server" if trusted else f"not trusted by this server: {why}")
        raise SystemExit(0 if trusted else 1)
    print_cert(info, host, port)
    if not args.server:
        ip = primary_ip()
        print()
        check_cert_info(info, "Certificate", ip)
        trusted, why = cert_trusted("127.0.0.1", 443, ip)
        if info.get("self_signed") or not trusted:
            print(f"\n  {c('2', 'Self-signed or private: each browser/device must trust it once. Copy it from the')}")
            print(f"  {c('2', 'web server, install it as a trusted root, and compare the SHA-256 above.')}")


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
class Tally:
    def __init__(self):
        self.bad = self.warn = 0


def doctor_https(m, api, T):
    head("HTTPS")
    ip, path = primary_ip(), web_path()
    info = cert_summary("127.0.0.1", 443)
    if not info:
        warn("the web server does not answer https: logins travel unencrypted on your network",
             "optional: enable mod_ssl / an https server block, then run: spidermap cert")
        T.warn += 1
        https = False
    else:
        https = True
        check_cert_info(info, "Certificate", ip)
        d = info.get("days_left")
        T.bad += 1 if d is not None and d < 0 else 0
        T.warn += 1 if (d is not None and 0 <= d < 30) or not name_matches(info.get("names") or [], ip) else 0
    if https:
        try:
            r = m.requests.get(f"http://{ip}{path}/", allow_redirects=False, timeout=5)
            loc = r.headers.get("Location", "")
            if r.status_code in (301, 302, 307, 308) and loc.startswith("https://"):
                ok("http:// redirects to https://")
            else:
                warn("http:// still works without redirecting to https://",
                     "optional: redirect http to https in the web server (keep 127.0.0.1 on http for SpiderMap)")
                T.warn += 1
        except Exception:  # noqa: BLE001
            pass
        try:
            r = m.requests.get(f"http://127.0.0.1{path}/spidermap.php", allow_redirects=False, timeout=5)
            if r.status_code in (301, 302, 307, 308):
                warn("http://127.0.0.1 redirects to https: local tools expect plain http there",
                     "exclude 127.0.0.1 from the https redirect, or set ZABBIX_URL to https and trust the certificate")
                T.warn += 1
        except Exception:  # noqa: BLE001
            pass
    if api is None:
        return
    url = None
    try:
        dash = api.call("dashboard.get", {"output": ["dashboardid"], "filter": {"name": m.DASHBOARD_NAME},
                                          "selectPages": "extend"})
        for p in (dash[0]["pages"] if dash else []):
            for w in p.get("widgets", []):
                for f in w.get("fields", []):
                    if f.get("name") == "url" and "spidermap" in str(f.get("value", "")).lower():
                        url = f["value"]
    except Exception:  # noqa: BLE001
        return
    if not url:
        return
    if not re.match(r"(?i)^https?://", url):
        ok(f"dashboard widget opens '{url}': follows Zabbix itself (http or https)")
    elif url.lower().startswith("http://") and https:
        warn(f"dashboard widget opens {url}: over https Zabbix, browsers block this http page",
             "Dashboards > SpiderMap > Edit > widget > URL: spidermap.php  (relative: works on http and https)")
        T.warn += 1
    else:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ""
        if https and info and info.get("names") and not name_matches(info["names"], h):
            warn(f"dashboard widget opens {url}, but the certificate does not cover {h}",
                 "use the relative URL spidermap.php instead")
            T.warn += 1
        else:
            ok(f"dashboard widget: {url}")
            print(f"      {c('2', 'tip: the relative URL spidermap.php keeps working if the address or http/https changes')}")


def doctor(args):
    need_root()
    m = sm()
    at = m.at
    T = Tally()

    def B(text, fix=None):
        T.bad += 1
        bad(text, fix)

    def W(text, fix=None):
        T.warn += 1
        warn(text, fix)

    print(c("1", f"SpiderMap {m.__version__} - checking everything"))

    head("Service")
    svc = service_state()
    if svc == "active":
        st = load_json(os.path.join(m.DATA_DIR, m.STATUS), {})
        age = time.time() - st.get("t", 0)
        if st.get("t") and age > 120:
            B(f"running, but its status is {ago(age)} old: it looks stuck", "systemctl restart spidermap")
        else:
            ok(f"running (as {st.get('user', '?')}, since {when(st.get('service_since'))})")
        if st.get("version") and st["version"] != m.__version__:
            B(f"the service runs {st['version']}, the code is {m.__version__}", "bash install.sh --update")
    else:
        B(f"the service is not running ({svc})", "systemctl start spidermap   (then: spidermap logs --service)")
    try:
        r = subprocess.run(["git", "-C", HERE, "status", "-sb"], capture_output=True, text=True, timeout=10)
        first = (r.stdout.splitlines() or [""])[0]
        if "behind" in first:
            W(f"the code is behind GitHub ({first.split('[', 1)[-1].rstrip(']')})", "spidermap update")
        dirty = [ln for ln in r.stdout.splitlines()[1:] if not ln.endswith(("zabbix.env", "devices.json"))]
        if dirty:
            W(f"{len(dirty)} changed file(s) in {HERE}: an update may overwrite them",
              f"see: git -C {HERE} status")
    except (OSError, subprocess.TimeoutExpired):
        pass

    head("Configuration")
    env = os.path.join(m.CONF_DIR, "zabbix.env")
    if not os.path.isfile(env):
        B(f"no {env}", "bash install.sh  (choose Install)")
    else:
        s = os.stat(env)
        if s.st_mode & 0o007:
            B(f"{env} is readable by everyone (it holds the API token)", "spidermap fix-permissions")
        else:
            ok(f"{env} (private)")
    if not at.ZABBIX_TOKEN:
        B("no ZABBIX_TOKEN", "bash install.sh, then 4) Zabbix connection")
    devs = load_json(m.DEVICES_FILE)
    if devs is None and os.path.exists(m.DEVICES_FILE):
        B(f"{m.DEVICES_FILE} is not valid JSON", "Settings > General > Saved versions > Restore, or fix it by hand")
    elif devs is not None:
        ok(f"{m.DEVICES_FILE}: {len(devs.get('devices', {}))} named devices")

    head("Permissions")
    wu, su = m.web_user(), m.service_user()
    problems = []
    if os.path.isdir(m.INBOX) and wu and os.stat(m.INBOX).st_uid != wu.pw_uid:
        problems.append(f"{m.INBOX} must belong to {wu.pw_name}")
    if su:
        for p in (m.STATE_DIR, m.DATA_DIR):
            if os.path.isdir(p) and os.stat(p).st_uid != su.pw_uid:
                problems.append(f"{p} must belong to {su.pw_name}")
    if os.path.isdir(m.DATA_DIR) and os.stat(m.DATA_DIR).st_mode & 0o007:
        problems.append(f"{m.DATA_DIR} is readable by everyone")
    for p in problems:
        B(p, "spidermap fix-permissions")
    if not problems:
        ok(f"files belong to {su.pw_name if su else 'root'}; the web server ({wu.pw_name if wu else '?'}) has only its inbox")

    head("Web")
    webd = m.webdir()
    path = web_path()
    if not webd:
        B("the Zabbix web folder was not found", "is the Zabbix frontend installed on this server?")
    elif not os.path.isfile(os.path.join(webd, m.GATEWAY)):
        B(f"{m.GATEWAY} is missing in {webd}", "bash install.sh --update")
    else:
        ok(f"{os.path.join(webd, m.GATEWAY)}")
    try:
        r = m.requests.get(f"http://127.0.0.1{path}/spidermap.php", allow_redirects=False, timeout=5)
        if r.status_code in (401, 403):
            ok(f"the page asks for a Zabbix login ({r.status_code})")
        elif r.status_code in (301, 302, 307, 308):
            pass                                                     # reported under HTTPS
        elif r.status_code == 200:
            B("the page opens without a Zabbix login", "bash install.sh --update, then check the web server rules")
        else:
            W(f"the page answers {r.status_code}", "check the web server error log")
    except Exception as e:  # noqa: BLE001
        why = "connection refused" if "refused" in str(e).lower() else str(e)[:100]
        B(f"the web server does not answer on 127.0.0.1 ({why})", "systemctl status apache2   (or nginx)")
    if webd:
        hits = subprocess.run(["grep", "-rlsE", r'spidermap[^"]*\\?\.\(?[a-z|]*(php|html)', "/etc/apache2", "/etc/httpd",
                               "/etc/nginx"], capture_output=True, text=True).stdout.split()
        if hits:
            W(f"a web server rule may block SpiderMap: {', '.join(hits)}", "block only spidermap*.json (see install.sh)")

    head("Zabbix")
    api = None
    if at.ZABBIX_TOKEN:
        try:
            api = at.Zabbix(at.ZABBIX_URL, at.ZABBIX_TOKEN, at.VERIFY_TLS)
            n = api.call("host.get", {"countOutput": True})
            ok(f"API {at.ZABBIX_URL}: token accepted, {n} host{'' if str(n) == '1' else 's'}")
            if at.readonly_mode():
                ok("Zabbix read-only: SpiderMap creates nothing in Zabbix (events stay in its Activity)")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate verify failed" in msg:
                B(f"{at.ZABBIX_URL}: this server does not trust the https certificate",
                  "use http://127.0.0.1/zabbix/api_jsonrpc.php (local, never leaves the server), or add the "
                  "certificate to /usr/local/share/ca-certificates and run update-ca-certificates")
            elif "401" in msg or "Not authorized" in msg or "re-login" in msg.lower() or "session" in msg.lower():
                B("the API token was refused (expired or deleted?)",
                  "Zabbix > Users > API tokens: create one, then bash install.sh > 4) Zabbix connection")
            elif "Connection refused" in msg or "NewConnectionError" in msg or "timed out" in msg.lower():
                B(f"{at.ZABBIX_URL}: no answer (web server down, or wrong address)",
                  "systemctl status apache2   /   bash install.sh > 4) Zabbix connection")
            else:
                B(f"{at.ZABBIX_URL}: {msg[:160]}", "bash install.sh > 4) Zabbix connection")
            api = None
        if at.ZABBIX_URL.lower().startswith("https://") and not at.VERIFY_TLS:
            W("ZABBIX_VERIFY_TLS=0: the API certificate is not checked", "use http://127.0.0.1 locally instead")
    if api is not None and at.readonly_mode():
        try:
            left = api.call("item.get", {"output": ["itemid"], "search": {"key_": "autotopo."}, "startSearch": True,
                                         "templated": False, "countOutput": True})
            if str(left) not in ("0", ""):
                W(f"{left} earlier SpiderMap item(s) are still in Zabbix and no longer updated (their problems stay open)",
                  "remove them once: SPIDERMAP_ZABBIX_READONLY=0 python3 " + os.path.join(HERE, "spidermap.py")
                  + " --uninstall-zabbix   (or bash install.sh > 4) Zabbix connection)")
        except Exception:  # noqa: BLE001
            pass
    if api is not None and not at.readonly_mode():
        try:
            s = api.call("settings.get", {"output": ["iframe_sandboxing_enabled", "iframe_sandboxing_exception"]})
            if str(s.get("iframe_sandboxing_enabled", "1")) == "1":
                have = set(str(s.get("iframe_sandboxing_exception", "")).split())
                need = {"allow-scripts", "allow-same-origin", "allow-popups", "allow-downloads"}
                if need - have:
                    B(f"the dashboard widget is missing iframe exceptions: {' '.join(sorted(need - have))}",
                      "Administration > General > Other > Security > Iframe sandboxing exceptions")
                else:
                    ok("iframe sandboxing exceptions for the dashboard widget")
                if "allow-popups-to-escape-sandbox" not in have:
                    warn("optional: allow-popups-to-escape-sandbox (device web pages opened from the map work fully)",
                         "add it to the same Iframe sandboxing exceptions field")
        except Exception:  # noqa: BLE001
            print(f"  {c('2', '- iframe settings: not readable with this token (fine; check them by hand once)')}")
        try:
            dash = api.call("dashboard.get", {"output": ["name"], "filter": {"name": m.DASHBOARD_NAME}})
            (ok if dash else warn)(f"dashboard \"{m.DASHBOARD_NAME}\"" + ("" if dash else " not found (bash install.sh to create it)"))
        except Exception:  # noqa: BLE001
            pass

    doctor_https(m, api, T)

    if m.PVE_URL:
        head("Proxmox")
        try:
            s = m.pve_session()
            s.headers["Authorization"] = f"PVEAPIToken={m.PVE_TOKEN_ID}={m.PVE_TOKEN_SECRET}"
            r = s.get(m.PVE_URL.rstrip("/") + "/api2/json/version", timeout=8)
            if r.status_code == 200:
                ok(f"{m.PVE_URL}: Proxmox {r.json().get('data', {}).get('version', '?')}")
            elif r.status_code == 401:
                B("Proxmox refused the token", "bash install.sh > 5) Proxmox")
            else:
                B(f"Proxmox answers {r.status_code}")
            if getattr(s, "unverified", False):
                W("the Proxmox certificate is not checked", "bash install.sh > 5) Proxmox pins it for you")
            else:
                ok("Proxmox certificate checked" + (" (pinned)" if m.PVE_FINGERPRINT else ""))
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "fingerprint" in msg.lower():
                B("Proxmox presents a different certificate than the pinned one (renewed?)",
                  "bash install.sh > 5) Proxmox, and confirm the new fingerprint")
            else:
                B(f"Proxmox: {msg[:160]}")

    head("Data")
    st = load_json(os.path.join(m.DATA_DIR, m.STATUS), {})
    health = st.get("health") or {}
    ages = health.get("data_age") or {}
    mage = ages.get("map")
    sj = load_json(m.JOBS_FILE, {}).get("jobs", {}).get("scan", {})
    if mage is None:
        W("no map data yet", "spidermap scan")
    elif sj.get("last_result") not in (None, "ok"):
        B(f"the last scan ({when(sj.get('last_end'))}) ended: {sj.get('last_result')}", "spidermap logs --errors")
    else:
        ok(f"last scan {when(sj.get('last_end'))}, map data {ago(mage)} old")
    disk = health.get("disk") or {}
    if disk.get("free") is not None and disk["free"] < 500e6:
        B(f"only {disk['free'] / 1e6:.0f} MB free on {disk.get('path')}", "free some space")
    sp = health.get("speedtest")
    if sp:
        ok(f"speed test program: {sp}")
    else:
        W("no speed test program", "bash install.sh --update  (offers to install one)")

    head("Switches (SNMP)")
    if api is None:
        warn("skipped: no Zabbix connection")
    else:
        try:
            hosts = snmp_hosts(api)
        except Exception as e:  # noqa: BLE001
            hosts = []
            B(f"cannot list hosts: {e}")
        m.drop_privileges()
        if not hosts:
            W("no enabled hosts with an SNMP interface in Zabbix: the map has no switches",
              "add your first switch to Zabbix with an SNMP interface - see docs/COMMANDS.md > test-snmp")
        import concurrent.futures as cf

        def probe(h):
            try:
                auth, target = at.snmp_auth(h["_snmp"], h["_macros"])
                vals, err = snmp_text(auth, target, [OID["sysName"]])
                return h, target.split(":")[1], vals, err, getattr(auth, "version", "2")
            except Exception as e:  # noqa: BLE001
                return h, "?", {}, str(e), "?"
        with cf.ThreadPoolExecutor(8) as ex:
            for h, ip, vals, err, v in sorted(ex.map(probe, hosts), key=lambda x: x[0]["name"]):
                if vals:
                    ok(f"{h['name']} ({ip}, {m.snmp_summary(h)})")
                else:
                    B(f"{h['name']} ({ip}) does not answer SNMP", snmp_fix(err, ip, v))
        rep = load_json(m.SCAN_REPORT, {})
        for x in (rep.get("switches") or {}).values():
            if not x.get("ok") and x.get("error"):
                W(f"last scan: {x.get('name')}: {x['error'][:140]}", f"spidermap test-snmp \"{x.get('name')}\"")

    print()
    if T.bad:
        print(c("31", f"{T.bad} problem(s)") + (f", {T.warn} warning(s)" if T.warn else "") + " - fixes are under each line.")
    elif T.warn:
        print(c("33", f"No problems, {T.warn} warning(s).") + " SpiderMap works; the warnings are suggestions.")
    else:
        print(c("32", "Everything looks good."))
    raise SystemExit(2 if T.bad else 0)


# --------------------------------------------------------------------------- #
# the rest: thin wrappers around install.sh / spidermap.py
# --------------------------------------------------------------------------- #
def cmd_fix_permissions(args):
    need_root()
    sm().fix_ownership()
    print("Permissions fixed.")
    if service_state() == "active":
        subprocess.run(["systemctl", "restart", "spidermap"])
        print("Service restarted.")


def cmd_update(args):
    import spidermap_update as up
    if not args.check:
        need_root()                                   # --check only reads, so it works without sudo
    try:
        raise SystemExit(up.update(args))
    except up.UpdateError as e:
        raise SystemExit(f"Update: {e}")


def cmd_uninstall(args):
    need_root()
    os.execvp("bash", ["bash", os.path.join(HERE, "install.sh"), "--uninstall", *args.rest])


def cmd_menu(args):
    need_root()
    os.execvp("bash", ["bash", os.path.join(HERE, "install.sh"), "--menu"])


def cmd_version(args):
    try:
        print(f"SpiderMap {sm().__version__}")
    except Exception:  # noqa: BLE001
        m = re.search(r'__version__ = "([^"]+)"', open(os.path.join(HERE, "spidermap.py"), encoding="utf-8").read())
        print(f"SpiderMap {m.group(1) if m else '?'}")


HELP = """SpiderMap commands (run as root)

  spidermap status                  service, last scan, switches, where to open the map
  spidermap logs [-n 100] [-f]      the scan log  (--errors: only problems, --service: the service's journal)
  spidermap scan [--no-wait]        scan now and show the progress
  spidermap doctor                  check everything; every problem comes with its fix
  spidermap test-snmp <switch|ip>   does a switch answer SNMP, and does it give LLDP and MAC tables?
  spidermap check-ports             how SpiderMap sees every switch port
  spidermap cert [host[:port]]      the https certificate: names, expiry, SHA-256 fingerprint
  spidermap fix-permissions         put file owners and modes right, restart the service
  spidermap update [--check] [--yes] [--main]
                                    a newer version on GitHub? asks, then downloads, backs up, installs
                                    (rolls back by itself if it fails; --reinstall: the files already here)
  spidermap uninstall [--keep-data] [--keep-zabbix]
  spidermap menu                    the installer menu
  spidermap version

Details and examples: docs/COMMANDS.md"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spidermap", add_help=False)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("logs")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--errors", action="store_true")
    p.add_argument("--service", action="store_true")
    p.set_defaults(fn=cmd_logs)
    p = sub.add_parser("scan")
    p.add_argument("--no-wait", action="store_true")
    p.set_defaults(fn=cmd_scan)
    sub.add_parser("doctor").set_defaults(fn=doctor)
    p = sub.add_parser("test-snmp")
    p.add_argument("switch")
    p.add_argument("--community")
    p.set_defaults(fn=cmd_test_snmp)
    p = sub.add_parser("check-ports")
    p.add_argument("--community")
    p.set_defaults(fn=cmd_check_ports)
    p = sub.add_parser("cert")
    p.add_argument("server", nargs="?")
    p.add_argument("--fingerprint", action="store_true", help="print only the SHA-256 fingerprint")
    p.add_argument("--check-trust", action="store_true", help="exit 0 if this server trusts the certificate")
    p.set_defaults(fn=cmd_cert)
    sub.add_parser("fix-permissions").set_defaults(fn=cmd_fix_permissions)
    p = sub.add_parser("update")
    import spidermap_update as up
    up.add_arguments(p)
    p.set_defaults(fn=cmd_update)
    p = sub.add_parser("uninstall")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_uninstall)
    sub.add_parser("menu").set_defaults(fn=cmd_menu)
    sub.add_parser("version").set_defaults(fn=cmd_version)
    p = sub.add_parser("_setenv")                       # used by install.sh: values in SMSET_<KEY> variables
    p.add_argument("--unset", action="append")
    p.set_defaults(fn=cmd_setenv)
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(HELP)
        return
    if argv[0] in ("-V", "--version"):
        argv = ["version"]
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        print(HELP)
        return
    args.fn(args)


if __name__ == "__main__":
    main()
