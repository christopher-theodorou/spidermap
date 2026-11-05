#!/usr/bin/env python3
"""
Tests for SpiderMap's core logic. No network, no Zabbix, nothing installed or changed on the system.

    python3 tests/test_core.py

Run it before every release. Every line must say OK.
"""
import datetime
import json
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

    S2 = dict(S, discovery_paused=True)
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
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
