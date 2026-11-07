#!/usr/bin/env python3
"""
Tests for SpiderMap's core logic. No network, no Zabbix, nothing installed or changed on the system.

    python3 tests/test_core.py

Run it before every release. Every line must say OK.
"""
import datetime
import json
import re
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ZABBIX_TOKEN", "test")
import spidermap as sm  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"{'OK ' if cond else 'BAD'} {name}" + (f"  ({detail})" if detail and not cond else ""))


def test_gateway_step():
    # misses: 1st -> nothing, 2nd -> "down", 3rd -> nothing; first answer after that -> "up"
    seq, fails, out = [False, False, False, True, True, False, True], 0, []
    for answered in seq:
        fails, change = sm.gateway_step(fails, answered)
        out.append(change)
    check("gateway: down after 2 missed pings, up when it answers again",
          out == [None, "down", None, "up", None, None, None], out)


def test_speedtest_due():
    d = datetime.datetime
    check("speed test: not due before the set time", not sm.speedtest_due("03:30", d(2026, 11, 3, 3, 29), ""))
    check("speed test: due at the set time", sm.speedtest_due("03:30", d(2026, 11, 3, 3, 30), ""))
    check("speed test: not repeated the same day (e.g. after a restart)",
          not sm.speedtest_due("03:30", d(2026, 11, 3, 9, 0), "2026-11-03"))
    check("speed test: due again the next day", sm.speedtest_due("03:30", d(2026, 11, 4, 3, 31), "2026-11-03"))
    check("speed test: '3:30' works like '03:30'", sm.speedtest_due("3:30", d(2026, 11, 3, 10, 0), ""))
    check("speed test: empty or invalid time never runs",
          not any(sm.speedtest_due(x, d(2026, 11, 3, 23, 59), "") for x in ("", "abc", "25:00", "12:75")))


def test_apply_edits():
    tmp = tempfile.mkdtemp(prefix="spidermap-core-")
    sm.DEVICES_FILE = os.path.join(tmp, "devices.json")
    sm.EDITS_FILE = os.path.join(tmp, "edits.json")
    json.dump({"settings": {}, "devices": {"aa:bb:cc:dd:ee:01": {"name": "Old", "important": True}}},
              open(sm.DEVICES_FILE, "w"))

    json.dump({"aa:bb:cc:dd:ee:01": {"name": "Camera", "url": "http://{ip}:8080", "important": False, "by": "Admin", "at": 1}},
              open(sm.EDITS_FILE, "w"))
    changed = sm.apply_edits()
    dev = json.load(open(sm.DEVICES_FILE))["devices"]["aa:bb:cc:dd:ee:01"]
    check("edits: merged into devices.json", dev.get("name") == "Camera" and dev.get("url") == "http://{ip}:8080", dev)
    check("edits: 'important' switched off is removed", "important" not in dev, dev)
    check("edits: 'by' and 'at' are not stored", "by" not in dev and "at" not in dev, dev)
    check("edits: edits.json is emptied afterwards", open(sm.EDITS_FILE).read() == "{}")
    check("edits: returns what changed", "aa:bb:cc:dd:ee:01" in changed)

    open(sm.EDITS_FILE, "w").write("not json")
    check("edits: a damaged edits.json does not crash", sm.apply_edits() == {})
    os.remove(sm.EDITS_FILE)
    check("edits: a missing edits.json does not crash", sm.apply_edits() == {})

    # the web page writes an edit while SpiderMap is reading: it must wait, and the edit must not be lost
    open(sm.EDITS_FILE, "w").write("{}")
    writer = subprocess.Popen([sys.executable, "-c", f"""
import fcntl, json, time
f = open({sm.EDITS_FILE!r}, "r+")
fcntl.flock(f, fcntl.LOCK_EX)
time.sleep(1.0)
f.seek(0); f.truncate(); f.write(json.dumps({{"aa:bb:cc:dd:ee:02": {{"name": "Printer"}}}})); f.flush()
"""])
    time.sleep(0.3)                     # the "web page" now holds the lock
    t0 = time.time()
    changed = sm.apply_edits()
    waited = time.time() - t0
    writer.wait()
    devs = json.load(open(sm.DEVICES_FILE))["devices"]
    check("edits: waits while the web page is writing", waited > 0.4, f"waited {waited:.2f}s")
    check("edits: an edit saved at that moment is not lost", devs.get("aa:bb:cc:dd:ee:02", {}).get("name") == "Printer", devs)


def test_job_lock():
    tmp = tempfile.mkdtemp(prefix="spidermap-lock-")
    sm.STATE_DIR = tmp
    first = sm.job_lock("scan", wait=False)
    second = sm.job_lock("scan", wait=False)
    check("jobs: a second scan does not start while one runs", first is not None and second is None)
    other = sm.job_lock("live", wait=False)
    check("jobs: a live refresh can run next to a scan", other is not None)
    first.close()
    other.close()
    again = sm.job_lock("scan", wait=False)
    check("jobs: the next scan starts once the previous one ends", again is not None)
    again.close()


def test_audit():
    tmp = tempfile.mkdtemp(prefix="spidermap-audit-")
    sm.DEVICES_FILE, sm.EDITS_FILE = os.path.join(tmp, "devices.json"), os.path.join(tmp, "edits.json")
    sm.AUDIT_FILE, sm.EVENTS_FILE, sm.LOCK_FILE = os.path.join(tmp, "audit.log"), os.path.join(tmp, "ev.json"), os.path.join(tmp, ".lock")
    json.dump({"settings": {}, "devices": {"aa:bb:cc:dd:ee:01": {"name": "Cam", "type": "camera"}}}, open(sm.DEVICES_FILE, "w"))
    json.dump({"aa:bb:cc:dd:ee:01": {"name": "Driveway cam", "type": "camera", "important": True, "by": "Admin", "at": 1}},
              open(sm.EDITS_FILE, "w"))
    sm.apply_edits()
    lines = [json.loads(x) for x in open(sm.AUDIT_FILE)]
    check("audit: an edit is logged with who and what changed",
          len(lines) == 1 and lines[0]["by"] == "Admin" and lines[0]["changes"] == {"name": ["Cam", "Driveway cam"], "important": [None, True]},
          lines)
    check("audit: the log is readable by root only (600)", oct(os.stat(sm.AUDIT_FILE).st_mode & 0o777) == "0o600")
    evs = json.load(open(sm.EVENTS_FILE))
    check("audit: the change appears in the activity feed",
          any(e["type"] == "audit" and "Admin changed" in e["text"] and "Driveway cam" in e["text"] for e in evs), evs)
    json.dump({"aa:bb:cc:dd:ee:01": {"name": "Driveway cam", "type": "camera", "important": True, "by": "Admin", "at": 2}},
              open(sm.EDITS_FILE, "w"))
    sm.apply_edits()
    check("audit: saving without changes logs nothing", len(open(sm.AUDIT_FILE).readlines()) == 1)


