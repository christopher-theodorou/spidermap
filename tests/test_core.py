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
    sm.HERE = tmp
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
    cases = [({}, {"view": 3, "edit": 3, "speedtest": 3}),
             ({"view_min_user_type": 1}, {"view": 1, "edit": 2, "speedtest": 2}),
             ({"permissions": {"view": 1, "edit": 1, "speedtest": 1}}, {"view": 1, "edit": 2, "speedtest": 1}),
             ({"permissions": {"view": 2, "speedtest": 1}}, {"view": 2, "edit": 2, "speedtest": 2}),
             ({"permissions": {"edit": "x"}}, {"view": 3, "edit": 3, "speedtest": 3})]
    check("permissions: defaults, limits (edits never below admin, nothing below view) and bad values",
          all(sm.permissions(st) == want for st, want in cases), [(st, sm.permissions(st)) for st, _ in cases])


def main():
    test_gateway_step()
    test_speedtest_due()
    test_apply_edits()
    test_job_lock()
    test_audit()
    test_snmp_secrets()
    test_permissions()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