def test_snmp_secrets():
    import spidermap_zabbix as at
    tmp = tempfile.mkdtemp(prefix="spidermap-snmp-")
    at.SNMP_CONF_DIR = os.path.join(tmp, "snmp")
    v3 = {"ip": "10.0.0.2", "useip": "1", "port": "161", "details": {
        "version": "3", "securityname": "mon", "securitylevel": "2", "authprotocol": "1",
        "authpassphrase": 'a "quoted" pass#1', "privprotocol": "1", "privpassphrase": "{$PRIV}"}}
    auth, target = at.snmp_auth(v3, {"{$PRIV}": "priv secret 2"})
    cmd = " ".join(["snmpwalk", *auth, target])
    check("SNMP: no password or community on the command line",
          not any(x in cmd for x in ("quoted", "priv secret", "mon", "-A", "-X", "-c")), cmd)
    conf = open(os.path.join(auth.env["SNMPCONFPATH"], "snmp.conf")).read()
    check("SNMP: credentials are in the private snmp.conf (macros resolved, quotes escaped)",
          'defAuthPassphrase "a \\"quoted\\" pass#1"' in conf and 'defPrivPassphrase "priv secret 2"' in conf
          and "defSecurityName mon" in conf, conf)
    d = auth.env["SNMPCONFPATH"]
    check("SNMP: private folder 700, file 600",
          oct(os.stat(d).st_mode & 0o777) == "0o700" and oct(os.stat(d + "/snmp.conf").st_mode & 0o777) == "0o600")
    check("SNMP: extra options keep the private settings", (auth + ["-Oe"]).env is auth.env)
    v2 = {"ip": "10.0.0.3", "useip": "1", "port": "161", "details": {"version": "2", "community": "{$SNMP_COMMUNITY}"}}
    a2, _ = at.snmp_auth(v2, {"{$SNMP_COMMUNITY}": "s3cret"})
    check("SNMP: v2c community from a Zabbix macro, not on the command line",
          "s3cret" not in " ".join(a2) and "defCommunity s3cret" in open(a2.env["SNMPCONFPATH"] + "/snmp.conf").read())


def test_permissions():
    cases = [({}, {"view": 3, "edit": 3, "speedtest": 3, "settings": 3}),
             ({"view_min_user_type": 1}, {"view": 1, "edit": 2, "speedtest": 2, "settings": 3}),
             ({"permissions": {"view": 1, "edit": 1, "speedtest": 1, "settings": 1}}, {"view": 1, "edit": 2, "speedtest": 1, "settings": 2}),
             ({"permissions": {"view": 2, "speedtest": 1}}, {"view": 2, "edit": 2, "speedtest": 2, "settings": 3}),
             ({"permissions": {"edit": "x"}}, {"view": 3, "edit": 3, "speedtest": 3, "settings": 3})]
    check("permissions: defaults, limits (edits never below admin, nothing below view) and bad values",
          all(sm.permissions(st) == want for st, want in cases), [(st, sm.permissions(st)) for st, _ in cases])


def _sandbox():
    """Point every file SpiderMap writes at a temporary folder."""
    tmp = tempfile.mkdtemp(prefix="spidermap-svc-")
    for d in ("state", "www", "inbox", "lib"):
        os.makedirs(os.path.join(tmp, d))
    sm.STATE_DIR, sm.DATA_DIR, sm.INBOX, sm.EDITS_DIR = (os.path.join(tmp, d) for d in ("state", "www", "inbox", "lib"))
    sm.JOBS_FILE, sm.CONF_HIST_DIR = os.path.join(tmp, "state", "jobs.json"), os.path.join(tmp, "state", "hist")
    sm.JOB_LOG, sm.AUDIT_FILE = os.path.join(tmp, "lib", "job.log"), os.path.join(tmp, "lib", "audit.log")
    sm.SCAN_REQ, sm.CANCEL_REQ, sm.SETTINGS_REQ = (os.path.join(tmp, "inbox", f) for f in ("scan.request", "scan.cancel", "settings.request"))
    sm.DEVICES_FILE, sm.EVENTS_FILE, sm.LOCK_FILE = os.path.join(tmp, "devices.json"), os.path.join(tmp, "ev.json"), os.path.join(tmp, ".lock")
    sm.SCAN_REPORT = os.path.join(tmp, "state", "report.json")
    json.dump({"settings": {"permissions": {"view": 3}}, "devices": {"aa:bb:cc:dd:ee:01": {"name": "Cam"}}}, open(sm.DEVICES_FILE, "w"))
    return tmp


def _wait(sched, name, secs=10):
    end = time.time() + secs
    while name in sched.proc and time.time() < end:
        sched.poll()
        time.sleep(0.05)


def test_scheduler():
    _sandbox()
    sm.Scheduler.command = staticmethod(lambda name: ["sh", "-c", "sleep 0.3"])
    S = sm.effective_settings({})
    sch = sm.Scheduler()
    t = int(time.time())
    sch.tick(t, S)
    check("scheduler: first run starts the scan and the refresh", set(sch.proc) == {"scan", "live"}, list(sch.proc))
    _wait(sch, "scan"); _wait(sch, "live")
    check("scheduler: finished jobs are recorded as ok",
          sch.jobs["scan"].get("last_result") == "ok" and sch.history and sch.history[-1]["result"] == "ok", sch.jobs)
    sch.tick(t + 10, S)
    check("scheduler: nothing starts again before its interval", not sch.proc, list(sch.proc))
    sch.tick(t + 61, S)
    check("scheduler: the refresh starts after its interval, the scan not yet", set(sch.proc) == {"live"}, list(sch.proc))
    _wait(sch, "live")

    sm.Scheduler.command = staticmethod(lambda name: ["sh", "-c", "sleep 30"])
    json.dump({"by": "Admin"}, open(sm.SCAN_REQ, "w"))
    sch.tick(t + 70, S)
    json.dump({"by": "Admin"}, open(sm.SCAN_REQ, "w"))
    sch.tick(t + 71, S)
    check("scheduler: 'Scan now' starts a scan, a second click does not start another",
          "scan" in sch.proc and sch.proc["scan"]["by"] == "Admin" and not os.path.exists(sm.SCAN_REQ))
    json.dump({"by": "Admin"}, open(sm.CANCEL_REQ, "w"))
    sch.tick(t + 72, S)
    _wait(sch, "scan")
    check("scheduler: Cancel stops the scan", sch.jobs["scan"]["last_result"] == "cancelled", sch.jobs["scan"])

    sm.JOB_TIMEOUT["scan"] = 1
    sch.start("scan", "manual")
    time.sleep(1.2)
    _wait(sch, "scan")
    sm.JOB_TIMEOUT["scan"] = 900
    check("scheduler: a scan that takes too long is stopped", sch.jobs["scan"]["last_result"] == "timeout", sch.jobs["scan"])

    sm.Scheduler.command = staticmethod(lambda name: ["sh", "-c", "echo 'SNMP timeout on SW-Core'; exit 3"])
    sch.start("scan", "manual")
    _wait(sch, "scan")
    check("scheduler: a failed scan keeps its error message",
          sch.jobs["scan"]["last_result"] == "failed (exit 3)" and "SNMP timeout" in sch.jobs["scan"].get("error", ""), sch.jobs["scan"])

    S2 = dict(S, auto_scan=False)
    sch.jobs["scan"]["last_start"] = 0
    sm.Scheduler.command = staticmethod(lambda name: ["sh", "-c", "sleep 0.2"])
    sch.tick(t + 5000, S2)
    check("scheduler: paused discovery starts no scans, the refresh goes on", "scan" not in sch.proc and "live" in sch.proc)
    _wait(sch, "live")

    sch.jobs["scan"].update(last_start=t + 5000, running=True)
    sch.save()
    again = sm.Scheduler()
    again.tick(t + 5010, S)
    check("scheduler: after a restart nothing is repeated at once, an interrupted job is marked",
          "scan" not in again.proc and again.jobs["scan"]["last_result"] == "interrupted", again.jobs["scan"])
    for n in list(again.proc):
        again.cancel(n)
    st = again.status(t + 5010, S, {})
    check("scheduler: status says when the next scan runs", st["jobs"]["scan"]["next"] == t + 5000 + 300, st["jobs"]["scan"])


def test_settings_apply():
    _sandbox()
    before = open(sm.DEVICES_FILE).read()
    r = sm.apply_settings_request({"by": "Admin", "settings": {"scan_interval_min": 1}})
    check("settings: an invalid value is refused and nothing is written",
          not r["ok"] and r["errors"] and open(sm.DEVICES_FILE).read() == before, r)
    r = sm.apply_settings_request({"by": "Admin", "settings": {"scan_interval_min": 15, "scan_subnets": "192.168.1.0/24",
                                                                "permissions.view": 2}})
    cfg = json.load(open(sm.DEVICES_FILE))
    check("settings: valid values are saved, other settings and the devices are kept",
          r["ok"] and cfg["settings"]["scan_interval_min"] == 15 and cfg["settings"]["permissions"]["view"] == 2
          and cfg["devices"]["aa:bb:cc:dd:ee:01"]["name"] == "Cam", (r, cfg))
    hist = sm.config_history()
    check("settings: the previous version is kept", len(hist) == 1 and hist[0]["by"] == "Admin", hist)
    lines = [json.loads(x) for x in open(sm.AUDIT_FILE)]
    check("settings: the change is in the audit log", lines[-1]["action"] == "settings" and "scan_interval_min" in lines[-1]["changes"], lines)
    r = sm.apply_settings_request({"by": "Admin", "settings": {"scan_interval_min": 15}})
    check("settings: saving the same values writes nothing", r["ok"] and len(sm.config_history()) == 1, r)
    r = sm.apply_settings_request({"by": "Admin", "import": {"settings": {}, "devices": {"aa:bb:cc:dd:ee:02": {"name": "X", "url": "javascript:alert(1)"}}}})
    check("import: a dangerous web address is refused", not r["ok"] and "http" in " ".join(r["errors"]), r)
    r = sm.apply_settings_request({"by": "Admin", "import": {"settings": {"speedtest_daily": "04:00", "port_media": {"SW": {"9": "SFP+"}}},
                                                           "devices": {"AA:BB:CC:DD:EE:02": {"name": "Printer", "type": "printer", "junk": 1}}}})
    cfg = json.load(open(sm.DEVICES_FILE))
    check("import: devices and settings replaced, unknown fields dropped, MACs in lower case",
          r["ok"] and cfg["devices"] == {"aa:bb:cc:dd:ee:02": {"name": "Printer", "type": "printer"}}
          and cfg["settings"]["speedtest_daily"] == "04:00" and cfg["settings"]["port_media"] == {"SW": {"9": "SFP+"}}, (r, cfg))
    first = sm.config_history()[-1]["id"]
    r = sm.apply_settings_request({"by": "Admin", "restore": first})
    cfg = json.load(open(sm.DEVICES_FILE))
    check("restore: the first saved version is back", r["ok"] and cfg["devices"] == {"aa:bb:cc:dd:ee:01": {"name": "Cam"}}, (r, cfg))
    r = sm.apply_settings_request({"by": "Admin", "restore": "../../etc/passwd"})
    check("restore: only saved versions, no paths", not r["ok"], r)
    for i in range(3):                                     # several saves within one second keep separate versions
        sm.apply_settings_request({"by": "Admin", "settings": {"flag_hours": 10 + i}})
    hist = sm.config_history()
    check("settings: quick saves keep separate versions, newest first",
          len({h["id"] for h in hist}) == len(hist) and hist[0]["saved_at"] >= hist[-1]["saved_at"], hist)
    sm.publish_settings()
    acc = json.load(open(os.path.join(sm.DATA_DIR, sm.ACCESS)))
    out = json.load(open(os.path.join(sm.DATA_DIR, sm.SETTINGS_OUT)))
    check("published: permissions for spidermap.php and the form for the page",
          acc == {"view": 3, "edit": 3, "speedtest": 3, "settings": 3} and len(out["schema"]) == len(sm.SETTINGS_SCHEMA)
          and out["values"]["flag_hours"] == 12 and out["values"]["scan_interval_min"] == 5, (acc, out["values"]))


def test_config_location():
    import spidermap_zabbix as at
    tmp = tempfile.mkdtemp(prefix="spidermap-conf-")
    new = os.path.join(tmp, "etc")
    os.makedirs(new)
    check("config: before the installer moved anything, files stay next to the code",
          at._pick("SPIDERMAP_TEST_UNSET", new, ("zabbix.env", "devices.json")) == at.CODE_DIR)
    open(os.path.join(new, "zabbix.env"), "w").write('export ZABBIX_TOKEN="t"\n')
    check("config: a first install (only zabbix.env there yet) already uses the new folder",
          at._pick("SPIDERMAP_TEST_UNSET", new, ("zabbix.env", "devices.json")) == new)
    env = os.path.join(tmp, "z.env")
    open(env, "w").write('# comment\nexport ZABBIX_TEST_URL="http://x/api_jsonrpc.php"\nexport ZABBIX_TEST_TOK=\'a b"c\'\nBAD LINE\n')
    at._load_env_file(env)
    check("config: zabbix.env is read without sourcing it (quotes, comments, bad lines)",
          os.environ.get("ZABBIX_TEST_URL") == "http://x/api_jsonrpc.php" and os.environ.get("ZABBIX_TEST_TOK") == 'a b"c')


def test_dialog_settings():
    import datetime as dt
    V = sm.validate_settings
    check("dialog: a preview setting cannot be switched on", V({"lock_positions": True})[1] and not V({"zabbix_triggers": True})[1])
    check("dialog: read-only SNMP is always on", V({"snmp_readonly": False})[1] and sm.effective_settings({"snmp_readonly": False})["snmp_readonly"] is True)
    check("dialog: planned options are refused, offered ones accepted",
          V({"layout": "force"})[1] and V({"default_view": "vlan"})[1] and not V({"layout": "tree", "units": "percent"})[1])
    check("dialog: load critical must be above warning", V({"load_warn": 80, "load_crit": 70})[1] and not V({"load_warn": 60, "load_crit": 95})[1])
    check("dialog: map name without HTML, at most 60 characters",
          V({"map_name": "<script>"})[1] and V({"map_name": "x" * 61})[1] and V({"map_name": " Home network "})[0] == {"map_name": "Home network"})
    check("dialog: the scan window needs two different times", V({"offpeak": True, "offpeak_start": "22:00", "offpeak_end": "22:00"})[1]
          and not V({"offpeak": True, "offpeak_start": "22:00", "offpeak_end": "6:00"})[1])
    check("dialog: ranges from the design (payload 0-65500, timeout 100-10000, parallel 1-128, pps 10-10000)",
          not V({"ping_size": 0, "ping_timeout_ms": 100, "ping_parallel": 128, "scan_pps": 10000})[1]
          and V({"ping_size": 65501})[1] and V({"ping_parallel": 129})[1] and V({"scan_pps": 9})[1])
    S = dict(sm.effective_settings({}), offpeak=True, offpeak_start="22:00", offpeak_end="06:00")
    check("dialog: off-peak window across midnight",
          [sm.in_scan_window(S, dt.datetime(2026, 1, 1, h, 0)) for h in (21, 22, 3, 6)] == [False, True, True, False])
    check("dialog: without the window, scans may always run", sm.in_scan_window(sm.effective_settings({}), dt.datetime(2026, 1, 1, 12)))
    check("dialog: the earlier 'paused' setting becomes 'Automatic scan: off'",
          sm.effective_settings({"discovery_paused": True})["auto_scan"] is False and sm.effective_settings({})["auto_scan"] is True)
    h = {"_snmp": {"details": {"version": "3", "securityname": "{$U}", "securitylevel": "2", "authpassphrase": "SECRET1",
                               "privpassphrase": "SECRET2"}}, "_macros": {"{$U}": "monitor"}}
    sumry = sm.snmp_summary(h)
    check("dialog: the SNMP summary shows version and user, never a password or community",
          sumry == "v3 authPriv, user monitor" and "SECRET" not in sumry
          and sm.snmp_summary({"_snmp": {"details": {"version": "2", "community": "public123"}}}) == "v2c, community from Zabbix", sumry)
    tmp = tempfile.mkdtemp(prefix="spidermap-log-")
    sm.JOB_LOG = os.path.join(tmp, "spidermap.log")
    open(sm.JOB_LOG, "w").write("yesterday\n")
    old = time.time() - 86400
    os.utime(sm.JOB_LOG, (old, old))
    ancient = os.path.join(tmp, "spidermap.log.2020-01-01")
    open(ancient, "w").write("x")
    os.utime(ancient, (old - 60 * 86400, old - 60 * 86400))
    sm.rotate_log()
    files = sorted(os.listdir(tmp))
    check("dialog: logs are kept per day, older than 'Keep log files' are removed",
          files == [f"spidermap.log.{dt.date.fromtimestamp(old).isoformat()}"], files)


def _fake_router(loc_host="127.0.0.1", rate_down=12_500_000, rate_up=2_500_000, stuck=False, frozen=False, x64=False):
    """A UPnP router in threads: SSDP on a random UDP port, description and SOAP on a random HTTP port."""
    import http.server, socket, threading
    t0, state = time.time(), {"answer": True}
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    hport = httpd.server_address[1]
    desc = ('<root><device><modelName>Speedport Test</modelName><deviceList><device><serviceList><service>'
            '<serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>'
            '<SCPDURL>/scpd.xml</SCPDURL><controlURL>/ctl</controlURL></service></serviceList></device></deviceList></device></root>')
    names = ["GetCommonLinkProperties", "GetTotalBytesSent", "GetTotalBytesReceived"] + (["X_GetTotalBytesReceived64", "X_GetTotalBytesSent64"] if x64 else [])
    scpd = "<scpd><actionList>" + "".join(f"<action><name>{n}</name></action>" for n in names) + "</actionList></scpd>"

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write((scpd if "scpd" in self.path else desc).encode())

        def do_POST(self):
            if not state["answer"]:
                self.send_response(500); self.end_headers(); return
            act = self.headers.get("SOAPAction", "").split("#")[-1].strip('"')
            el = 0 if frozen else time.time() - t0
            if stuck and act.startswith("GetTotalBytes"):
                body = f"<New{act[3:]}>2147483647</New{act[3:]}>"
                self.send_response(200); self.end_headers(); self.wfile.write(f"<r>{body}</r>".encode()); return
            body = {"X_GetTotalBytesReceived64": f"<NewTotalBytesReceived>{int(9e12 + el * rate_down)}</NewTotalBytesReceived>",
                    "X_GetTotalBytesSent64": f"<NewTotalBytesSent>{int(5e12 + el * rate_up)}</NewTotalBytesSent>",
                    "GetTotalBytesReceived": f"<NewTotalBytesReceived>{int(1e6 + el * rate_down) % 2**32}</NewTotalBytesReceived>",
                    "GetTotalBytesSent": f"<NewTotalBytesSent>{int(1e6 + el * rate_up)}</NewTotalBytesSent>",
                    "GetCommonLinkProperties": "<NewLayer1DownstreamMaxBitRate>300000000</NewLayer1DownstreamMaxBitRate>"}.get(act, "")
            self.send_response(200); self.end_headers(); self.wfile.write(f"<r>{body}</r>".encode())
    httpd.RequestHandlerClass = H
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("127.0.0.1", 0))

    def ssdp():
        while True:
            d, a = u.recvfrom(4096)
            u.sendto(f"HTTP/1.1 200 OK\r\nLOCATION: http://{loc_host}:{hport}/d.xml\r\n\r\n".encode(), a)
    threading.Thread(target=ssdp, daemon=True).start()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return u.getsockname()[1], state


def _upnp_with(sport):
    real = sm.upnp_discover
    sm.upnp_discover = lambda gw, timeout=1.5, port=None, mcast=None: real(gw, timeout, sport, "127.0.0.1")
    return real


def test_upnp_stuck():
    data = {"nodes": [{"kind": "router", "ip": "127.0.0.1"}]}
    sport, _ = _fake_router(stuck=True)
    real = _upnp_with(sport)
    try:
        upnp = {"svc": None, "next": 0, "fails": 0, "prev": None, "link": 0, "error": ""}
        r = sm.read_upnp(upnp, data, {}, "auto", 0)
        check("UPnP: counters stuck at 2,147,483,647 are not taken as 'no traffic'",
              r is None and "2,147,483,647" in upnp["error"] and "switch port" in upnp["error"] and upnp["stuck_until"] == 600, upnp)
        check("UPnP: a stuck router is asked again only after 10 minutes", sm.read_upnp(upnp, data, {}, "auto", 300) is None
              and upnp["stuck_until"] == 600)
    finally:
        sm.upnp_discover = real
    sport, _ = _fake_router(frozen=True)
    real = _upnp_with(sport)
    try:
        upnp = {"svc": None, "next": 0, "fails": 0, "prev": None, "link": 0, "error": ""}
        for i in range(14):
            sm.read_upnp(upnp, data, {}, "auto", i * 5, switch_rate=50e6)
        check("UPnP: counters that do not move while the switch sees traffic are not used",
              "do not change" in upnp["error"] and upnp.get("stuck_until"), upnp)
        quiet = {"svc": None, "next": 0, "fails": 0, "prev": None, "link": 0, "error": ""}
        for i in range(14):
            sm.read_upnp(quiet, data, {}, "auto", i * 5, switch_rate=0)
        check("UPnP: a quiet line (no traffic anywhere) is not mistaken for frozen counters", not quiet.get("stuck_until"), quiet)
    finally:
        sm.upnp_discover = real
    sport, _ = _fake_router(x64=True)
    real = _upnp_with(sport)
    try:
        upnp, usage = {"svc": None, "next": 0, "fails": 0, "prev": None, "link": 0, "error": ""}, {}
        sm.read_upnp(upnp, data, usage, "auto", 0)
        time.sleep(2)
        r = sm.read_upnp(upnp, data, usage, "auto", 10)
        check("UPnP: a router's 64-bit counter is found in its action list and used",
              upnp["svc"]["rx"] == "X_GetTotalBytesReceived64" and r and abs(r["rx"] / 1e6 - 100) < 8, (upnp["svc"], r))
    finally:
        sm.upnp_discover = real


def test_upnp():
    sport, state = _fake_router()
    real = sm.upnp_discover
    sm.upnp_discover = lambda gw, timeout=1.5, port=None, mcast=None: real(gw, timeout, sport, "127.0.0.1")
    try:
        check("UPnP: an answer from another address than the gateway is ignored", sm.upnp_discover("127.0.0.9") is None)
        data = {"nodes": [{"kind": "router", "ip": "127.0.0.1"}]}
        upnp, usage = {"svc": None, "next": 0, "fails": 0, "prev": None, "link": 0, "error": ""}, {}
        first = sm.read_upnp(upnp, data, usage, "auto", 0)
        time.sleep(2)
        r = sm.read_upnp(upnp, data, usage, "auto", 10)
        check("UPnP: the router is found, the first reading is only a baseline", first is None and upnp["svc"]
              and upnp["svc"]["name"] == "Speedport Test" and upnp["link"] == 300, upnp)
        check("UPnP: throughput from the router's counters (100 / 20 Mbps)",
              r and abs(r["rx"] / 1e6 - 100) < 8 and abs(r["tx"] / 1e6 - 20) < 3, r)
        check("UPnP: day and month totals grow", usage["wan"]["d"][0] > 20e6 and usage["wan"]["m"] == usage["wan"]["d"]
              and usage.get("wan_since"), usage)
        state["answer"] = False
        for i in range(3):
            sm.read_upnp(upnp, data, usage, "auto", 20 + i)
        check("UPnP: after 3 failed readings the router is looked for again", upnp["svc"] is None and upnp["next"] == 22 + 60
              and upnp["error"], upnp)
        state["answer"] = True
        data2 = {"nodes": [{"kind": "router", "ip": "127.0.0.9"}]}
        up2 = {"svc": None, "next": 0, "fails": 0, "prev": None, "link": 0, "error": ""}
        sm.read_upnp(up2, data2, {}, "auto", 0)
        check("UPnP: a gateway that does not answer gets a clear reason", up2["svc"] is None and "turn UPnP on" in up2["error"], up2)
    finally:
        sm.upnp_discover = real


def test_outages():
    T = 1_800_000_000
    inet = {}
    sm.inet_start(inet, T)
    ch = []
    for k in range(1, 241):                             # one hour, a check every 15 s; one check fails at T+600
        ch.append(sm.inet_update(inet, k * 15 != 600, T + k * 15))
    up, outs, cov = sm.month_uptime(inet, T + 3600, month_start=T - 86400)
    check("outages: a 15-second outage is recorded (not lost)", outs == [[T + 600, T + 615]] and "down" in ch, outs)
    check("outages: uptime over the hour is 99.583 %, everything monitored", up == 99.583 and cov == 100.0, (up, cov))
    inet2 = {}
    sm.inet_start(inet2, T)
    for k in range(1, 241):
        sm.inet_update(inet2, True, T + k * 15)
    check("outages: an hour without any outage is exactly 100 %", sm.month_uptime(inet2, T + 3600, T - 86400)[0] == 100.0)
    sm.inet_start(inet2, T + 7200)                     # the service was stopped for an hour
    up2, _, cov2 = sm.month_uptime(inet2, T + 7200, T - 86400)
    check("outages: an hour without the service is 'not monitored' - not up, not down",
          inet2["gaps"] == [[T + 3600, T + 7200]] and up2 == 100.0 and cov2 == 50.0, (inet2.get("gaps"), up2, cov2))
    inet3 = {}
    sm.inet_start(inet3, T)
    for k in range(1, 241):
        sm.inet_update(inet3, k * 15 < 3000, T + k * 15)   # internet down from T+3000, still down when the service stops
    sm.inet_start(inet3, T + 7200)
    up3, outs3, cov3 = sm.month_uptime(inet3, T + 7200, T - 86400)
    check("outages: an outage going on when the service stopped ends at the last known moment",
          outs3 == [[T + 3000, T + 3600]] and "down_since" not in inet3 and cov3 == 50.0 and up3 == round(100 * (1 - 600 / 3600), 3),
          (outs3, up3, cov3))
    inet4 = {"since": T - 3600, "outages": [[T - 600, T + 600]], "seen": T + 3600}
    up4, outs4, _ = sm.month_uptime(inet4, T + 3600, month_start=T)
    check("outages: only the part inside this month counts", up4 == round(100 * (1 - 600 / 3600), 3) and outs4, up4)
    inet5 = {}
    sm.inet_start(inet5, T)
    sm.inet_update(inet5, True, T + 15)
    sm.inet_update(inet5, True, T + 400)                 # 385 s without a check while running (stuck): not monitored
    check("outages: a long pause while running is not monitored time either", inet5["gaps"] == [[T + 15, T + 400]], inet5.get("gaps"))


def test_reset():
    tmp = _sandbox()
    sm.STATE_FILE, sm.HIST_FILE, sm.PRESENCE_FILE = (os.path.join(tmp, "state", f) for f in ("st.json", "hist.json", "pres.json"))
    sm.INET_FILE, sm.USAGE_FILE, sm.SPEED_FILE = (os.path.join(tmp, "state", f) for f in ("inet.json", "usage.json", "speed.json"))
    sm.EVENTS_FILE = os.path.join(tmp, "state", "ev.json")
    for f in (sm.STATE_FILE, sm.HIST_FILE, sm.PRESENCE_FILE, sm.INET_FILE, sm.USAGE_FILE, sm.SPEED_FILE, sm.EVENTS_FILE):
        json.dump({"old": 1}, open(f, "w"))
    for f in (sm.DATA, sm.LIVE, sm.RT):
        json.dump({"old": 1}, open(os.path.join(sm.DATA_DIR, f), "w"))
    json.dump({"by": "Admin", "reset": {"keep_devices": True, "keep_speedtests": True}}, open(sm.SETTINGS_REQ, "w"))
    sm.Scheduler.command = staticmethod(lambda name: ["sh", "-c", "sleep 30"])
    sch = sm.Scheduler()
    sch.start("scan", "scheduled")
    sch.tick(int(time.time()), sm.effective_settings({}))
    r = sch.config_result
    bdir = [d for d in os.listdir(sm.STATE_DIR) if d.startswith("reset-")]
    check("reset: the running scan is stopped, the service is told to restart and scan at once",
          r["ok"] and sch.restart and "scan" not in sch.proc and sch.jobs["scan"]["last_start"] == 0, (r, sch.jobs["scan"]))
    check("reset: history moved to a backup folder (not deleted), the speed test history kept",
          len(bdir) == 1 and sorted(os.listdir(os.path.join(sm.STATE_DIR, bdir[0]))) == sorted(["st.json", "hist.json", "pres.json", "inet.json", "usage.json", "ev.json"])
          and os.path.exists(sm.SPEED_FILE), os.listdir(os.path.join(sm.STATE_DIR, bdir[0])) if bdir else None)
    check("reset: the old map is gone from the page data", not any(os.path.exists(os.path.join(sm.DATA_DIR, f)) for f in (sm.DATA, sm.LIVE, sm.RT)))
    check("reset: device names kept, the reset is in the audit log and the new activity feed",
          json.load(open(sm.DEVICES_FILE))["devices"] and json.loads(open(sm.AUDIT_FILE).readlines()[-1])["action"] == "reset"
          and "reset the map" in json.load(open(sm.EVENTS_FILE))[-1]["text"])
    time.sleep(1.1)
    r2 = sm.apply_settings_request({"by": "Admin", "reset": {"keep_devices": False, "keep_speedtests": False}})
    check("reset: 'forget device names' empties devices.json (kept in saved versions) and the speed tests",
          r2["ok"] and json.load(open(sm.DEVICES_FILE))["devices"] == {} and sm.config_history()[0]["why"] == "before map reset"
          and not os.path.exists(sm.SPEED_FILE), r2)


def _fake_speed_servers(down_bps=40e6, up_bps=10e6, fast_token=True):
    """Imitations of speed.cloudflare.com and fast.com on one local port, limited to a known line speed."""
    import http.server, threading
    lock = threading.Lock()
    budget = {"down": [None, 0.0], "up": [None, 0.0]}

    def throttle(kind, n, bps):                       # one shared limit for all connections, like a real line
        with lock:
            if budget[kind][0] is None:                # the clock starts with the first byte, no saved-up credit
                budget[kind][0] = time.monotonic()
            t0, used = budget[kind]
            used += n
            budget[kind][1] = used
            wait = t0 + used * 8 / bps - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, body, ctype="text/plain"):
            self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            p = self.path
            if p == "/meta":
                return self._send(json.dumps({"colo": "ATH", "city": "Athens", "country": "GR", "asOrganization": "Test ISP"}).encode(), "application/json")
            if p == "/":
                return self._send(b'<html><script src="/app-1a2b3c.js"></script></html>', "text/html")
            if p == "/app-1a2b3c.js":
                return self._send(b'var a={https:!0,token:"YXNkZmFzZGxmbnNkYWZoYXNk",urlCount:5}' if fast_token else b"var a={}")
            if p.startswith("/api"):
                tg = [{"url": f"http://127.0.0.1:{self.server.server_port}/speedtest?c=gr&n={i}", "location": {"city": "Athens", "country": "GR"}} for i in range(3)]
                return self._send(json.dumps({"client": {"isp": "Test ISP"}, "targets": tg}).encode(), "application/json")
            m = re.search(r"bytes=(\d+)|/range/(\d+)-(\d+)", p)
            n = int(m.group(1)) if m and m.group(1) is not None else (int(m.group(3)) - int(m.group(2)) + 1 if m else 0)
            n = 0 if (m and m.group(3) == "0") else n
            self.send_response(200); self.send_header("Content-Length", str(n)); self.end_headers()
            sent, chunk = 0, b"\0" * 65536
            try:
                while sent < n:
                    c = chunk[:min(65536, n - sent)]
                    throttle("down", len(c), down_bps)
                    self.wfile.write(c); sent += len(c)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            got = 0
            while got < n:
                c = self.rfile.read(min(65536, n - got)); got += len(c)
                throttle("up", len(c), up_bps)
            self._send(b"ok")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def test_speed_providers():
    real_ping, real = sm.ping_stats, (sm.CF_BASE, sm.FAST_HOME, sm.FAST_API, sm.SPEED_SECONDS)
    sm.ping_stats = lambda ip, count=4: {"ms": 12.0, "loss": 0.0, "jit": 1.0}
    sm.SPEED_SECONDS = 4
    try:
        base, srv = _fake_speed_servers()
        sm.CF_BASE = base
        r = sm._speedtest_cloudflare()
        check("Cloudflare: download and upload close to the line (40 / 10 Mbps)",
              30e6 < r["down"] < 46e6 and 7e6 < r["up"] < 12e6, (round(r["down"] / 1e6, 1), round(r["up"] / 1e6, 1)))
        check("Cloudflare: latency, latency under load, location and provider",
              r["ping"] is not None and r["load_down"] == 12.0 and r["server"] == "Cloudflare ATH, Athens, GR"
              and r["isp"] == "Test ISP" and r["engine"] == "Cloudflare", r)
        srv.shutdown()
        base, srv = _fake_speed_servers()
        sm.FAST_HOME, sm.FAST_API = base, base + "/api"
        r = sm._speedtest_fast()
        check("Fast.com: token read, the three targets used, download and upload close to the line",
              30e6 < r["down"] < 46e6 and 7e6 < r["up"] < 12e6 and r["server"] == "Fast.com, Athens, GR" and r["engine"] == "Fast.com",
              (round(r["down"] / 1e6, 1), round(r["up"] / 1e6, 1), r["server"]))
        srv.shutdown()
        base, srv = _fake_speed_servers(fast_token=False)
        sm.FAST_HOME, sm.FAST_API = base, base + "/api"
        try:
            sm._speedtest_fast()
            msg = ""
        except RuntimeError as e:
            msg = str(e)
        check("Fast.com: if Netflix changes its page, a clear message instead of a crash", "changed" in msg and "Ookla" in msg, msg)
        srv.shutdown()
        sm.CF_BASE = "http://127.0.0.1:9"                 # nothing listens there
        try:
            sm._speedtest_cloudflare()
            msg = ""
        except RuntimeError as e:
            msg = str(e)
        check("Cloudflare: an unreachable server gives a clear message", "does not answer" in msg, msg)
    finally:
        sm.ping_stats = real_ping
        sm.CF_BASE, sm.FAST_HOME, sm.FAST_API, sm.SPEED_SECONDS = real
    check("settings: the provider is one of Ookla, Cloudflare, Fast.com (nPerf is not offered)",
          not sm.validate_settings({"speedtest_provider": "cloudflare"})[1] and sm.validate_settings({"speedtest_provider": "nperf"})[1])


def test_measure_wan():
    S = sm.effective_settings({})
    data = {"nodes": [{"kind": "router", "ip": "192.168.1.1"}]}
    gw = {"name": "MikroTik", "_snmp": {"ip": "192.168.1.1"}, "_ips": ["192.168.1.1"], "_macros": {}}
    sw = {"rx": 5e6, "tx": 1e6}
    real = (sm.read_router_snmp, sm.read_upnp)
    calls = []

    def fake(snmp_ok, upnp_ok):
        def rs(rsnmp, h, t0, usage, name=""):
            calls.append("snmp")
            rsnmp["error"] = "" if snmp_ok else "router MikroTik over SNMP: Timeout"
            return {"rx": 90e6, "tx": 9e6} if snmp_ok else None

        def ru(upnp, data, usage, source, t0, switch_rate=None):
            calls.append("upnp")
            upnp["error"] = "" if upnp_ok else "router not used: its byte counters stopped at 2,147,483,647"
            return {"rx": 80e6, "tx": 8e6} if upnp_ok else None
        sm.read_router_snmp, sm.read_upnp = rs, ru
    try:
        r = sm.measure_wan(dict(S, wan_source="switch"), data, [gw], sw, {}, {}, {}, 0)
        check("WAN: 'switch port' chosen in Settings -> the switch port", r == (sw, "switch", ""), r)
        fake(True, True); calls.clear()
        r = sm.measure_wan(S, data, [gw], sw, {}, {}, {}, 0)
        check("WAN: the gateway is an SNMP host -> its WAN over SNMP, UPnP not even asked", r[1] == "snmp" and calls == ["snmp"], (r, calls))
        fake(False, True)
        r = sm.measure_wan(S, data, [gw], sw, {}, {}, {}, 0)
        check("WAN: SNMP fails -> the router over UPnP", r[1] == "upnp" and r[0]["rx"] == 80e6, r)
        fake(False, False)
        r = sm.measure_wan(S, data, [gw], sw, {}, {}, {}, 0)
        check("WAN: nothing works -> no numbers (not the switch port), with both reasons",
              r[0] is None and r[1] is None and "SNMP: Timeout" in r[2] and "2,147,483,647" in r[2], r)
        r = sm.measure_wan(S, data, [], sw, {}, {}, {}, 0)
        check("WAN: gateway not in Zabbix and UPnP stuck -> the reason says how to get exact figures",
              r[0] is None and "add the gateway to Zabbix with an SNMP interface" in r[2], r)
    finally:
        sm.read_router_snmp, sm.read_upnp = real
    check("WAN: the 0.5.1 setting 'auto' now means the router",
          sm.effective_settings({"wan_source": "auto"})["wan_source"] == "router" and S["wan_source"] == "router")


def test_uninstall():
    tmp = tempfile.mkdtemp(prefix="spidermap-web-")
    open(os.path.join(tmp, "spidermap.php"), "w").write(sm.gateway_code({}, "http://x/api_jsonrpc.php"))
    open(os.path.join(tmp, "spidermap.html"), "w").write(sm.redirect_stub("spidermap.php"))
    open(os.path.join(tmp, "netmap.html"), "w").write("<html>my own page</html>")
    open(os.path.join(tmp, "index.php"), "w").write("<?php // Zabbix")
    os.makedirs(os.path.join(tmp, sm.ASSETS))
    sm.uninstall_web(tmp, dry=True)
    check("uninstall: a dry run removes nothing", len(os.listdir(tmp)) == 5)
    sm.uninstall_web(tmp)
    check("uninstall: only SpiderMap's files go (your own netmap.html and Zabbix stay)",
          sorted(os.listdir(tmp)) == ["index.php", "netmap.html"], os.listdir(tmp))
    calls = []

    class Api:
        def call(self, m, p=None):
            calls.append((m, p))
            return {"item.get": [{"itemid": "1", "key_": "autotopo.new", "hostid": "1"}, {"itemid": "2", "key_": "autotopo.down", "hostid": "1"}],
                    "dashboard.get": [{"dashboardid": "7", "name": "SpiderMap"}], "map.get": []}.get(m, {})
    sm.uninstall_zabbix(Api(), dry=True)
    check("uninstall: a dry run sends nothing to delete to Zabbix", not any(m.endswith(".delete") for m, _ in calls))
    calls.clear()
    sm.uninstall_zabbix(Api())
    get = next(p for m, p in calls if m == "item.get")
    check("uninstall: only 'autotopo.' items (their triggers go with them) and SpiderMap's dashboard",
          get["search"] == {"key_": "autotopo."} and get["startSearch"] is True
          and [(m, p) for m, p in calls if m.endswith(".delete")] == [("item.delete", ["1", "2"]), ("dashboard.delete", ["7"])], calls)


def test_approve():
    tmp = tempfile.mkdtemp(prefix="spidermap-appr-")
    sm.DEVICES_FILE, sm.EDITS_FILE = os.path.join(tmp, "devices.json"), os.path.join(tmp, "edits.json")
    sm.AUDIT_FILE, sm.EVENTS_FILE, sm.LOCK_FILE = os.path.join(tmp, "audit.log"), os.path.join(tmp, "ev.json"), os.path.join(tmp, ".lock")
    json.dump({"settings": {}, "devices": {}}, open(sm.DEVICES_FILE, "w"))
    json.dump({"aa:bb:cc:dd:ee:09": {"approved": True, "by": "Admin", "at": 1}}, open(sm.EDITS_FILE, "w"))
    sm.apply_edits()
    dev = json.load(open(sm.DEVICES_FILE))["devices"]
    ev = json.load(open(sm.EVENTS_FILE))[-1]["text"]
    check("approve: written to devices.json, logged as 'approved'", dev == {"aa:bb:cc:dd:ee:09": {"approved": True}}
          and "approved" in ev and json.loads(open(sm.AUDIT_FILE).read().splitlines()[-1])["changes"] == {"approved": [None, True]}, (dev, ev))
    json.dump({"aa:bb:cc:dd:ee:09": {"approved": False, "by": "Admin", "at": 2}}, open(sm.EDITS_FILE, "w"))
    sm.apply_edits()
    check("approve: taking it back removes it", json.load(open(sm.DEVICES_FILE))["devices"]["aa:bb:cc:dd:ee:09"] == {})
    real = sm.snmp_get
    sm.snmp_get = lambda auth, target, oids: {oids[0]: 1000}
    try:
        check("speed: a port's current speed is read again on demand", sm.recheck_speed(None, "t", "5") == 1000)
    finally:
        sm.snmp_get = real


def test_070():
    class Api:
        def call(self, m, p=None):
            return [{"description": "Interface Gi0/1 down", "priority": "4", "lastchange": "100", "hosts": [{"hostid": "11"}], "tags": []},
                    {"description": "Network: new device(s)", "priority": "2", "lastchange": "90", "hosts": [{"hostid": "11"}],
                     "tags": [{"tag": "source", "value": "autotopo"}]},
                    {"description": "High CPU", "priority": "2", "lastchange": "80", "hosts": [{"hostid": "12"}], "tags": []}]
    pr = sm.zabbix_problems(Api(), {"11", "12"})
    check("Zabbix problems: worst severity per host, SpiderMap's own alerts left out",
          pr == {"11": {"sev": 4, "list": [["Interface Gi0/1 down", 4, 100]]}, "12": {"sev": 2, "list": [["High CPU", 2, 80]]}}, pr)
    check("Zabbix problems: no hosts, no question to Zabbix", sm.zabbix_problems(Api(), set()) == {})
    import spidermap_ports as zp
    cdp = zp.cdp_records({"10101": "Gi0/1"}, {"10101.3": '"SW-Core.example.com(FOC1234X)"'}, {"10101.3": '"GigabitEthernet1/0/24"'},
                         {"10101.3": "0A 00 00 02"})
    check("CDP: neighbour name (serial removed), its port and address, in LLDP's form",
          cdp == [{"local_port": "Gi0/1", "local_names": ["Gi0/1"], "sysname": "SW-Core.example.com", "chassis": "",
                   "remote_port": "GigabitEthernet1/0/24", "mgmt_ip": "10.0.0.2", "via": "cdp"}], cdp)
    _sandbox()
    json.dump({"switches": {"1": {"name": "SW-Core", "ports": [{"label": "Port 7 (SFP+)", "base": "Port 7"}]}}},
              open(os.path.join(sm.DATA_DIR, sm.DATA), "w"))
    r = sm.apply_settings_request({"by": "Admin", "place": {"key": "SW-Core|Port 7", "kind": "switch", "name": "Office switch"}})
    cfg = json.load(open(sm.DEVICES_FILE))
    check("map edit: a switch confirmed on a port is saved, a scan is asked for",
          r["ok"] and r.get("rescan") and cfg["places"] == {"SW-Core|Port 7": {"kind": "switch", "name": "Office switch"}}, (r, cfg))
    bad = [sm.apply_settings_request({"by": "A", "place": pl}) for pl in
           ({"key": "SW-Core|Port 99", "kind": "switch"}, {"key": "SW-Core|Port 7", "kind": "printer"},
            {"key": "SW-Core|Port 7", "kind": "switch", "name": "<script>"})]
    check("map edit: unknown port, unknown kind and HTML are refused", all(not b["ok"] for b in bad), bad)
    r = sm.apply_settings_request({"by": "Admin", "place": {"key": "SW-Core|Port 7", "kind": "remove"}})
    check("map edit: Remove takes it away again", r["ok"] and json.load(open(sm.DEVICES_FILE))["places"] == {}, r)


def main():
    test_gateway_step()
    test_speedtest_due()
    test_apply_edits()
    test_job_lock()
    test_audit()
    test_snmp_secrets()
    test_permissions()
    test_scheduler()
    test_settings_apply()
    test_config_location()
    test_dialog_settings()
    test_upnp()
    test_upnp_stuck()
    test_measure_wan()
    test_outages()
    test_reset()
    test_speed_providers()
    test_uninstall()
    test_approve()
    test_070()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
