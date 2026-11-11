#!/usr/bin/env python3
"""
spidermap.py - SpiderMap: live network topology for Zabbix. "Nothing touches the web unnoticed."
Normally installed and scheduled by install.sh - see README.md.

  python3 spidermap.py --setup            # page, icons, Zabbix items/triggers, dashboard, service
  python3 spidermap.py                    # full scan          (cron: every 5 minutes)
  python3 spidermap.py --live             # live status/traffic (cron: every minute)
  python3 spidermap.py --rt               # real-time collector (run by the spidermap-rt service)
  python3 spidermap.py --install-service  # (re)install the spidermap-rt service

Needs spidermap_zabbix.py, spidermap_ports.py and spidermap.html in the same folder.
Your own names / icons / pins go in devices.json (created by --setup).
"""
import argparse
import concurrent.futures as cf
import datetime
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

import requests

sys.modules.setdefault("spidermap", sys.modules[__name__])   # the modules below import helpers from here
import spidermap_zabbix as at  # noqa: E402
import spidermap_ports as zp

__version__ = "0.9.12"   # keep in sync with PAGE_VERSION in spidermap.html and CHANGELOG.md

HERE = os.path.dirname(os.path.abspath(__file__))          # the code
CONF_DIR, STATE_DIR = at.CONF_DIR, at.STATE_DIR              # see spidermap_zabbix.py: /etc/spidermap, /var/lib/spidermap/state
DEVICES_FILE = os.path.join(CONF_DIR, "devices.json")
STATE_FILE = os.path.join(STATE_DIR, "spidermap_state.json")
DATA_DIR = "/var/lib/spidermap/www"   # topology data: NOT in the web folder, served only to logged-in Zabbix users
PAGE, DATA, LIVE, ASSETS = "spidermap.html", "spidermap.json", "spidermap_live.json", "spidermap-assets"
DASHBOARD_NAME = "SpiderMap"
OLD_DASHBOARDS = ["Network topology (live)"]   # earlier versions (zabbix-netmap)

# Optional read-only Proxmox API token (gives names + state of every VM / container)
PVE_URL = os.environ.get("PVE_URL", "")                 # e.g. https://192.168.1.10:8006
PVE_TOKEN_ID = os.environ.get("PVE_TOKEN_ID", "")       # e.g. spidermap@pve!spidermap
PVE_TOKEN_SECRET = os.environ.get("PVE_TOKEN_SECRET", "")
# How to trust the Proxmox certificate (strongest first). Without any of these the connection is not verified.
PVE_FINGERPRINT = os.environ.get("PVE_FINGERPRINT", "")   # SHA-256 of its certificate (works with self-signed)
PVE_CA = os.environ.get("PVE_CA", "")                     # path to the CA certificate (pve-root-ca.pem)
PVE_VERIFY = os.environ.get("PVE_VERIFY", "") == "1"      # certificate trusted by the system (e.g. Let's Encrypt)

TABLER = "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/"
LOGO_URL = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/{}.svg"
LOGOS = {"grafana": "grafana", "immich": "immich", "influxdb": "influxdb", "zabbix": "zabbix",
         "proxmox": "proxmox", "omv": "openmediavault", "openmediavault": "openmediavault",
         "homeassistant": "home-assistant", "home-assistant": "home-assistant", "nextcloud": "nextcloud",
         "jellyfin": "jellyfin", "plex": "plex", "pihole": "pi-hole", "adguard": "adguard-home",
         "portainer": "portainer", "uptime-kuma": "uptime-kuma", "unifi": "unifi", "cloud key": "unifi"}

# Web UI address per app (matched on the device name); devices.json "url" overrides it.
# {ip} is replaced by the device's IP.
APP_URLS = {"grafana": "http://{ip}:3000", "immich": "http://{ip}:2283", "influxdb": "http://{ip}:8086",
            "proxmox": "https://{ip}:8006", "zabbix": "http://{ip}/zabbix", "cloud key": "https://{ip}",
            "unifi": "https://{ip}", "omv": "http://{ip}", "home-assistant": "http://{ip}:8123",
            "homeassistant": "http://{ip}:8123", "portainer": "https://{ip}:9443",
            "uptime-kuma": "http://{ip}:3001", "nextcloud": "https://{ip}", "jellyfin": "http://{ip}:8096",
            "plex": "http://{ip}:32400/web", "pihole": "http://{ip}/admin", "adguard": "http://{ip}:3000",
            "node-red": "http://{ip}:1880", "nginx-proxy-manager": "http://{ip}:81"}


def web_url(name, ip, cfg_url=""):
    if cfg_url:
        return cfg_url.replace("{ip}", ip or "")
    if not ip:
        return ""
    key = (name or "").lower()
    for k, tpl in APP_URLS.items():
        if k in key:
            return tpl.replace("{ip}", ip)
    return f"http://{ip}"


TYPE_ICON = {"router": "router", "switch": "server-2", "ap": "access-point", "unifi": "cloud",
             "camera": "device-cctv", "tv": "device-tv", "laptop": "device-laptop",
             "desktop": "device-desktop", "phone": "device-mobile", "printer": "printer",
             "appliance": "wash-machine", "iot": "bulb", "nas": "database", "server": "server",
             "vm": "device-desktop", "lxc": "box", "guest": "box", "unknown": "help-circle",
             "firewall": "shield-lock", "ont": "building-broadcast-tower", "ups": "battery-charging", "voip": "phone",
             "hypervisor": "server-cog", "industrial": "building-factory-2", "mediaconv": "arrows-exchange",
             "oob": "terminal-2"}


EXTRA_OUI = {"bc:24:11": "Proxmox VM", "3c:dc:75": "Espressif", "cc:8d:a2": "Espressif",
             "48:55:19": "Espressif", "70:a7:41": "Ubiquiti", "98:3f:60": "Huawei",
             "dc:cd:2f": "Seiko Epson", "60:83:e7": "TP-Link", "98:5f:41": "Intel",
             "c0:25:a5": "Dell", "2c:44:fd": "HP", "b8:83:03": "HPE", "74:06:35": "Sernet (Speedport)",
             "1c:e8:9e": "Samsung", "48:26:4c": "BSH (Bosch)"}

# devices.json is created from this on first setup. Everything about YOUR network goes there, not here.
DEFAULT_DEVICES = {
    "settings": {"flag_hours": 24, "forget_after_days": 30, "speedtest_daily": "",
                 "core_switch": "", "manual_links": [], "aliases": {}, "scan_subnets": [],
                 "view_min_user_type": 3},
    "devices": {
        "_example:00:00:00:00:00": {"name": "Example camera", "type": "camera", "important": True,
                                    "url": "http://{ip}:8080", "pin": "SW-Office:5"},
    },
}
MAC_RX = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
ONLINE_STATES = {"REACHABLE", "DELAY", "PROBE", "PERMANENT"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now():
    return int(time.time())


def iso(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _web_gid():
    for u in ("www-data", "nginx", "apache", "http"):
        try:
            import pwd
            return pwd.getpwnam(u).pw_gid
        except (KeyError, ImportError):
            continue
    return None


def save_json(path, data):
    if path.startswith(DATA_DIR + "/"):
        ensure_data_dir()                          # also creates it on the very first run
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    if path.startswith(DATA_DIR + "/"):
        os.chmod(tmp, 0o640)                      # root + web server group only
        gid = _web_gid()
        if gid is not None:
            try:
                os.chown(tmp, -1, gid)
            except OSError:
                pass
    else:
        os.chmod(tmp, 0o644)
    os.replace(tmp, path)


EVENTS_FILE = os.path.join(STATE_DIR, "spidermap_events_all.json")
PRESENCE_FILE = os.path.join(STATE_DIR, "spidermap_presence_all.json")
INET_FILE = os.path.join(STATE_DIR, "spidermap_inet.json")
USAGE_FILE = os.path.join(STATE_DIR, "spidermap_usage.json")
LOCK_FILE = os.path.join(STATE_DIR, ".spidermap.lock")


def ev(kind, sev, text, node=None, t=None):
    """One activity-feed entry. sev: info | ok | warn | bad"""
    return {"t": t or now(), "type": kind, "sev": sev, "text": text, "node": node}


def audit(action, by, detail):
    """Append to the audit log (root only, 600). Kept to about 1 MB, plus one older file."""
    try:
        os.makedirs(EDITS_DIR, exist_ok=True)
        if os.path.exists(AUDIT_FILE) and os.path.getsize(AUDIT_FILE) > 1_000_000:
            os.replace(AUDIT_FILE, AUDIT_FILE + ".1")
        fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": now(), "by": by or "?", "action": action, **detail}, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"audit log: {e}", flush=True)


def add_events(evs):
    if not evs:
        return
    import fcntl
    with open(LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        allv = load_json(EVENTS_FILE, [])
        if not isinstance(allv, list):          # a damaged file: start a new history rather than stop
            allv = []
        allv.extend(evs)
        cut = now() - setting("events_keep_days") * 86400
        allv = [e for e in allv if e["t"] >= cut][-3000:]
        save_json(EVENTS_FILE, allv)
        ensure_data_dir()
        save_json(os.path.join(DATA_DIR, "spidermap_events.json"), allv[-400:])


def mbps_txt(bps):
    m = (bps or 0) / 1e6
    return f"{m / 1000:.2f} Gbps" if m >= 1000 else f"{m:.0f} Mbps" if m >= 10 else f"{m:.2f} Mbps"


RUN_AS_FILE = os.path.join(CONF_DIR, "run-as")       # written by install.sh: "spidermap", or "root" as a fallback


def service_user():
    """pwd entry of the user the service runs as (None = root / not set up yet)."""
    import pwd
    try:
        name = open(RUN_AS_FILE, encoding="utf-8").read().strip()
    except OSError:
        return None
    if not name or name == "root":
        return None
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def drop_privileges():
    """Root running a SpiderMap command becomes the service user, so it never leaves files the service cannot
    write. Only installation commands keep root."""
    if os.geteuid() != 0:
        return
    u = service_user()
    if not u:
        return
    os.initgroups(u.pw_name, u.pw_gid)
    os.setgid(u.pw_gid)
    os.setuid(u.pw_uid)
    os.environ["HOME"] = u.pw_dir
    os.umask(0o027)


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.geteuid() != 0:
        return                                  # created and owned by the installer
    os.chmod(DATA_DIR, 0o2750)                  # setgid: new files belong to the web server's group
    gid = _web_gid()
    u = service_user()
    try:
        os.chown(DATA_DIR, u.pw_uid if u else 0, gid if gid is not None else -1)
    except OSError:
        pass


def fix_ownership():
    """After root installed or migrated something: everything the service writes belongs to the service user.
    The web server keeps its inbox; the page data stays readable by the web server's group only."""
    if os.geteuid() != 0:
        return
    u, wu, wgid = service_user(), web_user(), _web_gid()
    uid, gid = (u.pw_uid, u.pw_gid) if u else (0, 0)

    def own(path, o, g, mode=None):
        try:
            os.chown(path, o, g)
            if mode is not None:
                os.chmod(path, mode)
        except OSError:
            pass
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(EDITS_DIR, exist_ok=True)
    own(EDITS_DIR, uid, gid, 0o755)
    for root_dir, mode in ((STATE_DIR, 0o700), (os.path.join(EDITS_DIR, "snmp"), 0o700), (CONF_HIST_DIR, 0o700)):
        if os.path.isdir(root_dir):
            for r, dirs, files in os.walk(root_dir):
                own(r, uid, gid, mode if r == root_dir else None)
                for f in files:
                    own(os.path.join(r, f), uid, gid)
    for f in os.listdir(EDITS_DIR):
        full = os.path.join(EDITS_DIR, f)
        if os.path.isfile(full):
            own(full, uid, gid, 0o600 if f.startswith(("audit.log", "spidermap.log")) else None)
    if os.path.isdir(DATA_DIR):
        own(DATA_DIR, uid, wgid if wgid is not None else gid, 0o2750)
        for f in os.listdir(DATA_DIR):
            own(os.path.join(DATA_DIR, f), uid, wgid if wgid is not None else gid, 0o640)
    if os.path.isdir(INBOX) and wu:                     # written by the web server, read and cleared by the service
        own(INBOX, wu.pw_uid, gid, 0o2770)
        for f in os.listdir(INBOX):
            full = os.path.join(INBOX, f)
            if os.path.isfile(full):
                own(full, wu.pw_uid, gid, 0o660)
        sess = os.path.join(INBOX, "sess")
        if os.path.isdir(sess):
            own(sess, wu.pw_uid, wu.pw_gid, 0o700)
    if CONF_DIR != HERE and os.path.isdir(CONF_DIR):
        own(CONF_DIR, uid, gid, 0o750)
        own(os.path.join(CONF_DIR, "zabbix.env"), 0, gid, 0o640)       # root may change it, the service only reads
        for f in ("devices.json", "run-as"):
            p_ = os.path.join(CONF_DIR, f)
            if os.path.exists(p_):
                own(p_, uid if f == "devices.json" else 0, gid, 0o640)


def webdir():
    return next((d for d in zp.WEB_DIRS if os.path.isfile(os.path.join(d, "api_jsonrpc.php"))), None)


def is_private_mac(mac):
    return bool(int(mac[1], 16) & 2)


def port_num(name):
    m = re.search(r"(\d+)$", name or "")
    return int(m.group(1)) if m else 0


def _port_family(name):
    """'GigabitEthernet0/0/5' -> 'gigabitethernet', 'sfp-sfpplus1' -> 'sfp-sfpplus', 'two-gigabitEthernet 1/0/3' -> ..."""
    return re.sub(r"[\d/\s.:_-]+$", "", name or "").lower()


def _port_unit(name):
    """Stack member from names like 'GigabitEthernet2/0/5' or 'Gi2/0/5' (None when there is no unit)."""
    m = re.search(r"(\d+)/\d+/\d+\s*$", name or "")
    return int(m.group(1)) if m else None


def label_ports(ports, media_override=None):
    """Vendor-neutral port labels, unique per switch. Identity is always the ifIndex (p["pid"]);
    the label is only for display.
      - one family of ports with unique numbers (GigabitEthernet0/0/1-24, ether1-8, Gi1/0/1-48): the device's
        own numbers -> Port 1..N
      - several families (ether1-8 + sfp-sfpplus1-2, Fa0/1-24 + Gi0/1-2): numbered in device order -> Port 1..N
      - stacks (Gi1/0/x and Gi2/0/x): Port 1/x, Port 2/x
      - link aggregations: LAG 1..N
    Pluggable ports are marked: "Port 9 (SFP+)". The type comes from, in this order: media_override
    ({"9": "SFP+"} or {"<interface name>": "SFP+"}, "copper" removes it), the interface name when it says so
    (sfp, sfpplus, qsfp), or the device's hardware inventory (p["media_hw"]). Without any of these: no mark."""
    ifkey = lambda p: int(p["ifindex"]) if str(p["ifindex"]).isdigit() else 0  # noqa: E731

    def number(group):
        nums = [port_num(p["name"]) for p in group]
        if len({_port_family(p["name"]) for p in group}) == 1 and all(nums) and len(set(nums)) == len(nums):
            for p, n in zip(group, nums):
                p["pos"] = n
        else:
            for i, p in enumerate(sorted(group, key=ifkey), 1):
                p["pos"] = i

    phys = [p for p in ports if p.get("kind", "port") == "port"]
    lags = [p for p in ports if p.get("kind") == "lag"]
    multi = len({_port_unit(p["name"]) for p in phys} - {None}) > 1
    groups = {}
    for p in phys:
        groups.setdefault(_port_unit(p["name"]) if multi else None, []).append(p)
    for unit, group in groups.items():
        number(group)
        for p in group:
            p["label"] = f"Port {unit}/{p['pos']}" if unit is not None else f"Port {p['pos']}"
    if lags:
        number(lags)
        for p in lags:
            p["label"], p["pos"] = f"LAG {p['pos']}", 1000 + p["pos"]
    media_override = media_override or {}
    for p in phys:
        ov = media_override.get(str(p.get("pos"))) or media_override.get(p["name"])
        if ov is not None:
            media = "" if str(ov).strip().lower() in ("", "copper", "rj45", "none") else str(ov).strip()
        else:
            media = zp.media_from_text(p["name"]) or p.get("media_hw", "")
        p["media"], p["base"] = media, p["label"]
        if media:
            p["label"] = f"{p['label']} ({media})"
    seen = set()
    for p in sorted(ports, key=ifkey):
        p["pid"] = str(p["ifindex"])
        if p.get("label") in seen or not p.get("label"):
            p["label"] = p["name"]                 # never two ports with the same label
        seen.add(p["label"])
    return ports


def port_label(name):
    n = port_num(name)
    if re.match(r"(?i)(xge|xgigabit|10ge|25ge|40ge|100ge)", name or ""):
        return f"SFP+ {n}"
    if re.match(r"(?i)eth-trunk", name or ""):
        return f"LAG {n}"
    return f"Port {n}"


_OUI = None


def vendor_of(mac):
    global _OUI
    if mac[:8] in EXTRA_OUI:
        return EXTRA_OUI[mac[:8]]
    if is_private_mac(mac):
        return "private MAC"
    if _OUI is None:
        _OUI = {}
        for path in ("/usr/share/ieee-data/oui.txt", "/usr/share/nmap/nmap-mac-prefixes"):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        m = re.match(r"^([0-9A-F]{2})-([0-9A-F]{2})-([0-9A-F]{2})\s+\(hex\)\s+(.+)$", line.strip()) \
                            or re.match(r"^([0-9A-F]{2})([0-9A-F]{2})([0-9A-F]{2})\s+(.+)$", line.strip())
                        if m:
                            _OUI.setdefault(f"{m.group(1)}:{m.group(2)}:{m.group(3)}".lower(), m.group(4).strip())
            except FileNotFoundError:
                pass
    return _OUI.get(mac[:8], "")


def logo_for(name, webd):
    key = (name or "").lower()
    for k, slug in LOGOS.items():
        if k in key and webd and os.path.isfile(os.path.join(webd, ASSETS, "logos", slug + ".svg")):
            return f"{ASSETS}/logos/{slug}.svg"
    return ""


def arp_table():
    try:
        out = subprocess.run(["ip", "neigh"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return {}
    res = {}
    for line in out.splitlines():
        p = line.split()
        if "lladdr" in p and p[0].count(".") == 3:
            res[p[p.index("lladdr") + 1].lower()] = (p[0], p[-1])
    return res


def ping_many(ips):
    def one(ip):
        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "1", ip], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=3)
            return ip, r.returncode == 0
        except Exception:  # noqa: BLE001
            return ip, False
    with cf.ThreadPoolExecutor(32) as ex:
        return dict(ex.map(one, ips))


class _PinnedAdapter(requests.adapters.HTTPAdapter):
    """HTTPS that accepts exactly one certificate, identified by its SHA-256 fingerprint."""
    def __init__(self, fingerprint, **kw):
        self.fingerprint = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, *a, **kw):
        kw["assert_fingerprint"] = self.fingerprint
        super().init_poolmanager(*a, **kw)


class _PveSession(requests.Session):
    """Passes the certificate setting with every request: a session-wide verify can otherwise be overridden by
    REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE in the environment."""
    def request(self, method, url, **kw):
        kw.setdefault("verify", self.verify)
        return super().request(method, url, **kw)


def pve_session():
    """requests session for the Proxmox API with the configured certificate check."""
    s = _PveSession()
    fp = re.sub(r"[^0-9a-fA-F]", "", PVE_FINGERPRINT).lower()
    if fp:
        if len(fp) != 64:
            raise RuntimeError("PVE_FINGERPRINT must be a SHA-256 fingerprint (64 hex digits)")
        requests.packages.urllib3.disable_warnings()
        s.verify = False                      # the chain is not checked; the exact certificate is
        s.mount("https://", _PinnedAdapter(fp))
    elif PVE_CA:
        s.verify = PVE_CA
    elif PVE_VERIFY:
        s.verify = True
    else:
        requests.packages.urllib3.disable_warnings()
        s.verify = False
        s.unverified = True
    return s


def pve_guests():
    if not (PVE_URL and PVE_TOKEN_ID and PVE_TOKEN_SECRET):
        return []
    s = pve_session()
    if getattr(s, "unverified", False):
        print("Proxmox: certificate not verified - set PVE_FINGERPRINT in zabbix.env (see README)", flush=True)
    s.headers["Authorization"] = f"PVEAPIToken={PVE_TOKEN_ID}={PVE_TOKEN_SECRET}"
    base = PVE_URL.rstrip("/") + "/api2/json"
    out = []
    for node in s.get(base + "/nodes", timeout=10).json()["data"]:
        nn = node["node"]
        for kind in ("qemu", "lxc"):
            for g in s.get(f"{base}/nodes/{nn}/{kind}", timeout=10).json()["data"]:
                cfg = s.get(f"{base}/nodes/{nn}/{kind}/{g['vmid']}/config", timeout=10).json()["data"]
                macs = [m.lower() for k, v in cfg.items() if re.match(r"net\d+$", k)
                        for m in re.findall(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}", str(v))]
                out.append({"vmid": g["vmid"], "name": g.get("name") or f"{kind} {g['vmid']}",
                            "kind": "vm" if kind == "qemu" else "lxc", "status": g.get("status", ""),
                            "node": nn, "macs": macs})
    return out


# --------------------------------------------------------------------------- #
# Full scan
# --------------------------------------------------------------------------- #
def full_scan(api, community=None, quiet=False):
    say = (lambda *a: None) if quiet else print
    t0 = now()
    apply_edits()
    cfg = load_json(DEVICES_FILE, DEFAULT_DEVICES)
    dcfg = {k.lower(): v for k, v in cfg.get("devices", {}).items() if MAC_RX.match(k.lower())}
    flag_s = cfg.get("settings", {}).get("flag_hours", 24) * 3600
    forget_s = cfg.get("settings", {}).get("forget_after_days", 30) * 86400
    state = load_json(STATE_FILE, {"baseline": 0, "devices": {}})
    baseline = bool(state.get("baseline"))
    sdev = state["devices"]
    webd = webdir()
    report = {"started": t0, "switches": {}, "warnings": [], "partial": False}

    hosts = at.load_hosts(api)
    switches = [h for h in hosts if h["_snmp"] and str(h["status"]) == "0"]
    host_by_ip = {ip: h for h in hosts for ip in h["_ips"]}

    S = effective_settings(cfg.get("settings", {}))
    scan_progress(2, "starting")
    outside, auto_nets = [], ([] if S["scan_subnets"] else zp.local_nets())
    if S["scan_subnets"] or auto_nets:
        nets_ = [ipaddress.ip_network(n, strict=False) for n in (S["scan_subnets"] or auto_nets)]

        def _inside(h):
            try:
                return any(ipaddress.ip_address((h["_snmp"] or {}).get("ip", "")) in n for n in nets_)
            except ValueError:
                return True                                  # a DNS name: we cannot tell, so it stays
        outside = [h["name"] for h in switches if not _inside(h)]
        if not S["scan_subnets"] and len(outside) == len(switches):
            outside = []                                     # no switch on this server's own network: the switches
            say("No SNMP host is on this server's own network - scanning them all (Settings > Scans > Allowed networks)")
        else:                                                # live on a management network of their own, routed to us
            switches = [h for h in switches if _inside(h)]
        if outside:
            say(f"Not scanned (outside {'the allowed networks' if S['scan_subnets'] else 'the networks this server is on: ' + ', '.join(auto_nets)}): {', '.join(outside)}")
    at.SNMP_WALK.update(timeout_s=S["snmp_timeout_ms"] / 1000, retries=S["snmp_retries"], max_rep=S["snmp_max_repetitions"])
    zp.CDP_ON = S["topo_cdp"]
    nets = S["scan_subnets"] or auto_nets or sorted({str(ipaddress.ip_network(f"{h['_snmp']['ip']}/24", strict=False))
                                                     for h in switches if h["_snmp"].get("ip")})
    targets = list(scan_targets(nets, S["scan_exclude"]))
    if not S["icmp_discovery"]:
        say("Ping sweep off (Settings > Scans): only devices the network already knows are seen")
    elif len(targets) > S["max_targets"]:
        say(f"Ping sweep skipped: {len(targets)} addresses, more than the limit of {S['max_targets']} (Settings > Scans)")
        report["warnings"].append(f"Ping sweep skipped: {len(targets)} addresses > limit {S['max_targets']}")
    else:
        say(f"Ping sweep {', '.join(nets)} ({len(targets)} addresses)...")
        scan_progress(5, "ping sweep")
        zp.sweep_ips(targets, S["ping_timeout_ms"], S["ping_parallel"], S["ping_size"], S["scan_pps"])
    arp = arp_table()

    sw = {}
    cache = state.setdefault("switch_cache", {})
    for i_sw, h in enumerate(switches):
        scan_progress(40 + 40 * i_sw / max(len(switches), 1), f"reading {h['name']}")
        t_sw = now()
        try:
            if not S["snmp_collection"]:
                raise RuntimeError("SNMP collection is off (Settings > Scans)")
            ports, sarp = zp.scan_switch(h, community, hosts)
            # a plain-JSON copy (sets become sorted lists), so the cache can always be saved with the state
            cache[h["hostid"]] = {"t": t_sw, "ports": json.loads(json.dumps(ports, default=sorted)), "sarp": dict(sarp),
                                  "vlans": {str(k): v for k, v in zp.VLANS.get(h["hostid"], {}).items()}}
            report["switches"][h["hostid"]] = {"name": h["name"], "ok": True, "ports": len(ports), "seconds": now() - t_sw}
        except Exception as e:  # noqa: BLE001
            # one switch not answering must not make its devices look offline: use its last good data for 30 min
            old = cache.get(h["hostid"])
            msg = str(e)[:200] or type(e).__name__
            if old and (t0 - old["t"] <= 1800 or not S["snmp_collection"]):
                ports, sarp = old["ports"], old["sarp"]
                zp.VLANS[h["hostid"]] = {int(k): v for k, v in (old.get("vlans") or {}).items()}
                report["partial"] = True
                report["switches"][h["hostid"]] = {"name": h["name"], "ok": False, "error": msg,
                                                   "used_from": old["t"], "ports": len(ports)}
                say(f"  ! {h['name']}: {msg} - using its data from {iso(old['t'])}")
            else:
                report["switches"][h["hostid"]] = {"name": h["name"], "ok": False, "error": msg}
                report["partial"] = True                 # a switch is missing from the map
                say(f"  ! {h['name']}: {msg}")
                continue
        if not S["topo_lldp"] or not S["topo_fdb"]:          # link sources (Settings > Topology)
            ports = [dict(p, lldp=p["lldp"] if S["topo_lldp"] else [], macs=p["macs"] if S["topo_fdb"] else [])
                     for p in ports]
        sw[h["hostid"]] = {"host": h, "ports": ports}
        for mac, ip in (sarp.items() if S["topo_arp"] else ()):
            arp.setdefault(mac, (ip, "STALE"))
        say(f"  {h['name']}: {len(ports)} ports, {sum(p['status'] == 'up' for p in ports)} up")
    scan_progress(82, "building the map")
    vlan_names, mac_vlan, v6_of = {}, {}, {}
    for sid in sw:
        for m_, ips_ in zp.V6.get(sid, {}).items():
            v6_of.setdefault(m_, []).extend(ips_)
    if S["topo_vlans"]:
        for sid, s_ in sw.items():
            vlan_names.update(zp.VLANS.get(sid, {}))
            for p in s_["ports"]:
                for m_, v_ in (p.get("mac_vlans") or {}).items():
                    if len(p.get("macs") or []) <= 2 or m_ not in mac_vlan:     # an access port knows best
                        mac_vlan[m_] = v_
    port_media = cfg.get("settings", {}).get("port_media", {})
    for s_ in sw.values():
        label_ports(s_["ports"], port_media.get(s_["host"]["name"]) or port_media.get(s_["host"]["host"]) or {})
    _pinfo = {(sid, p["name"]): p for sid, s_ in sw.items() for p in s_["ports"]}

    def plabel(sid, pname):
        return _pinfo[(sid, pname)]["label"] if (sid, pname) in _pinfo else pname

    def pid_of(sid, pname):
        return _pinfo[(sid, pname)]["pid"] if (sid, pname) in _pinfo else pname

    def pos_of(sid, pname):
        return _pinfo[(sid, pname)].get("pos", 0) if (sid, pname) in _pinfo else 0
    not_switches = [k for k, s_ in sw.items() if not any(p["macs"] for p in s_["ports"])
                    and sum(1 for p in s_["ports"] if p.get("kind", "port") == "port") < 5]
    demoted = {}
    for k in not_switches:                                   # an access point, NAS, IPMI ...: a device, still linked to Zabbix
        role = zp.ROLE.get(k)
        say(f"  {sw[k]['host']['name']}: no MAC table and fewer than 5 ports - drawn as "
            + (f"an access point ({role[1]})" if role else "a device, not as a switch"))
        demoted[k] = sw.pop(k)["host"]
    ipmap = {m: v[0] for m, v in arp.items()}
    # what such a host is, by its MACs (its management address, its interfaces): an access point found over SNMP
    role_type, role_why = {}, {}
    for k, h_ in demoted.items():
        if zp.ROLE.get(k):
            for m_ in {m for m, ip_ in ipmap.items() if ip_ in h_.get("_ips", ())} | zp.IFMACS.get(k, set()):
                role_type[m_], role_why[m_] = zp.ROLE[k][0], f"its SNMP: {zp.ROLE[k][1]}"
    # ... and what the switches' LLDP says is plugged in: a neighbour announcing "WLAN access point", from any vendor,
    # also when it is not in Zabbix
    for s_ in sw.values():
        for p_ in s_["ports"]:
            for nb, _peer in p_.get("lldp", ()):
                said = ("announces itself as a WLAN access point" if zp.lldp_says_ap(nb.get("caps")) else
                        (lambda m: m and f"describes itself as {m!r}")(zp.ap_model(f"{nb.get('sysdesc', '')} {nb.get('sysname', '')}")))
                if said:
                    m_ = zp.mac_of(nb.get("chassis", "")) or next((m for m, ip_ in ipmap.items() if ip_ == nb.get("mgmt_ip")), None)
                    if m_ and m_ not in role_type:
                        role_type[m_], role_why[m_] = "ap", f"LLDP of {s_['host']['name']}: {said}"
    dns = zp.rdns_all(sorted(set(ipmap.values())))
    gw_ip = zp.default_gateway()
    gw_mac = next((m for m, v in arp.items() if v[0] == gw_ip), None)
    try:
        guests = pve_guests()
    except Exception as e:  # noqa: BLE001
        say(f"  ! Proxmox API: {e}")
        guests = []
    guest_by_mac = {m: g for g in guests for m in g["macs"]}
    try:
        public_ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception:  # noqa: BLE001
        public_ip = ""

    # ---------------- infrastructure: switch-to-switch and switch-to-host LLDP links
    uplink, sw_link, host_port = set(), {}, {}
    for sid, s in sw.items():
        for p in s["ports"]:
            for nb, peer in p["lldp"]:
                if peer and peer["hostid"] in sw:
                    uplink.add((sid, p["name"]))
                    sw_link[(sid, peer["hostid"])] = (p["name"], nb["remote_port"])
                elif peer:
                    host_port[(sid, p["name"])] = peer

    # ---------------- a cable is a cable, whichever end reports it: a switch that does not tell its LLDP neighbours
    # (some TP-Link models) is still linked to the switches that see it. The tree grows from the core outwards, so a
    # link known only from the far end must count in both directions.
    for (a, b), (pa, pb) in list(sw_link.items()):
        if (b, a) not in sw_link:
            mine = next((p["name"] for p in sw[b]["ports"] if pb and pb in (p["name"], p.get("alias"), p.get("descr"))), pb or "")
            sw_link[(b, a)] = (mine, pa)
            if mine in {p["name"] for p in sw[b]["ports"]}:
                uplink.add((b, mine))
            say(f"  {sw[a]['host']['name']} reports {sw[b]['host']['name']} by LLDP; {sw[b]['host']['name']} does not report it back - linked both ways")

    # ---------------- switches without LLDP: switch B's own MAC on a port of switch A that does not lead up to the
    # gateway means B sits behind that port (the closest such port: the one with the fewest MACs)
    mac_links = set()
    if S["topo_fdb"] and gw_mac:
        ip2mac = {}
        for m_, ip_ in ipmap.items():
            ip2mac.setdefault(ip_, set()).add(m_)
        own = {}
        for k, s_ in sw.items():
            ms = set().union(*[ip2mac.get(ip_, set()) for ip_ in s_["host"]["_ips"]]) if s_["host"]["_ips"] else set()
            if zp.BRIDGE.get(k, {}).get("mac"):
                ms.add(zp.BRIDGE[k]["mac"])
            ms |= zp.IFMACS.get(k, set())                    # its interfaces' MACs: often what the others learn
            own[k] = ms
        core = core_switch_id(sw, S["core_switch"]) or min(
            ((k, len(p["macs"])) for k, s_ in sw.items() for p in s_["ports"] if gw_mac in p["macs"]),
            key=lambda x: x[1], default=(None, 0))[0]
        for a, pa, b, pb in mac_switch_links({k: s_["ports"] for k, s_ in sw.items()}, own, gw_mac, set(sw_link), core):
            sw_link[(a, b)] = (pa, pb)
            mac_links.add((a, b))
            uplink.add((a, pa))
            if pb:
                uplink.add((b, pb))
            say(f"  {sw[b]['host']['name']} is behind {sw[a]['host']['name']} {pa} (MAC table)")
        linked = {x for pair in sw_link for x in pair}
        for k, s_ in sw.items():                             # say why a switch stays unlinked, for whoever reads the log
            if k == core or k in linked:
                continue
            seen = [f"{sw[a]['host']['name']} {p['name']}" for a, t in sw.items() if a != k for p in t["ports"] if own[k] & set(p["macs"])]
            say(f"  {s_['host']['name']}: no link found - "
                + ("its own MAC is unknown (no ARP entry, bridge or interface MAC)" if not own[k] else
                   f"its MAC is seen only on ports toward the gateway: {', '.join(seen[:4])}" if seen else
                   "its MAC is in no other switch's MAC table"))

    # ---------------- where is each MAC right now (access port with fewest MACs)
    seen_at = {}
    for sid, s in sw.items():
        for p in s["ports"]:
            if (sid, p["name"]) in uplink:
                continue
            for mac in p["macs"]:
                cur = seen_at.get(mac)
                if not cur or len(p["macs"]) < cur[2]:
                    seen_at[mac] = (sid, p["name"], len(p["macs"]))
    online = set(seen_at) | {m for m, v in arp.items() if v[1] in ONLINE_STATES}
    # a switch's own management address is not a device: the switch is already on the map as a switch
    sw_ips = {ip for s_ in sw.values() for ip in s_["host"]["_ips"]}
    switch_macs = {m for m, ip in ipmap.items() if ip in sw_ips}
    online -= switch_macs
    for m in switch_macs:
        seen_at.pop(m, None)
        sdev.pop(m, None)

    gw_loc = seen_at.get(gw_mac) if gw_mac else None
    root_sw = core_switch_id(sw, S["core_switch"]) or (gw_loc[0] if gw_loc else (next(iter(sw)) if sw else None))
    if root_sw and gw_mac and (not gw_loc or gw_loc[0] != root_sw):
        gw_loc = next(((root_sw, p["name"], len(p["macs"])) for p in sw[root_sw]["ports"] if gw_mac in p["macs"]), gw_loc)

    # ---------------- device records (memory)
    events = {"new": [], "changed": []}
    sw_name = {sid: s["host"]["name"] for sid, s in sw.items()}

    def loc_of(mac):
        if mac == gw_mac:
            return {"k": "gw"}
        if mac not in seen_at:
            return None
        sid, pname, _ = seen_at[mac]
        if gw_mac and seen_at.get(gw_mac, (None, None))[:2] == (sid, pname):
            return {"k": "behind", "of": gw_mac}
        hp = host_port.get((sid, pname))
        if hp:
            hmac = next((m for m in seen_at if ipmap.get(m) in hp["_ips"]), None)
            if hmac and hmac != mac:
                return {"k": "behind", "of": hmac}
        return {"k": "port", "sw": sid, "port": pname}

    def dname(mac):                      # defined before loc_text, which uses it
        d, c, g = sdev.get(mac, {}), dcfg.get(mac, {}), guest_by_mac.get(mac)
        h = host_by_ip.get(d.get("ip", ""))
        return (c.get("name") or (g and g["name"]) or (h and h["name"])
                or (d.get("dns", "").split(".")[0] if d.get("dns") else "") or d.get("ip") or mac)

    def loc_text(loc):
        if not loc:
            return "unknown"
        if loc["k"] == "gw":
            return "gateway"
        if loc["k"] == "behind":
            return "Wi-Fi / gateway LAN" if loc["of"] == gw_mac else "via " + dname(loc["of"])
        return f"{sw_name.get(loc['sw'], '?')} {plabel(loc['sw'], loc['port'])}"

    # private-MAC phones: same hostname, new random MAC -> same device
    for mac in list(online):
        if mac in sdev or not is_private_mac(mac):
            continue
        name = (dns.get(ipmap.get(mac, ""), "") or "").split(".")[0]
        old = next((m for m, d in sdev.items() if is_private_mac(m) and m not in online and name
                    and d.get("dns", "").split(".")[0] == name), None)
        if old:
            sdev[mac] = sdev.pop(old)

    t = now()
    for mac in online:
        ip = ipmap.get(mac, "")
        d = sdev.get(mac)
        loc = loc_of(mac)
        if d is None:
            d = sdev[mac] = {"first_seen": t}
            if baseline:
                d["new_at"] = t
        if d.get("ip") and ip and d["ip"] != ip:
            d["ip_prev"], d["changed_at"] = d["ip"], t
        if loc and d.get("loc") and loc != d["loc"] and baseline:
            d["loc_prev"], d["moved_at"] = loc_text(d["loc"]), t
        d.update({"ip": ip or d.get("ip", ""), "dns": dns.get(ip, "") or d.get("dns", ""), "last_seen": t})
        if ip:
            d["ip_at"] = t
        if loc:
            d["loc"] = loc
    for mac in [m for m, d in sdev.items() if t - d.get("last_seen", t) > forget_s and m not in dcfg]:
        del sdev[mac]

    # ---------------- IP conflicts: two devices on the switches using the same IP within 10 minutes
    by_ip = {}
    for mac in seen_at:
        d = sdev.get(mac, {})
        if d.get("ip") and t - d.get("ip_at", 0) <= 600:
            by_ip.setdefault(d["ip"], []).append(mac)
    conflict_ips = {ip: macs for ip, macs in by_ip.items() if len(macs) > 1}
    conflict_macs = {m for macs in conflict_ips.values() for m in macs}

    # ---------------- port speed drops (e.g. 1 Gbps -> 100 Mbps on the same device = cable/connector)
    pspeed, downgrade = state.setdefault("port_speed", {}), {}

    def spd_mbps(txt):
        m = re.match(r"([\d.]+)\s*(G|M)", txt or "")
        return float(m.group(1)) * (1000 if m.group(2) == "G" else 1) if m else 0
    for sid, s_ in sw.items():
        for p in s_["ports"]:
            v = spd_mbps(p["speed"])
            if p["status"] != "up" or not v:
                continue
            if (sid, p["name"]) in uplink:
                who = "uplink"
            elif len(p["macs"]) == 1:
                who = p["macs"][0]
            else:
                continue
            key, rec = f"{sid}:{p['name']}", pspeed.get(f"{sid}:{p['name']}")
            if not rec or rec.get("who") != who:
                pspeed[key] = {"max": v, "who": who}
            elif v > rec["max"]:
                rec["max"] = v
            elif v < rec["max"]:
                fmt = lambda x: f"{x / 1000:g} Gbps" if x >= 1000 else f"{x:g} Mbps"  # noqa: E731
                downgrade[key] = f"running at {fmt(v)}, normally {fmt(rec['max'])}"

    # ---------------- build nodes
    nodes, by_mac = {}, {}

    def add(nid, parent, **kw):
        nodes[nid] = {"id": nid, "parent": parent, "flags": [], "details": [], **kw}
        return nodes[nid]

    add("inet", None, name="Internet", sub=public_ip or "public IP", icon="world", kind="inet",
        status="up" if public_ip else "down", details=[["Public IP", public_ip or "unknown"]])

    def dtype(mac):
        """A device's type, the same way device_node() decides it."""
        d, c, g = sdev.get(mac, {}), dcfg.get(mac, {}), guest_by_mac.get(mac)
        return c.get("type") or role_type.get(mac) or (g and g["kind"]) or classify(f"{dname(mac)} {d.get('dns', '')}", vendor_of(mac), mac,
                                                              S["device_types_off"])

    def device_node(mac, parent, port_ref=None, port_lbl="", via=""):
        d, c, g = sdev.get(mac, {}), dcfg.get(mac, {}), guest_by_mac.get(mac)
        ip, name = d.get("ip", ""), dname(mac)
        vendor = vendor_of(mac)
        typ = c.get("type") or role_type.get(mac) or (g and g["kind"]) or classify(f"{name} {d.get('dns', '')}", vendor, mac, S["device_types_off"])
        h = host_by_ip.get(ip)
        is_on = mac in online
        if g and g["status"] and not is_on:
            st = "off"
        elif is_on:
            st = "up"
        elif c.get("important"):
            st = "down"
        else:
            st = "off"
        flags = []
        if d.get("new_at") and t - d["new_at"] < flag_s:
            flags.append("new")
        if d.get("moved_at") and t - d["moved_at"] < flag_s:
            flags.append("moved")
        if d.get("changed_at") and t - d["changed_at"] < flag_s:
            flags.append("ip changed")
        approved = mac in dcfg or g is not None or h is not None
        if not approved:
            flags.append("unknown")
        if c.get("approved"):                      # approved from the map: neither unknown nor new any more
            flags = [f for f in flags if f not in ("new", "unknown")]
        stale_s = S["stale_days"] * 86400
        if not is_on and d.get("last_seen") and t - d["last_seen"] > stale_s:
            flags.append("stale")                  # not seen for days; forgotten after "forget_after_days"
        if c.get("important"):
            flags.append("important")
        if mac in conflict_macs:
            flags.append("ip conflict")
        conn = ""
        if port_ref:
            pinfo = next((p for p in sw[port_ref[0]]["ports"] if p["pid"] == port_ref[1]), None)
            conn = f"{sw_name[port_ref[0]]} · {pinfo['label'] if pinfo else port_lbl}" + \
                   (f" ({pinfo['name']}) · {pinfo['speed']}" if pinfo and pinfo["speed"] else "")
        elif parent in nodes:
            conn = "via " + nodes[parent]["name"]
        det = [["IP", ip or "-"], ["MAC", mac], ["Manufacturer", vendor or "-"], ["Type", typ],
               ["Connection", conn or "-"]]
        if via:
            det.append(["Link from", VIA_TEXT.get(via, via)])
        if v6_of.get(mac):
            det.append(["IPv6", ", ".join(sorted(set(v6_of[mac]))[:3])])
        if ip and zp.TTL.get(ip):
            t_ = zp.TTL[ip]
            base = min((b for b in (64, 128, 255) if b >= t_), default=255)
            det.append(["TTL", f"{t_} · {base - t_} hop{'' if base - t_ == 1 else 's'} away · "
                               + {64: "Linux, macOS, Android, iOS", 128: "Windows", 255: "network device"}[base]])
        pp_ = next((q for q in sw[port_ref[0]]["ports"] if q["pid"] == port_ref[1]), None) if port_ref else None
        if pp_ and (pp_.get("poe") or {}).get("on"):
            det.append(["Power", "PoE" + (f", class {pp_['poe']['class']}" if pp_["poe"].get("class") is not None else "")
                                 + " - often an access point, camera or phone"])
        if mac_vlan.get(mac):
            det.append(["VLAN", f"{mac_vlan[mac]} · {vlan_names.get(mac_vlan[mac], 'VLAN ' + str(mac_vlan[mac]))}"])
        if g:
            det.append(["Proxmox", f"{g['kind'].upper()} {g['vmid']} on {g['node']} · {g['status']}"])
        if d.get("loc_prev") and "moved" in flags:
            det.append(["Moved from", d["loc_prev"]])
        if d.get("ip_prev") and "ip changed" in flags:
            det.append(["Previous IP", d["ip_prev"]])
        det += [["First seen", iso(d.get("first_seen"))], ["Last seen", "now" if is_on else iso(d.get("last_seen"))]]
        sub = ("." + ip.split(".")[-1]) if ip and is_on else ("asleep" if st == "asleep" else
                                                              ("last seen " + iso(d.get("last_seen")) if d.get("last_seen") else "never seen"))
        n = add("d-" + mac.replace(":", ""), parent, name=name, sub=sub, icon=TYPE_ICON.get(typ, "help-circle"),
                logo=c.get("logo") or logo_for(name, webd), kind="device", type=typ, status=st, flags=flags,
                details=det, ip=ip, mac=mac, url=web_url(name, ip, c.get("url", "")), zbx=h["hostid"] if h else "",
                port=port_lbl, port_ref=port_ref, vendor=vendor, conn=conn or "-", vlan=mac_vlan.get(mac), via=via,
                link="fiber" if port_ref and (next((p for p in sw[port_ref[0]]["ports"] if p["pid"] == port_ref[1]), None)
                                               or {}).get("media") else "",
                first=d.get("first_seen", 0), last=t if is_on else d.get("last_seen", 0))
        by_mac[mac] = n["id"]
        return n

    # gateway + root switch
    if gw_mac:
        g = device_node(gw_mac, "inet")
        g["kind"] = "router"
    gw_id = by_mac.get(gw_mac, "inet")

    # a router before the gateway (double NAT, e.g. a provider's router in front of a firewall): the second hop to
    # the internet, when it is a private address
    if S["detect_upstream"] and gw_id != "inet":
        up_ip = upstream_router(gw_ip)
        if up_ip:
            zh = next((h for h in hosts if up_ip in (h.get("_ips") or ())), None)
            site = (cfg.get("settings", {}).get("site_topology") or {})
            up_name = (zh and zh["name"]) or site.get("upstream_name") or dns.get(up_ip) or "Upstream router"
            add("up-router", "inet", name=up_name, sub=up_ip, icon="router", kind="upstream", status="up", ip=up_ip,
                zbx=zh["hostid"] if zh else "", via="traceroute",
                details=[["IP", up_ip], ["Link from", "the second hop to the internet: a router before the gateway (double NAT)"]])
            nodes[gw_id]["parent"] = "up-router"
            say(f"Upstream router before the gateway: {up_name} ({up_ip})")

    # switches in BFS order from the root switch
    # ---- evidence for each link between switches: LLDP/CDP (above), STP, LACP, and your confirmations
    link_ev = {k: ["MAC table" if k in mac_links else "LLDP/CDP"] for k in sw_link}
    bmac = {zp.BRIDGE[k]["mac"]: k for k in sw if zp.BRIDGE.get(k, {}).get("mac")}
    redundant = []

    def port_by_if(k, ifx):
        return next((q for q in sw[k]["ports"] if str(q.get("ifindex")) == str(ifx)), None)

    def add_link(a, b, pa, pb, src):
        if (a, b) not in sw_link:
            sw_link[(a, b)] = (pa, pb)
        if src not in link_ev.setdefault((a, b), []):
            link_ev[(a, b)].append(src)
    for a in sw:
        for p in sw[a]["ports"]:
            st = p.get("stp") or {}
            b = bmac.get(st.get("dbridge"))
            if b and b != a:                                    # the far end of this cable is switch b
                rb = port_by_if(b, zp.BRIDGE[b]["bport"].get(st.get("dport"), ""))
                pb = rb["name"] if rb else ""
                if st.get("state") == "blocking":               # a real cable that carries no traffic: redundant
                    redundant.append((a, p["name"], b, pb))
                else:
                    add_link(a, b, p["name"], pb, "STP")
                    if pb:
                        add_link(b, a, pb, p["name"], "STP")
            lb = bmac.get(p.get("lacp_partner"))
            if lb and lb != a:
                add_link(a, lb, p["name"], "", "LACP")
    by_name = {v: k for k, v in sw_name.items()}
    for L in cfg.get("links", []) or []:
        if L.get("state") != "confirmed":
            continue
        a, b = by_name.get((L.get("a") or [None])[0]), by_name.get((L.get("b") or [None])[0])
        if a in sw and b in sw and a != b:
            pa = next((q["name"] for q in sw[a]["ports"] if q.get("base", q["label"]) == L["a"][1]), "")
            pb = next((q["name"] for q in sw[b]["ports"] if q.get("base", q["label"]) == L["b"][1]), "")
            sw_link.pop((a, b), None)                           # your confirmation outranks everything else
            sw_link.pop((b, a), None)
            add_link(a, b, pa, pb, "confirmed")
            add_link(b, a, pb, pa, "confirmed")

    order, parent_of = [], {}
    if root_sw:
        queue, seen = [root_sw], {root_sw}
        while queue:
            cur = queue.pop(0)
            order.append(cur)
            for (a, b), (pa, pb) in sorted(sw_link.items()):
                if a == cur and b not in seen:
                    seen.add(b)
                    parent_of[b] = (cur, pa, pb)
                    queue.append(b)
        order += [s for s in sw if s not in seen]
    for b_, (a_, pa_, pb_) in parent_of.items():
        x, y = _pinfo.get((a_, pa_), {}), _pinfo.get((b_, pb_), {})
        if x.get("speed") and y.get("speed") and x["speed"] != y["speed"]:
            report["warnings"].append(f"Speed mismatch: {sw_name[a_]} {plabel(a_, pa_)} at {x['speed']}, "
                                      f"{sw_name[b_]} {plabel(b_, pb_)} at {y['speed']}")
    for k_ in sw:
        for p in sw[k_]["ports"]:
            if p.get("status") == "up" and p.get("duplex") == "half" and re.search(r"(\d+) ?([MG])", p.get("speed", "")):
                report["warnings"].append(f"Half duplex: {sw_name[k_]} {plabel(k_, p['name'])} ({p['speed']}) - "
                                          "usually a duplex mismatch with the device on the other end")
    for sid in order:
        h = sw[sid]["host"]
        up = sum(p["status"] == "up" for p in sw[sid]["ports"])
        if sid in parent_of:
            par, pa, pb = parent_of[sid]
            rb = next((p for p in sw[sid]["ports"] if zp.norm(p["name"]) == zp.norm(pb)), None)
            parent, lbl = "s-" + par, f"{plabel(par, pa)}" + (f" ↔ {rb['label'].replace('Port ', '') if rb else pb}" if (rb or pb) else "")
            pref = [par, pid_of(par, pa)]
            # a link aggregation: name the LAG, and draw the link as one thicker link
            mine = _pinfo.get((par, pa), {})
            agg = next((q for q in sw[par]["ports"] if mine.get("lag") and str(q.get("ifindex")) == str(mine["lag"])), None) \
                or (mine if mine.get("kind") == "lag" else None)
            ev_ = link_ev.get((par, sid)) or link_ev.get((sid, par)) or []
            link_via = ("manual" if "confirmed" in ev_ else "lldp" if "LLDP/CDP" in ev_ else "stp" if "STP" in ev_ else "lacp" if "LACP" in ev_
                        else "mac" if "MAC table" in ev_ else "lldp")
            if agg:
                members = [q["label"].replace("Port ", "") for q in sw[par]["ports"] if str(q.get("lag")) == str(agg.get("ifindex"))]
                lbl = f"{agg['label']}" + (f" ({'+'.join(members)})" if members else "") + f" ↔ {rb['label'].replace('Port ', '') if rb else pb}"
                link_kind = "lag"
            else:
                link_kind = "fiber" if mine.get("media") else ""
        else:
            link_kind = "fiber" if gw_loc and sid == root_sw and _pinfo.get((sid, gw_loc[1]), {}).get("media") else ""
            link_via = "mac" if gw_loc and sid == root_sw else ""
            parent, lbl = gw_id, (plabel(sid, gw_loc[1]) if gw_loc and sid == root_sw else "")
            if sid != root_sw and root_sw and "s-" + root_sw in nodes:
                parent, link_via, lbl = "s-" + root_sw, "behind", ""    # no evidence: guessed, below the core switch
            pref = [sid, pid_of(sid, gw_loc[1])] if gw_loc and sid == root_sw else None
        add("s-" + sid, parent, name=h["name"], sub="." + h["_snmp"]["ip"].split(".")[-1], icon="server-2",
            kind="switch", sw=sid, status="up", port=lbl, port_ref=pref, zbx=sid, flip=sid not in parent_of, link=link_kind, via=link_via,
            evidence=(link_ev.get((parent_of[sid][0], sid)) or link_ev.get((sid, parent_of[sid][0])) or []) if sid in parent_of else [],
            link_ends=[[sw_name[parent_of[sid][0]], _pinfo.get((parent_of[sid][0], parent_of[sid][1]), {}).get("base") or parent_of[sid][1]],
                       [sw_name[sid], _pinfo.get((sid, parent_of[sid][2]), {}).get("base") or parent_of[sid][2]]]
            if sid in parent_of and parent_of[sid][2] else None,
            url=f"http://{h['_snmp']['ip']}",
            details=[["IP", h["_snmp"]["ip"]], ["Ports up", f"{up} of {len(sw[sid]['ports'])}"]])

    # Wi-Fi / gateway LAN group
    behind = {}
    for mac, d in sdev.items():
        loc = d.get("loc") or {}
        if loc.get("k") == "behind":
            behind.setdefault(loc["of"], []).append(mac)
    if gw_mac and behind.get(gw_mac):
        add("wifi", gw_id, name="Wi-Fi / LAN", sub=f"{len(behind[gw_mac])} clients", icon="wifi", kind="group",
            status="up", port="", details=[["Connection", "Wi-Fi and LAN ports of the gateway"]])

    # devices on switch ports (online, remembered, or pinned)
    placed = {gw_mac} if gw_mac else set()

    for n_ in nodes.values():                                 # say it in the panel: a switch placed without evidence
        if n_.get("kind") == "switch" and n_.get("via") == "behind":
            n_["details"].append(["Link from", "guessed: no LLDP, spanning tree, LACP or MAC table evidence - drawn below the "
                                               "core switch (confirm the real link in a panel, or set the core switch in Settings)"])

    def pin_loc(mac):
        pin = dcfg.get(mac, {}).get("pin", "")
        if ":" in pin:
            swn, pn = pin.rsplit(":", 1)
            sid = next((s for s in sw if sw_name[s] == swn), None)
            pn = pn.strip()
            p = next((p for p in sw[sid]["ports"] if p.get("base", p["label"]) == f"Port {pn}" or p["label"] == pn
                      or p["name"] == pn), None) if sid else None
            if p:
                return {"k": "port", "sw": sid, "port": p["name"]}
        return None

    places, done_places = cfg.get("places", {}) or {}, set()
    port_groups = {}
    for mac, d in sorted(sdev.items(), key=lambda x: x[0]):
        loc = (d.get("loc") if mac in online else None) or pin_loc(mac) or d.get("loc")
        if not loc or loc.get("k") != "port" or loc["sw"] not in sw:
            continue
        port_groups.setdefault((loc["sw"], loc["port"]), []).append(mac)
    for mac in dcfg:  # pinned but never seen
        if mac not in sdev and pin_loc(mac):
            loc = pin_loc(mac)
            port_groups.setdefault((loc["sw"], loc["port"]), []).append(mac)
    for (sid, pname), macs in sorted(port_groups.items(), key=lambda x: (x[0][0], pos_of(*x[0]))):
        pr, lbl = [sid, pid_of(sid, pname)], plabel(sid, pname)
        live_macs = [m for m in macs if m in online]
        pkey = f"{sw_name[sid]}|{_pinfo.get((sid, pname), {}).get('base') or lbl}"
        place = places.get(pkey)
        if place and place.get("kind") != "none":        # confirmed or named on the map (Edit)
            gid = f"g-{sid}-{pid_of(sid, pname)}"
            add(gid, "s-" + sid, name=place.get("name") or PLACE_KINDS[place["kind"]][0], icon=PLACE_KINDS[place["kind"]][1],
                sub=f"{lbl} · {len(live_macs)} device{'' if len(live_macs) == 1 else 's'} now", kind="group",
                type=place["kind"], confirmed=True, place=pkey, status="up", port=lbl, port_ref=pr, via="manual",
                details=[["Connection", f"{sw_name[sid]} · {lbl}"], ["Type", PLACE_KINDS[place["kind"]][0]]]
                + [[k_.title(), place[k_]] for k_ in ("model", "location") if place.get(k_)]
                + [["Link from", "confirmed on the map"]])
            for m in macs:
                device_node(m, gid, via="mac")
                placed.add(m)
            done_places.add(pkey)
            continue
        not_a_device = bool(place and place.get("kind") == "none")     # "Not a device" on the map: the plain group
        if len(live_macs) > 1 and (sid, pname) not in host_port:
            # several devices on one port: behind an access point / hypervisor that is one of them,
            # or behind a switch nobody manages (inferred: drawn dashed)
            hub = next((m for m in live_macs if dtype(m) == "ap"), None)   # not "unifi": a Cloud Key is no AP
            if not hub:                                      # a hypervisor only with its virtual machines behind it
                virt = [m for m in live_macs if is_virtual_mac(m)]
                hub = next((m for m in live_macs if dtype(m) == "hypervisor" and not is_virtual_mac(m)
                            and len(virt) * 2 >= len(live_macs) - 1 and virt), None)
            if hub and not not_a_device:
                hn = device_node(hub, "s-" + sid, pr, lbl, "mac")
                placed.add(hub)
                for m in macs:
                    if m != hub:
                        device_node(m, hn["id"], via="behind")
                        placed.add(m)
                continue
            gid = f"g-{sid}-{pid_of(sid, pname)}"
            if S["infer_switches"] and not not_a_device:
                add(gid, "s-" + sid, name="Unmanaged switch", sub=f"{lbl} · {len(macs)} devices", icon="server-2",
                    kind="group", inferred=True, place=pkey, status="up", port=lbl, port_ref=pr, via="mac",
                    details=[["Connection", f"{sw_name[sid]} · {lbl}"],
                             ["Why", f"{len(live_macs)} devices answer on this one port, without LLDP: probably a "
                                     "switch SpiderMap cannot read (or a hub, or a Wi-Fi bridge)"]])
            else:
                add(gid, "s-" + sid, name=f"{lbl} devices", sub=f"{len(macs)} devices", icon="sitemap", kind="group",
                    status="up", port=lbl, port_ref=pr, place=pkey if not_a_device else None,
                    details=[["Connection", f"{sw_name[sid]} · {lbl}"]])
            for m in macs:
                device_node(m, gid, via="behind" if S["infer_switches"] and not not_a_device else "mac")
                placed.add(m)
        else:
            for m in macs:
                seen_now = m in online and (sdev.get(m, {}).get("loc") or {}).get("port") == pname
                via = "lldp" if (sid, pname) in host_port else "mac" if seen_now else "manual" if pin_loc(m) else "remembered"
                n = device_node(m, "s-" + sid, pr, lbl, via)
                port = next((p for p in sw[sid]["ports"] if p["name"] == pname), None)
                if n["status"] == "off" and port and port["status"] == "up":
                    n["status"], n["sub"] = "asleep", "asleep"
                placed.add(m)

    for pkey, place in places.items():
        if pkey in done_places or place.get("kind") in (None, "none") or "|" not in pkey:
            continue
        swn, plab = pkey.split("|", 1)
        sid = next((k for k, v in sw_name.items() if v == swn), None)
        prt = next((p for p in sw[sid]["ports"] if p.get("base", p["label"]) == plab), None) if sid in sw else None
        if prt:
            add(f"g-{sid}-{prt['pid']}", "s-" + sid, name=place.get("name") or PLACE_KINDS[place["kind"]][0],
                icon=PLACE_KINDS[place["kind"]][1], sub=f"{prt['label']} · no devices now", kind="group", type=place["kind"],
                confirmed=True, place=pkey, status="up" if prt["status"] == "up" else "off", port=prt["label"],
                port_ref=[sid, prt["pid"]], via="manual", details=[["Connection", f"{swn} · {prt['label']}"]])

    # devices behind another device (gateway Wi-Fi, Proxmox guests, ...)
    for host_mac, macs in behind.items():
        parent = "wifi" if host_mac == gw_mac else by_mac.get(host_mac)
        if not parent:
            continue
        for m in sorted(macs, key=lambda x: dname(x).lower()):
            if m not in placed:
                device_node(m, parent, via="arp" if parent == "wifi" else "proxmox" if m in guest_by_mac else "behind")
                placed.add(m)

    # "Connected through" (Edit): a device placed behind another one, e.g. a media converter - a manual link
    for m, c in dcfg.items():
        target = str(c.get("behind") or "").lower()
        if not target or m not in by_mac or target not in by_mac or target == m:
            continue
        nid, tid = by_mac[m], by_mac[target]
        up, loop = tid, False
        while up:                                         # never behind something that is behind it
            if up == nid:
                loop = True
                break
            up = nodes.get(up, {}).get("parent")
        if loop:
            continue
        n = nodes[nid]
        n["parent"], n["via"], n["behind"] = tid, "manual", target
        n["details"] = [x for x in n["details"] if x[0] != "Link from"] + [["Link from", VIA_TEXT["manual"] + " (connected through "
                                                                            + nodes[tid]["name"] + ")"]]

    unifi_inv = {}
    # UniFi: access points, and every wireless client under its AP (its own credentials - not from Zabbix)
    if S["unifi_url"] and S["unifi_user"]:
        try:
            uf = unifi_fetch(S["unifi_url"], S["unifi_site"] or "default", S["unifi_user"],
                             load_secrets().get("unifi_password", ""), S["unifi_fingerprint"])
            aps = {str(d.get("mac", "")).lower(): d for d in uf["devices"] if d.get("type") == "uap"}
            for d in uf["devices"]:
                um = str(d.get("mac", "")).lower()
                if um in by_mac:
                    unifi_inv[by_mac[um]] = {"model": d.get("model", ""), "serial": d.get("serial", ""), "firmware": d.get("version", ""),
                                             "vendor": "Ubiquiti", "source": "UniFi controller"}
            moved = 0
            for amac, d in aps.items():
                if amac in by_mac:
                    n = nodes[by_mac[amac]]
                    n["type"], n["icon"] = "ap", TYPE_ICON["ap"]
                    if not dcfg.get(amac, {}).get("name") and d.get("name"):
                        n["name"] = d["name"]
                    n["details"] += [["Model", d.get("model", "-")], ["Wireless clients", str(d.get("num_sta", "-"))]]
            for c in uf["clients"]:
                cm, am = str(c.get("mac", "")).lower(), str(c.get("ap_mac", "")).lower()
                if c.get("is_wired") or cm not in by_mac or am not in by_mac or cm == am:
                    continue
                n, tid = nodes[by_mac[cm]], by_mac[am]
                up, loop = tid, False
                while up:
                    if up == n["id"]:
                        loop = True
                        break
                    up = nodes.get(up, {}).get("parent")
                if loop or n.get("via") == "manual":           # your "connected through" wins
                    continue
                n["parent"], n["via"], n["port"] = tid, "unifi", ""
                if not dcfg.get(cm, {}).get("name") and (c.get("name") or c.get("hostname")) and n["name"] in (cm, n.get("ip")):
                    n["name"] = c.get("name") or c.get("hostname")
                n["details"] = [x for x in n["details"] if x[0] != "Link from"] + [
                    ["Link from", VIA_TEXT["unifi"]], ["Wi-Fi", f"{c.get('essid', '-')} · {c.get('signal', '-')} dBm"]]
                moved += 1
            report["unifi"] = {"ok": True, "kind": uf["kind"], "aps": len(aps), "clients": moved}
        except Exception as e:  # noqa: BLE001
            report["unifi"] = {"ok": False, "error": str(e)[:200]}
            report["warnings"].append(f"UniFi controller: {str(e)[:150]}")

    # stopped Proxmox guests the network never saw
    pve_node = next((n for n in nodes.values() if n.get("type") in ("server", "hypervisor") and "proxmox" in n["name"].lower()), None)
    if pve_node:
        for g in guests:
            if not any(m in placed for m in g["macs"]):
                gid = "v-" + str(g["vmid"])
                add(gid, pve_node["id"], name=g["name"], sub=g["status"], icon=TYPE_ICON[g["kind"]],
                    logo=logo_for(g["name"], webd), kind="device", type=g["kind"],
                    status="up" if g["status"] == "running" else "off",
                    details=[["Proxmox", f"{g['kind'].upper()} {g['vmid']} on {g['node']}"], ["State", g["status"]],
                             ["MAC", ", ".join(g["macs"]) or "-"]])

    # known devices never placed
    rest = [m for m in set(dcfg) | set(sdev) if m not in placed and m != gw_mac and m not in switch_macs
            and m not in by_mac and not (sdev.get(m, {}).get("loc") or {}).get("k") == "behind"]
    if rest and root_sw:
        add("unplaced", "s-" + root_sw, name="Not placed yet", sub=f"{len(rest)} devices", icon="question-mark",
            kind="group", status="off", collapsed=True,
            details=[["Why", "Known devices not seen since tracking started. Pin them in devices.json."]])
        for m in sorted(rest):
            device_node(m, "unplaced")

    # switch port panels
    for n_ in nodes.values():                                 # why a device is an access point - in its panel
        if n_.get("kind") == "device" and n_.get("type") == "ap":
            m_ = n_.get("mac", "")
            why = (role_why.get(m_) or (dcfg.get(m_, {}).get("type") and "your choice on the map")
                   or (zp.ap_model(n_.get("name", ""), n_.get("vendor", "")) and
                       f"its name: an access point model ({zp.ap_model(n_.get('name', ''), n_.get('vendor', ''))})"))
            if why:
                n_["details"].append(["Type from", why])
    switches_out = {}
    for sid in order:
        clients = {}
        for n in nodes.values():
            if n.get("port_ref") and n["port_ref"][0] == sid and n["kind"] != "switch":
                clients.setdefault(n["port_ref"][1], []).append(n["name"])
        if gw_loc and gw_loc[0] == sid and gw_mac in by_mac:
            clients.setdefault(pid_of(sid, gw_loc[1]), []).insert(0, nodes[by_mac[gw_mac]]["name"] + " (gateway)")
        for (a, b), (pa, _pb) in sw_link.items():
            if a == sid:
                clients.setdefault(pid_of(sid, pa), []).append(sw_name[b])
        switches_out[sid] = {"name": sw_name[sid], "ports": [
            {"id": p["pid"], "n": p.get("pos", 0), "label": p["label"], "kind": p.get("kind", "port"),
             "media": p.get("media", ""), "base": p.get("base", p["label"]), "duplex": p.get("duplex"),
             "stp": (p.get("stp") or {}).get("state"), "poe": p.get("poe"), "vlans": p.get("vlans") if S["topo_vlans"] else None,
             "lag": next((q["pid"] for q in sw[sid]["ports"] if str(q.get("ifindex")) == str(p.get("lag"))), None),
             "full": p["name"], "status": p["status"], "speed_mbps": p.get("speed_mbps", 0),
             "speed": p["speed"], "alias": p["alias"], "ifindex": p.get("ifindex"),
             "downgrade": downgrade.get(f"{sid}:{p['name']}", ""),
             "clients": clients.get(p["pid"], [])}
            for p in sorted(sw[sid]["ports"], key=lambda p: (p.get("kind") == "lag", _port_unit(p["name"]) or 0, p.get("pos", 0)))]}

    # ---------------- events for Zabbix
    for n in nodes.values():
        if n.get("mac"):
            where = n["details"][4][1] if len(n["details"]) > 4 else ""
            if "new" in n["flags"]:
                ident = n["ip"] or n["mac"]
                events["new"].append((n["name"] if n["name"] == ident else f"{n['name']} ({ident})") + f" {where}")
            if "moved" in n["flags"] or "ip changed" in n["flags"]:
                events["changed"].append(f"{n['name']} ({', '.join(f for f in n['flags'] if f in ('moved', 'ip changed'))})")
    down = [n["name"] for n in nodes.values() if n["status"] == "down"]

    # ---------------- activity feed
    evs = []

    def nid(mac):
        return by_mac.get(mac)
    for mac, d in sdev.items():
        loc, wired = d.get("loc") or {}, (d.get("loc") or {}).get("k") == "port"
        if d.get("new_at") == t:
            if mac not in dcfg and wired:
                who = dname(mac) if dname(mac) in (mac, d.get("ip")) else f"{dname(mac)} ({d.get('ip') or mac})"
                evs.append(ev("security", "warn", f"Unknown device plugged into {loc_text(loc)}: {who}" + ("" if who == mac else f" [{mac}]"), nid(mac)))
            else:
                ident = d.get("ip") or mac
                who = dname(mac) if dname(mac) == ident else f"{dname(mac)} ({ident})"
                evs.append(ev("device", "info", f"New device: {who} on {loc_text(loc)}", nid(mac)))
        if d.get("moved_at") == t:
            evs.append(ev("security" if wired else "device", "warn" if wired else "info",
                          f"{dname(mac)} moved from {d.get('loc_prev')} to {loc_text(loc)}", nid(mac)))
        if d.get("changed_at") == t:
            evs.append(ev("ip", "info", f"{dname(mac)} changed IP from {d.get('ip_prev')} to {d.get('ip')}", nid(mac)))
    old_ip = state.get("public_ip")
    if public_ip and old_ip and public_ip != old_ip:
        evs.append(ev("internet", "warn", f"Public IP changed from {old_ip} to {public_ip}", "inet"))
        state.setdefault("public_ip_history", []).append([t, old_ip, public_ip])
        state["public_ip_history"] = state["public_ip_history"][-20:]
    if public_ip:
        state["public_ip"] = public_ip
    conflicts = [{"ip": ip, "macs": macs, "names": [dname(m) for m in macs]} for ip, macs in conflict_ips.items()]
    for c_ in conflicts:
        if c_["ip"] not in state.get("conflicts", []):
            evs.append(ev("security", "bad", f"IP conflict: {c_['ip']} is used by {' and '.join(c_['names'])}", nid(c_["macs"][0])))
    for ip in state.get("conflicts", []):
        if ip not in conflict_ips:
            evs.append(ev("ip", "ok", f"IP conflict on {ip} resolved"))
    state["conflicts"] = list(conflict_ips)
    if baseline:
        add_events(evs)

    # ---------------- presence: online intervals per device (5-minute scans, gaps under 15 min joined)
    pres = load_json(PRESENCE_FILE, {})
    for mac in online:
        iv = pres.setdefault(mac, [])
        if iv and t - iv[-1][1] <= 900:
            iv[-1][1] = t
        else:
            iv.append([t, t])
    for mac in list(pres):
        pres[mac] = [x for x in pres[mac] if x[1] >= t - setting("presence_keep_days") * 86400]
        if not pres[mac]:
            del pres[mac]
    save_json(PRESENCE_FILE, pres)
    if webd:
        save_json(os.path.join(DATA_DIR, "spidermap_presence.json"),
                  {"t": t, "devices": {m: [x for x in iv if x[1] >= t - 8 * 86400] for m, iv in pres.items()}})

    state["baseline"] = state.get("baseline") or t
    save_json(STATE_FILE, state)
    try:
        with open(os.path.join(HERE, PAGE), encoding="utf-8") as f:
            pv = (re.search(r'PAGE_VERSION = "([^"]+)"', f.read()) or [None, ""])[1]
    except FileNotFoundError:
        pv = ""
    data = {"generated": t, "generated_text": iso(t), "page_version": pv, "conflicts": conflicts,
            "public_ip_history": state.get("public_ip_history", []), "scan_seconds": now() - t0, "nodes": list(nodes.values()),
            "switches": switches_out, "zabbix_url": "zabbix.php",
            "partial": report["partial"], "scan_warnings": report["warnings"],
            "extra_links": [{"a": "s-" + a, "b": "s-" + b, "kind": "redundant",
                             "label": f"{sw_name[a]} {plabel(a, pa)} ↔ {sw_name[b]} {plabel(b, pb) if pb else ''}".strip()
                             + " · blocked by spanning tree"} for a, pa, b, pb in redundant
                            if "s-" + a in nodes and "s-" + b in nodes],
            "inventory": build_inventory(nodes, sw, unifi_inv),
            "vlans": {str(v): {"name": vlan_names.get(v, f"VLAN {v}"),
                               "devices": sum(1 for n in nodes.values() if n.get("vlan") == v)}
                      for v in sorted(set(vlan_names) | {n["vlan"] for n in nodes.values() if n.get("vlan")})}}
    scan_progress(95, "saving")
    install_data_page()                   # a new page version reaches the browsers; the web folder is install.sh's job
    save_json(os.path.join(DATA_DIR, DATA), data)
    report.update(finished=now(), nodes=len(nodes), online=len(online), new=len(events["new"]),
                  changed=len(events["changed"]), down=len(down), targets=len(targets) if targets else 0)
    save_json(SCAN_REPORT, report)
    say(f"{len(nodes)} nodes, {len(online)} devices online, {len(events['new'])} new, "
        f"{len(events['changed'])} changed, {len(down)} important down")
    if not baseline:
        say("First run: everything seen now is the baseline, so nothing is flagged as new.")
    return events, down, hosts


# --------------------------------------------------------------------------- #
# Live refresh (every minute): port status + traffic from Zabbix, ping important devices
# --------------------------------------------------------------------------- #
EDITS_DIR = "/var/lib/spidermap"                         # root only
AUDIT_FILE = os.path.join(EDITS_DIR, "audit.log")     # who changed what from the map, one JSON line each
INBOX = os.path.join(EDITS_DIR, "inbox")                  # the only place the web server may write
EDITS_FILE = os.path.join(INBOX, "edits.json")
HIST_FILE = os.path.join(STATE_DIR, "spidermap_hist.json")
HIST_LEN = 90                                     # minutes of history kept for the small graphs
LAT_TARGETS = [("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8"), ("Quad9", "9.9.9.9")]
GUEST_RX = r"(?i)(qemu|lxc|vm)\s*\["
STAT_RULES = [("cpu", "CPU", r"(?i)\bcpu\b.*(util|usage)"),
              ("mem", "Memory", r"(?i)memory.*(util|usage|used)"),
              ("disk", "Disk", r"(?i)(space utilization|root ?fs|root filesystem|filesystem|disk|storage).*(util|usage|used)"),
              ("temp", "Temperature", r"(?i)temperature"),
              ("uptime", "Uptime", r"(?i)\buptime\b")]


def apply_edits():
    """Merge edits saved from the page (spidermap.php) into devices.json.
    edits.json is read and cleared under the same lock spidermap.php uses when it writes,
    so an edit saved at that very moment is never lost."""
    import fcntl
    try:
        fh = open(EDITS_FILE, "r+", encoding="utf-8")
    except FileNotFoundError:
        return {}
    with fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            edits = json.loads(fh.read() or "{}")
        except ValueError:
            edits = {}
        if not isinstance(edits, dict) or not edits:
            return {}
        merged = _merge_edits(edits)
        fh.seek(0)
        fh.truncate()            # in place: keeps the web server's ownership
        fh.write("{}")
        fh.flush()
    return merged


AUDIT_FIELDS = ("name", "type", "url", "pin", "important", "approved", "behind")


def _merge_edits(edits):
    cfg = load_json(DEVICES_FILE, DEFAULT_DEVICES)
    devs = cfg.setdefault("devices", {})
    evs = []
    for mac, e in edits.items():
        d = devs.setdefault(mac.lower(), {})
        before = {k: d.get(k) for k in AUDIT_FIELDS}
        for k, v in e.items():
            if k in ("by", "at"):
                continue
            if v in ("", None, False) and k not in ("important", "approved"):
                d.pop(k, None)
            else:
                d[k] = v
        for k_ in ("important", "approved"):
            if d.get(k_) is False:
                d.pop(k_)
        changes = {k: [before[k], d.get(k)] for k in AUDIT_FIELDS if (before[k] or None) != (d.get(k) or None)}
        if changes:
            by = e.get("by") or "?"
            audit("edit", by, {"mac": mac.lower(), "changes": changes})
            what = ", ".join(f"{k} “{old or '-'}” → “{new or '-'}”" if k not in ("important", "approved") else
                             ("marked important" if new else "no longer important") if k == "important" else
                             ("approved" if new else "approval removed") for k, (old, new) in changes.items())
            evs.append(ev("audit", "info", f"{by} changed {d.get('name') or before['name'] or mac}: {what}",
                          "d-" + mac.lower().replace(":", "")))
    save_json(DEVICES_FILE, cfg)
    if evs:
        add_events(evs)
    return {m.lower(): devs[m.lower()] for m in edits}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bytes(v):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or u == "TB":
            return f"{v:.0f} {u}" if u == "B" else f"{v:.1f} {u}"
        v /= 1024


def _dur(sec):
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h" if d else f"{h}h {r // 60}m"


def pick_stats(items):
    out = {}
    for key, label, rx in STAT_RULES:
        cands = [i for i in items if re.search(rx, i["name"]) and _f(i["lastvalue"]) is not None]
        if not cands:
            continue
        if key in ("cpu", "mem", "disk"):
            pct = [i for i in cands if i["units"] == "%"]
            if pct:
                v = _f(pct[0]["lastvalue"])
                out[key] = {"label": label, "pct": round(v, 1), "text": f"{v:.1f} %"}
                continue
            used = next((i for i in cands if i["units"] == "B" and re.search(r"(?i)used", i["name"])), None)
            if used:
                tname = re.sub(r"(?i)used", "total", used["name"])
                tot = next((i for i in items if i["name"].lower() == tname.lower()), None)
                u, t = _f(used["lastvalue"]), _f(tot["lastvalue"]) if tot else None
                if t:
                    out[key] = {"label": label, "pct": round(100 * u / t, 1),
                                "text": f"{_bytes(u)} of {_bytes(t)}"}
        elif key == "uptime":
            out[key] = {"label": label, "text": _dur(_f(cands[0]["lastvalue"]))}
        elif key == "temp":
            out[key] = {"label": label, "text": f"{_f(cands[0]['lastvalue']):.0f} °C"}
    return out


def ping_avg(ip, count=2):
    r = ping_stats(ip, count)
    return r["ms"] if r and r["loss"] < 100 else None


# --------------------------------------------------------------------------- #
# The router's own WAN counters over UPnP (WANCommonInterfaceConfig): the real internet traffic, including Wi-Fi
# devices that never pass a switch. Only the default gateway is trusted, not any device that answers.
# --------------------------------------------------------------------------- #


def ping_stats(ip, count=4):
    """avg latency, packet loss % and jitter (mdev) from a short burst of pings"""
    try:
        r = subprocess.run(["ping", "-n", "-c", str(count), "-i", "0.2", "-W", "1", ip],
                           capture_output=True, text=True, timeout=count + 4)
    except Exception:  # noqa: BLE001
        return {"ms": None, "loss": 100.0, "jit": None}
    m = re.search(r"(\d+) packets transmitted, (\d+) received", r.stdout)
    loss = 100.0 * (1 - int(m.group(2)) / max(1, int(m.group(1)))) if m else 100.0
    q = re.search(r"= [\d.]+/([\d.]+)/[\d.]+/([\d.]+)", r.stdout)
    return {"ms": round(float(q.group(1)), 1) if q else None, "loss": round(loss, 1),
            "jit": round(float(q.group(2)), 1) if q else None}


def zabbix_problems(api, hostids):
    """{hostid: {"sev": worst severity 0-5, "list": [[name, severity, since], ...]}} for triggers in problem state.
    SpiderMap's own triggers (new devices etc.) are left out: they are notifications, not problems of the host."""
    hostids = sorted(h for h in hostids if h)
    if not hostids:
        return {}
    trig = api.call("trigger.get", {"hostids": hostids, "filter": {"value": 1}, "monitored": True, "skipDependent": True,
                                    "expandDescription": True, "output": ["description", "priority", "lastchange"],
                                    "selectHosts": ["hostid"], "selectTags": "extend", "sortfield": "priority",
                                    "sortorder": "DESC"})
    out = {}
    for tr in trig:
        if any(tg.get("tag") == "source" and tg.get("value") == "autotopo" for tg in tr.get("tags", [])):
            continue
        for h in tr.get("hosts", []):
            e = out.setdefault(h["hostid"], {"sev": 0, "list": []})
            e["sev"] = max(e["sev"], int(tr["priority"]))
            if len(e["list"]) < 8:
                e["list"].append([tr["description"][:160], int(tr["priority"]), int(tr.get("lastchange") or 0)])
    return out


ZBX_PORT_KEYS = [("net.if.status[", "st"), ("net.if.in.errors[", "ein"), ("net.if.out.errors[", "eout"),
                 ("net.if.in[", "rx"), ("net.if.out[", "tx")]


def zabbix_port_item(key, name, by_name):
    """Which port and which value a Zabbix interface item is: (kind, ifIndex) or (None, None).
    Standard SNMP templates put the ifIndex in the key: net.if.in[ifHCInOctets.<ifIndex>].
    Fallback: the interface name in the item name ("Interface <name>(<alias>): ..."), names may contain spaces."""
    kind = next((k for prefix, k in ZBX_PORT_KEYS if key.startswith(prefix)), None)
    if not kind:
        return None, None
    m = re.search(r"\.(\d+)\]$", key)
    if m:
        return kind, m.group(1)
    m = re.match(r"^Interface (.+?)\(", name or "")
    return (kind, by_name.get(m.group(1).strip())) if m else (None, None)


def live_refresh(api):
    webd = webdir()
    data = load_json(os.path.join(DATA_DIR, DATA), None)
    if not data:
        raise SystemExit("No spidermap.json yet - run a full scan first.")
    t = now()
    by_id = {n["id"]: n for n in data["nodes"]}
    events = []

    # edits from the page -> devices.json, and patch the current map right away
    changed = apply_edits()
    if changed:
        for n in data["nodes"]:
            c = changed.get(n.get("mac", ""))
            if not c:
                continue
            n["name"] = c.get("name") or n["name"]
            if c.get("type"):
                n["type"], n["icon"] = c["type"], TYPE_ICON.get(c["type"], n["icon"])
            n["url"] = web_url(n["name"], n.get("ip", ""), c.get("url", ""))
            n["flags"] = [f for f in n["flags"] if f not in ("important", "unknown")] + \
                         (["important"] if c.get("important") else [])
        save_json(os.path.join(DATA_DIR, DATA), data)

    # switch ports from Zabbix (status, traffic, errors), matched by the ifIndex in the item key
    ports = {}
    for sid, sw_ in data["switches"].items():
        known = {str(p.get("id", "")) for p in sw_["ports"]}
        by_name = {p["full"]: str(p.get("id", "")) for p in sw_["ports"]}
        for it in api.call("item.get", {"hostids": sid, "output": ["key_", "name", "lastvalue"], "filter": {"status": 0},
                                        "search": {"key_": "net.if."}}):
            kind, pid = zabbix_port_item(it.get("key_", ""), it.get("name", ""), by_name)
            v = _f(it["lastvalue"])
            if kind and pid in known and v is not None:
                ports.setdefault(sid, {}).setdefault(pid, {})[kind] = v

    rt = load_json(os.path.join(DATA_DIR, RT), {}) or {}
    rt_fresh = bool(rt) and t - rt.get("t", 0) < 60
    hist = load_json(HIST_FILE, {})
    if hist.get("schema") != 2:
        # 0.3.0: ports are identified by ifIndex. Per-port data stored under the old port numbers could belong to
        # the wrong port, so it starts fresh (graphs: 90 minutes, normal-traffic baselines: 24 hours).
        for k in ("cable", "unusual", "base", "ports", "wan"):
            hist.pop(k, None)
        hist["schema"] = 2

    def node_on_port(sid, n):
        return next((x["id"] for x in data["nodes"] if x.get("port_ref") == [sid, str(n)] and x["kind"] != "switch"), None)

    # important devices: ping every minute, log changes
    imp = {n["id"]: n["ip"] for n in data["nodes"] if "important" in n.get("flags", []) and n.get("ip")}
    res = ping_many(sorted(set(imp.values())))
    nstat = {nid: ("up" if res.get(ip) else "down") for nid, ip in imp.items()}
    for nid, st in nstat.items():
        was = hist.get("imp", {}).get(nid)
        if was and was != st:
            nm_ = by_id.get(nid, {}).get("name", nid)
            events.append(ev("device", "bad" if st == "down" else "ok",
                             f"{nm_} is offline" if st == "down" else f"{nm_} is back online", nid))
    hist["imp"] = nstat

    # CPU / memory / disk / uptime from Zabbix
    stats, cache = {}, {}

    def host_items(hid):
        if hid not in cache:
            cache[hid] = api.call("item.get", {"hostids": hid, "output": ["name", "lastvalue", "units"],
                                               "filter": {"status": 0, "value_type": [0, 3]}})
        return cache[hid]
    for n in data["nodes"]:
        try:
            if n.get("zbx"):
                st = pick_stats([i for i in host_items(n["zbx"]) if not re.search(GUEST_RX, i["name"])])
            elif by_id.get(n.get("parent") or "", {}).get("zbx") and by_id[n["parent"]].get("type") == "server":
                rx = r"(?i)\[[^\]]*\b" + re.escape(n["name"]) + r"\b[^\]]*\]"
                st = pick_stats([i for i in host_items(by_id[n["parent"]]["zbx"]) if re.search(rx, i["name"])])
            else:
                continue
        except Exception:  # noqa: BLE001
            continue
        if st:
            stats[n["id"]] = st

    # minute-average traffic per port (from the 5-second service when it runs)
    def port_rate(sid, n):
        rows = rt.get("hist", {}).get("ports", {}).get(sid, {}).get(n, [])[-12:] if rt_fresh else []
        if rows:
            return sum(r[1] for r in rows) / len(rows), sum(r[2] for r in rows) / len(rows)
        v = ports.get(sid, {}).get(n)
        return (v.get("rx", 0), v.get("tx", 0)) if v else None

    # bad-cable detection: new errors, flapping, speed drop
    sw_names = {sw["name"] for sw in data["switches"].values()}
    cable, prev_c = {}, hist.get("cable", {})
    flaps = rt.get("flaps", {}) if rt_fresh else {}
    for sid, sw in data["switches"].items():
        for p in sw["ports"]:
            n, issues = str(p.get("id") or p["n"]), []
            zp_ = ports.get(sid, {}).get(n, {})
            err = (zp_.get("ein") or 0) + (zp_.get("eout") or 0)
            if err > 0:
                issues.append(f"{err:.2g} errors/s")
            if flaps.get(sid, {}).get(n, 0) >= 3:
                issues.append(f"link went down {flaps[sid][n]} times in the last hour")
            if p.get("downgrade"):
                issues.append(p["downgrade"])
            key = f"{sid}:{n}"
            lbl = f"{sw['name']} {p['label']}" + (f" ({', '.join(p['clients'])})" if p["clients"] else "")
            if issues:
                cable.setdefault(sid, {})[n] = issues
                if key not in prev_c:
                    events.append(ev("cable", "warn", f"Possible cable problem on {lbl}: {'; '.join(issues)}",
                                     node_on_port(sid, n)))
            elif key in prev_c:
                events.append(ev("cable", "ok", f"Cable problem cleared on {lbl}", node_on_port(sid, n)))
    hist["cable"] = {f"{s_}:{n}": 1 for s_, pp in cable.items() for n in pp}

    # unusual traffic: learn each device port's normal level, warn on big sustained jumps
    base, unusual, prev_u = hist.setdefault("base", {}), {}, hist.get("unusual", {})
    for sid, sw in data["switches"].items():
        for p in sw["ports"]:
            n = str(p.get("id") or p["n"])
            if not p["clients"] or any(c in sw_names or c.endswith("(gateway)") for c in p["clients"]):
                continue
            r = port_rate(sid, n)
            if r is None:
                continue
            key = f"{sid}:{n}"
            b = base.setdefault(key, {"n": 0, "rx": 0.0, "tx": 0.0, "rxv": 0.0, "txv": 0.0, "hot": 0})
            found = []
            for d_, word, v in (("rx", "sending", r[0]), ("tx", "receiving", r[1])):
                if b["n"] >= 1440 and v > max(10 * b[d_], b[d_] + 8 * b[d_ + "v"] ** 0.5, 5e6):
                    found.append((word, v, b[d_]))
            b["hot"] = b["hot"] + 1 if found else 0
            if not found:
                a_ = max(1.0 / (b["n"] + 1), 1.0 / 1440)
                for d_, v in (("rx", r[0]), ("tx", r[1])):
                    diff = v - b[d_]
                    b[d_] += a_ * diff
                    b[d_ + "v"] = (1 - a_) * (b[d_ + "v"] + a_ * diff * diff)
                b["n"] += 1
            if b["hot"] >= 3:
                word, v, m_ = found[0]
                unusual[key] = f"{', '.join(p['clients'])} is {word} {mbps_txt(v)} (normally about {mbps_txt(m_)})"
    for key, txt in unusual.items():
        if key not in prev_u:
            sid, n = key.split(":")
            events.append(ev("traffic", "warn", "Unusual traffic: " + txt, node_on_port(sid, n)))
    for key, txt in prev_u.items():
        if key not in unusual:
            sid, n = key.split(":")
            events.append(ev("traffic", "ok", "Traffic back to normal: " + txt.split(" is ")[0], node_on_port(sid, n)))
    hist["unusual"] = unusual

    # internet summary: from the 5-second service, or a quick fallback check
    if rt_fresh and rt.get("inet"):
        inet = rt["inet"]
        lat = {k: v.get("ms") for k, v in rt.get("targets", {}).items() if k != "Gateway"}
    else:
        lat = {name: ping_avg(ip) for name, ip in LAT_TARGETS}
        inet = {"up": any(v is not None for v in lat.values())}
    wan = None
    root = next((n for n in data["nodes"] if n.get("kind") == "switch" and n.get("flip") and n.get("port_ref")), None)
    if root:
        lv = ports.get(root["port_ref"][0], {}).get(str(root["port_ref"][1]))
        if lv:
            wan = {"down": lv.get("rx", 0), "up": lv.get("tx", 0)}
    usage = load_json(USAGE_FILE, {})
    gw_use = usage.get("m", {}).get(root["port_ref"][0], {}).get(str(root["port_ref"][1])) if root else None

    def push(lst, row):
        lst.append(row)
        del lst[:-HIST_LEN]
    if wan:
        push(hist.setdefault("wan", []), [t, round(wan["down"]), round(wan["up"])])
    hp = hist.setdefault("ports", {})
    for sid, pp in ports.items():
        for pn, v in pp.items():
            push(hp.setdefault(sid, {}).setdefault(pn, []), [t, round(v.get("rx", 0)), round(v.get("tx", 0))])
    hs = hist.setdefault("stats", {})
    for nid_, st in stats.items():
        push(hs.setdefault(nid_, []), [t, st.get("cpu", {}).get("pct"), st.get("mem", {}).get("pct")])
    for k in [k for k in hs if k not in stats]:
        del hs[k]

    # warnings -> one Zabbix problem that lists what is wrong right now
    warn = [f"cable: {by_id.get(node_on_port(s_, n) or '', {}).get('name') or data['switches'][s_]['name'] + ' port ' + n}"
            for s_, pp in cable.items() for n in pp]
    warn += ["traffic: " + x.split(" is ")[0] for x in unusual.values()]
    warn += [f"IP conflict {c['ip']}" for c in data.get("conflicts", [])]
    if inet.get("loss_warn"):
        warn.append(f"packet loss {inet.get('loss', 0):.0f}%")
    if inet.get("down_since"):
        warn.append("internet down")
    hist["warnings"] = warn
    hist["last"] = t
    save_json(HIST_FILE, hist)
    add_events(events)
    try:
        zh = next((h for h in at.load_hosts(api) if h["_role"] == "zabbix"), None)
        if zh:
            ids = ensure_event_items(api, zh)
            api.call("history.push", [{"itemid": ids["autotopo.warnings"], "value": "; ".join(warn)[:480] or "none"}])
    except Exception:  # noqa: BLE001
        pass

    problems = {}
    if setting("zabbix_triggers"):
        try:
            problems = zabbix_problems(api, {n["zbx"] for n in data["nodes"] if n.get("zbx")})
        except Exception as e:  # noqa: BLE001
            print("zabbix problems:", e, flush=True)
    save_json(os.path.join(DATA_DIR, LIVE), {
        "generated": t, "ports": ports, "nodes": nstat, "stats": stats, "cable": cable, "unusual": unusual,
        "problems": problems,
        "warnings": warn,
        "inet": dict(inet, latency=lat, wan=wan,
                     month={"down": gw_use[0], "up": gw_use[1]} if gw_use else None),
        "hist": {"wan": hist.get("wan", []), "ports": hp, "stats": hs}})


# --------------------------------------------------------------------------- #
# Real-time collector (service): port counters every 5 s, latency every 15 s, speed tests
# --------------------------------------------------------------------------- #
RT, RT_EVERY, RT_KEEP = "spidermap_rt.json", 5, 72          # 72 x 5 s = 6 minutes of live graph
SPEED_REQ = os.path.join(INBOX, "speedtest.request")
SPEED_FILE = os.path.join(EDITS_DIR, "speedtest.json")
SPEED_ITEMS = [("autotopo.speed.down", "Speed test: download", "bps"),
               ("autotopo.speed.up", "Speed test: upload", "bps"),
               ("autotopo.speed.ping", "Speed test: ping", "ms"),
               ("autotopo.speed.load", "Speed test: latency under load", "ms")]
SERVICE = "/etc/systemd/system/spidermap.service"
OLD_SERVICE = "spidermap-rt.service"                     # before 0.5.0 (scans then ran from cron)


def snmp_get(auth, target, oids):
    """Integer values of the given OIDs. Counters stay exact integers (a Counter64 does not fit a float);
    anything that is not a plain integer (noSuchInstance, strings, ...) is left out."""
    out = {}
    for i in range(0, len(oids), 30):                      # keep each request small
        r = subprocess.run(["snmpget", "-t", "2", "-r", "0", "-On", "-Oq", "-Oe", "-Ot", *auth, target, *oids[i:i + 30]],
                           capture_output=True, text=True, timeout=10, env=getattr(auth, "env", None))
        for line in r.stdout.splitlines():
            oid, _, val = line.partition(" ")
            val = val.strip().strip('"')
            if re.fullmatch(r"\d+", val):
                out[oid.lstrip(".")] = int(val)
    return out


COUNTER_SANITY_BPS = 400e9          # upper limit when the link speed is unknown


def build_inventory(nodes, sw, unifi_inv):
    """The network devices with what SNMP (ENTITY-MIB) or the UniFi controller says about them. Devices of one model
    running different firmware are marked: often one was forgotten in an update."""
    out = []
    for sid in sw:
        inv = zp.INVENTORY.get(sid) or {}
        n = nodes.get("s-" + sid)
        if n:
            out.append({"id": n["id"], "name": n["name"], "kind": "switch", **{k: inv.get(k, "") for k in
                        ("model", "serial", "firmware", "hardware", "vendor", "location", "contact", "descr")},
                        "source": "SNMP" if inv else ""})
    for nid, inv in unifi_inv.items():
        n = nodes.get(nid)
        if n:
            out.append({"id": nid, "name": n["name"], "kind": n.get("type", "device"), "location": "", "contact": "",
                        "hardware": "", "descr": "", **inv})
    by_model = {}
    for x in out:
        if x.get("model") and x.get("firmware"):
            by_model.setdefault(x["model"], set()).add(x["firmware"])
    for x in out:
        x["firmware_differs"] = len(by_model.get(x.get("model"), ())) > 1
    return out


VIRTUAL_OUI = ("00:50:56", "00:0c:29", "00:05:69", "00:1c:14", "bc:24:11", "52:54:00", "00:15:5d", "08:00:27", "00:16:3e",
               "02:42:")                                     # VMware, Proxmox, KVM/QEMU, Hyper-V, VirtualBox, Xen, Docker


def is_virtual_mac(mac):
    """A virtual machine's (or container's) MAC address, by its maker's prefix."""
    return str(mac).lower().startswith(VIRTUAL_OUI)


def core_switch_id(sw, want):
    """The switch named (or with the IP) set as core switch in Settings, or None."""
    want = str(want or "").strip().lower()
    if not want:
        return None
    return next((k for k, s_ in sw.items() if want in (s_["host"]["name"].lower(), s_["host"]["host"].lower())
                 or want in {str(i) for i in s_["host"].get("_ips", ())}), None)


def recheck_speed(auth, target, ifindex):
    """The port's speed now (ifHighSpeed, Mbps), or 0."""
    try:
        return snmp_get(auth, target, [f"1.3.6.1.2.1.31.1.1.1.15.{ifindex}"]).get(f"1.3.6.1.2.1.31.1.1.1.15.{ifindex}", 0)
    except Exception:  # noqa: BLE001
        return 0


def counter_rate(prev, cur, speed_mbps):
    """Bits per second for one port from two counter samples, following RFC 2863.
    prev / cur: (monotonic_seconds, rx_octets, tx_octets, sysUpTime, discontinuity_time, is_32bit)
    Returns (rx_bps, tx_bps, rx_bytes, tx_bytes, reason): reason is None for a valid sample, otherwise
      "first"          no earlier sample (or the counter type changed)
      "short"          samples less than a second apart
      "reboot"         the device restarted (sysUpTime went back)
      "discontinuity"  the device says the counters are not comparable (ifCounterDiscontinuityTime changed)
      "reset"          a 64-bit counter went back
      "impossible"     more than the link can carry"""
    none = (None, None, None, None)
    if prev is None or prev[5] != cur[5]:
        return none + ("first",)
    dt = cur[0] - prev[0]
    if dt < 1:
        return none + ("short",)
    if cur[3] is not None and prev[3] is not None and cur[3] < prev[3]:
        return none + ("reboot",)
    if cur[4] != prev[4]:
        return none + ("discontinuity",)
    drx, dtx = cur[1] - prev[1], cur[2] - prev[2]
    if cur[5]:                                  # 32-bit counters wrap around at 2^32
        drx, dtx = drx % 2 ** 32, dtx % 2 ** 32
    elif drx < 0 or dtx < 0:
        return none + ("reset",)
    rx, tx = drx * 8 / dt, dtx * 8 / dt
    limit = speed_mbps * 1e6 * 1.05 if speed_mbps else COUNTER_SANITY_BPS
    if rx > limit or tx > limit:
        return rx, tx, drx, dtx, "impossible"
    return rx, tx, drx, dtx, None


OOKLA = "/usr/local/bin/ookla-speedtest"   # Ookla's official CLI, installed by install.sh


# --------------------------------------------------------------------------- #
# Speed test providers without an extra program: Cloudflare (speed.cloudflare.com) and Fast.com (Netflix)
# --------------------------------------------------------------------------- #


def run_speedtest(api, zh, state):
    state.update({"running": True, "started": now(), "error": ""})
    try:
        provider = setting("speedtest_provider")
        if provider == "cloudflare":
            res = dict(_speedtest_cloudflare(), at=now())
        elif provider == "fast":
            res = dict(_speedtest_fast(), at=now())
        else:
            res = dict(_speedtest_once(setting("speedtest_server")), at=now())
        extra = ""
        if res.get("load_down"):
            extra = f", under load ↓ {res['load_down']:.0f} ms ↑ {res.get('load_up') or 0:.0f} ms"
        add_events([ev("speedtest", "info", f"Speed test: ↓ {res['down'] / 1e6:.0f} Mbps, ↑ {res['up'] / 1e6:.0f} Mbps, "
                                            f"ping {res['ping']} ms{extra} ({res['server']})", "inet")])
        hist = load_json(SPEED_FILE, [])
        hist.append(res)
        save_json(SPEED_FILE, hist[-50:])
        if api and zh:
            ids = {}
            for key, name, units in SPEED_ITEMS:
                it = api.call("item.get", {"hostids": zh["hostid"], "output": ["itemid"], "filter": {"key_": key}})
                ids[key] = it[0]["itemid"] if it else api.call("item.create", {
                    "hostid": zh["hostid"], "name": name, "key_": key, "type": 2, "value_type": 0, "units": units,
                    "tags": [{"tag": "source", "value": "autotopo"}]})["itemids"][0]
            vals = {"autotopo.speed.down": res["down"], "autotopo.speed.up": res["up"], "autotopo.speed.ping": res["ping"],
                    "autotopo.speed.load": max(res.get("load_down") or 0, res.get("load_up") or 0) or None}
            api.call("history.push", [{"itemid": ids[k], "value": v} for k, v in vals.items() if v is not None and k in ids])
    except Exception as e:  # noqa: BLE001
        state["error"] = str(e)
    state["running"] = False


SCHED_FILE = os.path.join(STATE_DIR, "spidermap_schedule.json")         # last scheduled speed test (survives restarts)
SCAN_REPORT = os.path.join(STATE_DIR, "spidermap_scan_report.json")      # last full scan: per switch, warnings
SCAN_PROGRESS = os.path.join(STATE_DIR, "spidermap_scan_progress.json")  # how far the running scan is


def scan_progress(pct, phase):
    try:
        save_json(SCAN_PROGRESS, {"t": now(), "pct": int(pct), "phase": phase})
    except OSError:
        pass


def gateway_step(fails, answered):
    """Count missed gateway pings in a row. Returns (fails, "down" | "up" | None).
    "down" after the 2nd miss in a row, "up" on the first answer after that."""
    if answered:
        return 0, ("up" if fails >= 2 else None)
    fails += 1
    return fails, ("down" if fails == 2 else None)


def speedtest_due(daily, now_dt, last_day):
    """True when the daily speed test time ("HH:MM") has passed today and it has not run today yet."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", daily or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return False
    return now_dt.strftime("%Y-%m-%d") != last_day and (now_dt.hour, now_dt.minute) >= (int(m.group(1)), int(m.group(2)))


# --------------------------------------------------------------------------- #
# The service: scheduler for scans, requests from the page, settings, status and health
# --------------------------------------------------------------------------- #
JOBS_FILE = os.path.join(STATE_DIR, "spidermap_jobs.json")              # job history (survives restarts)
CONF_HIST_DIR = os.path.join(STATE_DIR, "config-history")               # earlier versions of devices.json
JOB_LOG = os.path.join(EDITS_DIR, "spidermap.log")
STATUS = "spidermap_status.json"          # in DATA_DIR, for every viewer: jobs, pause, health
SETTINGS_OUT = "spidermap_settings.json"  # in DATA_DIR, for users with the settings permission
ACCESS = "access.json"                    # in DATA_DIR, read by spidermap.php at every request
SCAN_REQ, CANCEL_REQ, SETTINGS_REQ = (os.path.join(INBOX, f) for f in ("scan.request", "scan.cancel", "settings.request"))
JOB_TIMEOUT = {"scan": 900, "live": 180}
DEVICE_FIELDS = {"name": str, "type": str, "url": str, "pin": str, "important": bool, "approved": bool, "behind": str}
ADVANCED_SETTINGS = {"port_media": dict, "aliases": dict, "manual_links": list, "core_switch": str, "speedtest_server": str}


def take_request(path):
    """Read and remove a request file written by spidermap.php. None if there is none.
    Always removed, also when unreadable, so that a bad file cannot repeat forever."""
    if not os.path.exists(path):
        return None
    req = {}
    try:
        with open(path, encoding="utf-8") as f:
            req = json.loads(f.read() or "{}")
    except (OSError, ValueError):
        pass
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return req if isinstance(req, dict) else {}


def validate_devices(devs):
    """Devices from an import: MAC address keys, known fields with the right types, safe web addresses."""
    if not isinstance(devs, dict) or len(devs) > 5000:
        return None, ["devices must be an object (at most 5000)"]
    out, errors = {}, []
    for mac, d in devs.items():
        m = str(mac).lower()
        if not MAC_RX.match(m) or not isinstance(d, dict):
            errors.append(f"device {str(mac)[:20]}: not a MAC address")
            continue
        clean = {}
        for k, v in d.items():
            if k not in DEVICE_FIELDS or not isinstance(v, DEVICE_FIELDS[k]) or (isinstance(v, str) and len(v) > 200):
                continue
            if k == "url" and v and not re.match(r"^https?://", v, re.I):
                errors.append(f"device {m}: web address must start with http:// or https://")
                continue
            clean[k] = v
        out[m] = clean
        if len(errors) > 10:
            break
    return (out, []) if not errors else (None, errors)


def config_backup(cfg, by, why):
    os.makedirs(CONF_HIST_DIR, exist_ok=True)
    os.chmod(CONF_HIST_DIR, 0o700)
    stamp = time.time_ns()                                   # unique even for several saves in one second
    while os.path.exists(os.path.join(CONF_HIST_DIR, f"devices-{stamp}.json")):
        stamp += 1
    name = f"devices-{stamp}.json"
    save_json(os.path.join(CONF_HIST_DIR, name), {"saved_at": now(), "by": by, "why": why, "config": cfg})
    for old in sorted((f for f in os.listdir(CONF_HIST_DIR) if f.startswith("devices-")),
                      key=lambda f: int(re.sub(r"\D", "", f) or 0))[:-10]:                  # keep 10
        os.remove(os.path.join(CONF_HIST_DIR, old))
    return name


def config_history():
    try:
        files = sorted((f for f in os.listdir(CONF_HIST_DIR) if f.startswith("devices-")),
                       key=lambda f: int(re.sub(r"\D", "", f) or 0), reverse=True)
    except FileNotFoundError:
        return []
    out = []
    for f in files:
        h = load_json(os.path.join(CONF_HIST_DIR, f), {})
        out.append({"id": f, "saved_at": h.get("saved_at"), "by": h.get("by"), "why": h.get("why")})
    return out


def apply_settings_request(req):
    """A change from the Settings panel: {"by", "settings": {...}} or {"by", "import": {...}} or {"by", "restore": id}.
    Returns the result shown on the page. devices.json is only written when everything is valid, and the previous
    version is kept (Settings > Backup > Restore)."""
    by = str(req.get("by") or "?")[:64]
    cfg = load_json(DEVICES_FILE, DEFAULT_DEVICES)
    cur = cfg.setdefault("settings", {})
    res = {"at": now(), "by": by, "ok": False, "errors": [], "rid": str(req.get("rid") or "")[:24]}
    if "settings" in req:
        clean, errors = validate_settings(req["settings"], cur)
        if errors:
            res["errors"] = errors
            return res
        old = effective_settings(cur)
        new_cfg = json.loads(json.dumps(cfg))
        ns = new_cfg["settings"]
        for k, v in clean.items():
            if k == "permissions":
                ns.setdefault("permissions", {}).update(v)
                ns.pop("view_min_user_type", None)
            else:
                ns[k] = v
        ns.pop("discovery_paused", None)
        changed = {k: [old[k], v] for k, v in effective_settings(ns).items() if old[k] != v and k not in SECRET_KEYS}
        if any(k in clean for k in SECRET_KEYS) and not changed:
            changed = {}                                          # only a password: still saved below
        if not changed and not any(k in clean for k in SECRET_KEYS):
            res.update(ok=True, message="Nothing changed.")
            return res
        secrets = {k: ns.pop(k) for k in SECRET_KEYS if k in ns}          # secrets never go into devices.json
        if secrets:
            save_secrets({**load_secrets(), **secrets})
            changed.update({k: ["(set)", "(changed)"] for k in secrets})
        config_backup(cfg, by, "before settings change")
        save_json(DEVICES_FILE, new_cfg)
        audit("settings", by, {"changes": changed})
        add_events([ev("audit", "info", f"{by} changed settings: " + ", ".join(sorted(changed))[:300])])
        res.update(ok=True, changed=sorted(changed), message="Saved.")
        return res
    if "import" in req:
        imp = req["import"] if isinstance(req["import"], dict) else {}
        s_in = dict(imp.get("settings") or {})
        flat = {}
        for key in SETTINGS_KEYS:                            # schema settings, as stored in devices.json
            v = _get(s_in, key, None)
            if v is not None:
                flat[key] = v
        clean, errors = validate_settings(flat, cur)
        adv = {}
        for k, typ in ADVANCED_SETTINGS.items():
            if k in s_in:
                if isinstance(s_in[k], typ):
                    adv[k] = s_in[k]
                else:
                    errors.append(f"{k}: wrong type")
        devs, derr = validate_devices(imp.get("devices", cfg.get("devices", {})))
        errors += derr
        if errors:
            res["errors"] = errors[:12]
            return res
        new_cfg = {"settings": {**cur, **adv}, "devices": devs}
        for k, v in clean.items():
            if k == "permissions":
                new_cfg["settings"].setdefault("permissions", {}).update(v)
            else:
                new_cfg["settings"][k] = v
        config_backup(cfg, by, "before import")
        save_json(DEVICES_FILE, new_cfg)
        audit("import", by, {"devices": len(devs), "settings": sorted(clean) + sorted(adv)})
        add_events([ev("audit", "info", f"{by} imported settings and {len(devs)} devices")])
        res.update(ok=True, message=f"Imported {len(devs)} devices and the settings.")
        return res
    if "link" in req:
        lk = req["link"] if isinstance(req["link"], dict) else {}
        a, b, state = lk.get("a"), lk.get("b"), str(lk.get("state") or "")
        dm = load_json(os.path.join(DATA_DIR, DATA), {}) or {}
        known = {(x.get("name"), p.get("base", p.get("label"))) for x in (dm.get("switches") or {}).values() for p in x.get("ports", [])}
        if not (isinstance(a, list) and isinstance(b, list) and len(a) == 2 and len(b) == 2 and tuple(a) in known
                and tuple(b) in known and a[0] != b[0]):
            res["errors"] = ["Both ends must be switch ports on the map, on two different switches."]
            return res
        if state not in ("confirmed", "rejected", "remove"):
            res["errors"] = ["Unknown choice."]
            return res
        config_backup(cfg, by, "before a link change")
        pair = frozenset([tuple(a), tuple(b)])
        links = [L for L in cfg.get("links", []) if frozenset([tuple(L["a"]), tuple(L["b"])]) != pair]
        if state != "remove":
            links.append({"a": a, "b": b, "state": state})
        cfg["links"] = links
        save_json(DEVICES_FILE, cfg)
        audit("link", by, {"a": a, "b": b, "state": state})
        add_events([ev("audit", "info", f"{by} {'removed the decision on' if state == 'remove' else state} the link "
                                        f"{a[0]} {a[1]} ↔ {b[0]} {b[1]}")])
        res.update(ok=True, rescan=True, message="Saved. The map shows it after the scan that starts now.")
        return res
    if "place" in req:
        pl = req["place"] if isinstance(req["place"], dict) else {}
        key, kind = str(pl.get("key") or ""), str(pl.get("kind") or "")
        dm = load_json(os.path.join(DATA_DIR, DATA), {}) or {}
        known = {f"{x.get('name')}|{p.get('base', p.get('label'))}" for x in (dm.get("switches") or {}).values() for p in x.get("ports", [])}
        if key not in known:
            res["errors"] = ["That switch port is not on the map."]
            return res
        if kind not in PLACE_KINDS and kind not in ("none", "remove"):
            res["errors"] = ["Unknown kind of device."]
            return res
        vals = {k_: str(pl.get(k_) or "").strip()[:60] for k_ in ("name", "model", "location")}
        if any(re.search(r"[<>\x00-\x1f]", v) for v in vals.values()):
            res["errors"] = ["No < > or control characters, please."]
            return res
        config_backup(cfg, by, "before a map edit")
        places = cfg.setdefault("places", {})
        if kind == "remove":
            places.pop(key, None)
        else:
            places[key] = {"kind": kind, **{k_: v for k_, v in vals.items() if v}}
        save_json(DEVICES_FILE, cfg)
        what = "removed" if kind == "remove" else ("not a device" if kind == "none" else PLACE_KINDS[kind][0] + (f" “{vals['name']}”" if vals["name"] else ""))
        audit("place", by, {"port": key, "kind": kind, **vals})
        add_events([ev("audit", "info", f"{by} set {key.replace('|', ' ')}: {what}")])
        res.update(ok=True, rescan=True, message="Saved. The map shows it after the scan that starts now.")
        return res
    if "reset" in req:
        opt = req["reset"] if isinstance(req["reset"], dict) else {}
        keep_dev, keep_sp = opt.get("keep_devices", True) is not False, opt.get("keep_speedtests", True) is not False
        bdir = os.path.join(STATE_DIR, "reset-" + time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(bdir, mode=0o700, exist_ok=True)
        files = [STATE_FILE, HIST_FILE, EVENTS_FILE, PRESENCE_FILE, INET_FILE, USAGE_FILE, SCAN_REPORT, SCAN_PROGRESS]
        if not keep_sp:
            files.append(SPEED_FILE)
        moved = 0
        for f in files:                                   # moved to a backup folder, not deleted
            if os.path.exists(f):
                shutil.move(f, os.path.join(bdir, os.path.basename(f)))
                moved += 1
        for f in (DATA, LIVE, RT, "spidermap_events.json", "spidermap_presence.json"):
            try:
                os.remove(os.path.join(DATA_DIR, f))
            except FileNotFoundError:
                pass
        if not keep_dev:
            config_backup(cfg, by, "before map reset")
            cfg["devices"] = {}
            save_json(DEVICES_FILE, cfg)
        olds = sorted(d for d in os.listdir(STATE_DIR) if d.startswith("reset-"))
        for d in olds[:-3]:                               # keep the last 3 resets
            shutil.rmtree(os.path.join(STATE_DIR, d), ignore_errors=True)
        audit("reset", by, {"keep_devices": keep_dev, "keep_speedtests": keep_sp, "backup": bdir})
        add_events([ev("audit", "info", f"{by} reset the map" + ("" if keep_dev else " and forgot the device names"))])
        res.update(ok=True, restart=True, message="Map reset. A new scan starts now; the map appears within a few minutes."
                   + ("" if keep_dev else " Your device settings can be brought back: Settings > General > Saved versions > "
                                          "'before map reset'."))
        return res
    if "restore" in req:
        rid = str(req["restore"])
        if not re.fullmatch(r"devices-\d+\.json", rid) or not os.path.isfile(os.path.join(CONF_HIST_DIR, rid)):
            res["errors"] = ["That saved version does not exist."]
            return res
        old = load_json(os.path.join(CONF_HIST_DIR, rid), {}).get("config")
        if not isinstance(old, dict) or not isinstance(old.get("devices", {}), dict):
            res["errors"] = ["That saved version cannot be read."]
            return res
        config_backup(cfg, by, "before restore")
        save_json(DEVICES_FILE, old)
        audit("restore", by, {"version": rid})
        saved = load_json(os.path.join(CONF_HIST_DIR, rid), {}).get("saved_at")
        add_events([ev("audit", "info", f"{by} restored the settings saved {iso(saved) if saved else 'earlier'}")])
        res.update(ok=True, message="Restored.")
        return res
    res["errors"] = ["Unknown request."]
    return res


def publish_settings():
    """What the Settings panel shows (settings permission only), and the permissions spidermap.php enforces."""
    cfg = load_json(DEVICES_FILE, DEFAULT_DEVICES)
    st = cfg.get("settings", {})
    save_json(os.path.join(DATA_DIR, SETTINGS_OUT), {
        "schema": SETTINGS_SCHEMA,
        "values": {k: ("" if k in SECRET_KEYS else v) for k, v in effective_settings(st).items()},
        "secrets_set": {k: bool(load_secrets().get(k)) for k in SECRET_KEYS},
        "effective_permissions": permissions(st),
        "advanced": {k: st[k] for k in ADVANCED_SETTINGS if k in st},
        "device_categories": [{"key": k, "title": t, "examples": e, "detect": d, "icon": TYPE_ICON.get(ty, "help-circle")}
                              for k, ty, t, e, d, _rx in DEVICE_CATEGORIES],
        "ookla_servers": load_json(OOKLA_SERVERS, {}).get("servers", []),
        "devices": cfg.get("devices", {}), "history": config_history()})
    save_json(os.path.join(DATA_DIR, ACCESS), permissions(st))


DISPLAY_KEYS = ("skin", "language", "layout", "default_view", "zabbix_triggers", "wheel", "map_name", "page_refresh_s", "show_offline", "ip_labels", "link_thickness", "units", "load_warn", "load_crit")


def in_scan_window(S, dt):
    """True when automatic scans may run now (always, unless the off-peak window is on)."""
    if not S["offpeak"]:
        return True
    hm = lambda x: int(x[:2]) * 60 + int(x[3:5])  # noqa: E731
    now_m, a, b = dt.hour * 60 + dt.minute, hm(S["offpeak_start"]), hm(S["offpeak_end"])
    return a <= now_m < b if a < b else (now_m >= a or now_m < b)      # a window can pass midnight


class Scheduler:
    """Runs the full scan and the status refresh as separate processes (like cron did), one at a time each,
    with timeouts; handles scan / cancel / settings requests from the page; remembers its history."""

    def __init__(self):
        st = load_json(JOBS_FILE, {})
        self.jobs = {k: dict(st.get("jobs", {}).get(k, {})) for k in ("scan", "live")}
        for j in self.jobs.values():
            if j.get("running"):                    # the service stopped while a job ran
                j.update(running=False, last_result="interrupted")
        self.history = st.get("history", [])[-30:]
        self.config_result = st.get("config_result")
        self.proc = {}
        self.finished = set()                       # jobs that ended since the last check (the loop reloads data)
        self.restart = False                        # after a map reset: the service starts again with a clean memory
        self.started_at = now()

    @staticmethod
    def command(name):
        quiet = setting("log_level") in ("error", "warning")
        return [sys.executable, os.path.join(HERE, "spidermap.py")] + (["--live"] if name == "live" else (["--quiet"] if quiet else []))

    def save(self):
        save_json(JOBS_FILE, {"jobs": self.jobs, "history": self.history[-30:], "config_result": self.config_result})

    def due(self, name, t, S):
        if name in self.proc:
            return False
        if name == "scan" and (not S["auto_scan"] or not in_scan_window(S, datetime.datetime.now())):
            return False
        every = S["scan_interval_min"] * 60 if name == "scan" else S["live_interval_s"]
        return t - self.jobs[name].get("last_start", 0) >= every

    def start(self, name, reason, by=None):
        if name in self.proc:
            return False
        rotate_log()
        log = open(JOB_LOG, "a", encoding="utf-8")
        log.write(f"--- {iso(now())} {name} ({reason}{', by ' + by if by else ''})\n")
        log.flush()
        try:
            env = dict(os.environ, SPIDERMAP_DEBUG="1") if setting("log_level") == "debug" else None
            p = subprocess.Popen(self.command(name), stdout=log, stderr=subprocess.STDOUT, cwd=HERE, start_new_session=True, env=env)
        finally:
            log.close()
        self.proc[name] = {"p": p, "t": now(), "mono": time.monotonic(), "reason": reason, "by": by}
        self.jobs[name].update(running=True, last_start=now(), reason=reason, by=by)
        self.save()
        return True

    def cancel(self, name, why="cancelled"):
        r = self.proc.get(name)
        if not r:
            return False
        r["cancel"] = why
        try:
            os.killpg(r["p"].pid, signal.SIGTERM)
            try:
                r["p"].wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(r["p"].pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return True

    def poll(self):
        for name, r in list(self.proc.items()):
            rc = r["p"].poll()
            if rc is None:
                if time.monotonic() - r["mono"] > JOB_TIMEOUT[name]:
                    self.cancel(name, "timeout")
                continue
            secs = round(time.monotonic() - r["mono"], 1)
            result = "ok" if rc == 0 else (r.get("cancel") or f"failed (exit {rc})")
            j = self.jobs[name]
            j.update(running=False, last_end=now(), last_result=result, last_seconds=secs)
            if rc == 0:
                j["last_ok"] = now()
                j.pop("error", None)
            else:
                j["last_error"], j["error"] = now(), last_error_line()
            if name == "scan" or rc != 0:           # every scan, and refreshes only when they fail
                self.history.append({"job": name, "t": r["t"], "seconds": secs, "result": result,
                                     "reason": r["reason"], "by": r["by"]})
            del self.proc[name]
            self.finished.add(name)
            self.save()

    def tick(self, t, S):
        self.poll()
        req = take_request(SCAN_REQ)
        if req is not None:
            by = str(req.get("by") or "?")[:64]
            if self.start("scan", "manual", by):
                audit("scan", by, {})
        req = take_request(CANCEL_REQ)
        if req is not None and self.cancel("scan"):
            audit("scan-cancel", str(req.get("by") or "?")[:64], {})
        req = take_request(SETTINGS_REQ)
        if req is not None:
            if "reset" in req:
                for n in list(self.proc):
                    self.cancel(n, "map reset")
                self.poll()
            self.config_result = apply_settings_request(req)
            if self.config_result.get("rescan"):
                self.start("scan", "map edit")
            if self.config_result.get("restart"):
                self.jobs["scan"]["last_start"] = 0          # scan right after the restart
                self.restart = True
                self.save()
                return                                       # nothing more now: the clean service starts the scan
            publish_settings()
            self.save()
        for name in ("live", "scan"):
            if self.due(name, t, S):
                self.start(name, "scheduled")

    def status(self, t, S, health):
        jobs = {}
        for name, j in self.jobs.items():
            jj = dict(j)
            if name in self.proc:
                jj["elapsed"] = round(time.monotonic() - self.proc[name]["mono"])
                if name == "scan":
                    pr = load_json(SCAN_PROGRESS, {})
                    if pr.get("t", 0) >= self.proc[name]["t"]:          # only this scan's progress
                        jj["pct"], jj["phase"] = pr.get("pct"), pr.get("phase")
            every = S["scan_interval_min"] * 60 if name == "scan" else S["live_interval_s"]
            jj["next"] = None if (name == "scan" and not S["auto_scan"]) else j.get("last_start", t) + every
            jobs[name] = jj
        return {"t": t, "version": __version__, "user": _run_user(), "service_since": self.started_at,
                "paused": not S["auto_scan"], "offpeak": [S["offpeak_start"], S["offpeak_end"]] if S["offpeak"] else None,
                "display": {k: S[k] for k in DISPLAY_KEYS}, "jobs": jobs, "history": self.history[-15:],
                "config": {"result": self.config_result, "saved_at": int(os.path.getmtime(DEVICES_FILE))
                           if os.path.exists(DEVICES_FILE) else None}, "health": health}


def rotate_log():
    """One log file per day (spidermap.log.YYYY-MM-DD), kept for "log_keep_days"; at most 5 MB a day."""
    os.makedirs(os.path.dirname(JOB_LOG), exist_ok=True)
    if os.path.exists(JOB_LOG):
        day = datetime.date.fromtimestamp(os.path.getmtime(JOB_LOG)).isoformat()
        if day != datetime.date.today().isoformat():
            os.replace(JOB_LOG, f"{JOB_LOG}.{day}")
        elif os.path.getsize(JOB_LOG) > 5_000_000:
            os.replace(JOB_LOG, f"{JOB_LOG}.{day}.{int(time.time())}")
    cut = time.time() - setting("log_keep_days") * 86400
    folder, base = os.path.split(JOB_LOG)
    for f in os.listdir(folder):
        full = os.path.join(folder, f)
        if f.startswith(base + ".") and os.path.getmtime(full) < cut:
            os.remove(full)


def snmp_summary(h):
    """What a switch uses for SNMP, from its interface in Zabbix - never the passwords or the community."""
    d = (h.get("_snmp") or {}).get("details") or {}
    v = str(d.get("version", "2"))
    if v == "3":
        lvl = at.SEC_LEVEL.get(str(d.get("securitylevel", "0")), "?")
        return f"v3 {lvl}, user {at.resolve(d.get('securityname'), h.get('_macros', {}))}"
    return ("v1" if v == "1" else "v2c") + ", community from Zabbix"


def last_error_line():
    """The last meaningful line of the job log, shown as the error of a failed job."""
    try:
        with open(JOB_LOG, "rb") as f:
            f.seek(max(0, os.path.getsize(JOB_LOG) - 4000))
            lines = [x.strip() for x in f.read().decode("utf-8", "ignore").splitlines() if x.strip()]
    except OSError:
        return ""
    lines = [x for x in lines if not x.startswith("---")]
    return lines[-1][:300] if lines else ""


def _run_user():
    import pwd
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def health_report(api_state, snmp_state):
    """Collector health: Zabbix API, SNMP per switch, disk space, age of the data, last scan per switch."""
    t = now()
    rep = load_json(SCAN_REPORT, {})
    disk = shutil.disk_usage(EDITS_DIR if os.path.isdir(EDITS_DIR) else "/")
    ages = {}
    for k, f in (("map", DATA), ("live", LIVE), ("realtime", RT)):
        try:
            ages[k] = t - int(os.path.getmtime(os.path.join(DATA_DIR, f)))
        except OSError:
            ages[k] = None
    switches = {}
    for sid, x in (rep.get("switches") or {}).items():
        switches[sid] = dict(x)
    for sid, x in snmp_state.items():
        switches.setdefault(sid, {"name": x.get("name", sid)})["realtime"] = x
    return {"api": api_state, "switches": switches, "unifi": rep.get("unifi"), "scan": {k: rep.get(k) for k in
            ("started", "finished", "partial", "warnings", "nodes", "online", "targets")},
            "disk": {"path": EDITS_DIR, "free": disk.free, "total": disk.total}, "data_age": ages,
            "speedtest": "ookla" if os.path.isfile(OOKLA) else ("speedtest-cli" if shutil.which("speedtest-cli") else None)}


def rt_loop(api):
    import threading
    from collections import deque
    webd = webdir()
    hosts, data, last_cfg, prev = [], None, 0, {}
    hist = {"wan": [], "ports": {}, "lat": []}
    targets, last_lat, speed = {}, 0, {"running": False, "error": ""}
    daily_done, last_save = load_json(SCHED_FILE, {}).get("speedtest_daily_last", ""), 0
    prev_st, flap_t = {}, {}
    changes, hints = deque(maxlen=200), load_json(HINTS_FILE, {})     # port up/down changes; ports that change together
    series, last_corr = {}, 0                                          # 5-second rates per port, for traffic correlation
    use32, quality, rej_said = {}, {}, {}
    live_speed, rescan_at = {}, {}                # port speeds read again after a change; last rescan per switch
    inet = load_json(INET_FILE, {})
    inet_start(inet, now())
    save_json(INET_FILE, inet)
    fails, gw_fails, loss_win = 0, 0, deque(maxlen=8)
    usage = load_json(USAGE_FILE, {})
    if usage.get("schema") != 2:
        # before 0.3.0 two ports could share one counter baseline and add impossible amounts to the totals.
        # Keep the old totals as a backup and start counting again, marked with the date.
        if usage.get("d") or usage.get("m"):
            base, ext = os.path.splitext(USAGE_FILE)
            save_json(f"{base}.before-0.3.0{ext or '.json'}", usage)
            add_events([ev("traffic", "info", "Data usage totals restarted: earlier totals could be wrong "
                                              "(backup kept as spidermap_usage.before-0.3.0.json)")])
        usage = {"schema": 2, "since": now()}
        save_json(USAGE_FILE, usage)
    sched = Scheduler()
    api_state, snmp_state, health, last_health = {}, {}, {}, 0
    upnp = {"svc": None, "next": 0, "fails": 0, "prev": None, "link": 0, "error": ""}
    rsnmp, wan_bad, hosts = {}, {}, []
    last_ookla = 0
    try:
        publish_settings()
    except Exception as e:  # noqa: BLE001
        print("settings:", e, flush=True)
    while True:
        t0 = time.time()
        S = current_settings()[0]
        every = S["rt_interval_s"]
        try:                                         # scans, refreshes, requests from the page - also before any data
            sched.tick(int(t0), S)
        except Exception as e:  # noqa: BLE001
            print("scheduler:", e, flush=True)
        if "scan" in sched.finished:                 # a new map: use it right away
            sched.finished.discard("scan")
            last_cfg = 0
        sched.finished.discard("live")
        if t0 - last_ookla > 86400:                  # nearby Ookla servers for Settings, once a day, in the background
            import threading
            last_ookla = t0
            threading.Thread(target=lambda: (ookla_servers(refresh=True), publish_settings()), daemon=True).start()
        if t0 - last_health > 30:
            health, last_health = health_report(api_state, snmp_state), t0
            health["wan"] = {"mode": S["wan_source"], "snmp": {"router": rsnmp.get("router"), "interface": rsnmp.get("ifname"),
                                                                "error": rsnmp.get("error", "")} if rsnmp else None,
                             "upnp": {"ok": bool(upnp["svc"]) and not upnp.get("stuck_until"),
                                      "name": (upnp["svc"] or {}).get("name", ""), "error": upnp["error"]}}
        save_json(os.path.join(DATA_DIR, STATUS), sched.status(int(t0), S, health))
        if sched.restart:
            print("map reset: restarting the service", flush=True)
            sys.exit(0)                              # systemd starts it again (Restart=always)
        if t0 - last_cfg > 300 or data is None:
            try:
                hosts = at.load_hosts(api)
                api_state = {"ok": True, "at": int(t0)}
                data = load_json(os.path.join(DATA_DIR, DATA), None)
            except Exception as e:  # noqa: BLE001
                api_state = {"ok": False, "at": int(t0), "error": str(e)[:200]}
                print("config reload failed:", e, flush=True)
            last_cfg = t0
        if not data:
            time.sleep(every)
            continue
        t = int(t0)
        evs = []
        day, month = datetime.datetime.now().strftime("%Y-%m-%d"), datetime.datetime.now().strftime("%Y-%m")
        if usage.get("day") != day:
            usage.update({"day": day, "d": {}})
            usage.setdefault("wan", {})["d"] = [0, 0]
        if usage.get("month") != month:
            usage.update({"month": month, "m": {}})
            usage.setdefault("wan", {})["m"] = [0, 0]
        hb = {h["hostid"]: h for h in hosts}
        port_name = {}
        ports = {}
        for sid, sw in data["switches"].items():
            h = hb.get(sid)
            if not h or not h["_snmp"]:
                continue
            # identity = ifIndex ("id"); "n" is only the number shown on screen
            plist = [p for p in sw["ports"] if p.get("ifindex")]
            idx = {str(p["ifindex"]): str(p.get("id") or p["ifindex"]) for p in plist}
            speeds = {str(p["ifindex"]): live_speed.get((sid, str(p["ifindex"])), p.get("speed_mbps", 0)) for p in plist}
            for p in plist:
                port_name[(sid, idx[str(p["ifindex"])])] = f"{sw['name']} {p['label']}" + \
                    (f" ({', '.join(p['clients'])})" if p["clients"] else "")
            v1 = str((h["_snmp"].get("details") or {}).get("version", "2")) == "1"
            low32 = use32.setdefault(sid, set(idx) if v1 else set())   # SNMPv1 has no 64-bit counters
            oids = ["1.3.6.1.2.1.1.3.0"]                                  # sysUpTime: detects a reboot
            for i in idx:
                oids += ([f"1.3.6.1.2.1.2.2.1.10.{i}", f"1.3.6.1.2.1.2.2.1.16.{i}"] if i in low32 else
                         [f"1.3.6.1.2.1.31.1.1.1.6.{i}", f"1.3.6.1.2.1.31.1.1.1.10.{i}"])
                oids.append(f"1.3.6.1.2.1.2.2.1.8.{i}")
                if not v1:
                    oids.append(f"1.3.6.1.2.1.31.1.1.1.19.{i}")              # ifCounterDiscontinuityTime
            try:
                auth, target = at.snmp_auth(h["_snmp"], h["_macros"])
                vals = snmp_get(auth, target, oids)
                if not vals:
                    raise RuntimeError("no answer")
                snmp_state[sid] = {"name": h["name"], "ok": True, "at": int(t0), "snmp": snmp_summary(h)}
            except Exception as e:  # noqa: BLE001
                snmp_state[sid] = {"name": h["name"], "ok": False, "at": int(t0), "error": str(e)[:200], "snmp": snmp_summary(h)}
                print(f"{h['name']}: {e}", flush=True)
                continue
            mono = time.monotonic()
            uptime = vals.get("1.3.6.1.2.1.1.3.0")
            for i, n in idx.items():
                c32 = i in low32
                rx_o = vals.get(f"1.3.6.1.2.1.2.2.1.10.{i}" if c32 else f"1.3.6.1.2.1.31.1.1.1.6.{i}")
                tx_o = vals.get(f"1.3.6.1.2.1.2.2.1.16.{i}" if c32 else f"1.3.6.1.2.1.31.1.1.1.10.{i}")
                st = vals.get(f"1.3.6.1.2.1.2.2.1.8.{i}")
                if not c32 and rx_o is None and st is not None:
                    low32.add(i)                       # the device answers, but has no 64-bit counters for this port
                key = (sid, n)
                # port up/down changes -> activity feed + flap counter
                if st in (1, 2):
                    was = prev_st.get(key)
                    if was in (1, 2) and was != st:
                        flap_t.setdefault(key, deque()).append(t)
                        changes.append((t, sid, str(n), st))      # for link-state correlation
                        imp = any("important" in x.get("flags", []) for x in data["nodes"]
                                  if x.get("port_ref") == [sid, str(n)])
                        evs.append(ev("port", "ok" if st == 1 else ("bad" if imp else "info"),
                                      f"{port_name.get(key, n)} is {'up' if st == 1 else 'down'}",
                                      next((x["id"] for x in data["nodes"] if x.get("port_ref") == [sid, str(n)]
                                            and x["kind"] != "switch"), None)))
                    prev_st[key] = st
                if rx_o is None or tx_o is None:
                    continue
                cur = (mono, rx_o, tx_o, uptime, vals.get(f"1.3.6.1.2.1.31.1.1.1.19.{i}"), c32)
                rx, tx, drx, dtx, why = counter_rate(prev.get(key), cur, speeds.get(i, 0))
                prev[key] = cur
                if why == "impossible":                  # maybe the port just moved to a higher speed: ask the switch
                    new_sp = recheck_speed(auth, target, i)
                    if new_sp and new_sp != speeds.get(i):
                        live_speed[(sid, i)] = speeds[i] = new_sp
                        if max(rx, tx) <= new_sp * 1e6 * 1.05:
                            why = None
                if why == "reboot" and t - rescan_at.get(sid, 0) > 600:
                    rescan_at[sid] = t                   # port numbers may have changed: scan again now
                    for k_ in [k_ for k_ in live_speed if k_[0] == sid]:
                        live_speed.pop(k_)
                    if sched.start("scan", f"{h['name']} restarted"):
                        evs.append(ev("port", "info", f"{h['name']} restarted: scanning again for its ports", "s-" + sid))
                if why is None:
                    ports.setdefault(sid, {})[n] = {"rx": rx, "tx": tx, "st": st or 0}
                    for per in ("d", "m"):
                        u = usage.setdefault(per, {}).setdefault(sid, {}).setdefault(n, [0, 0])
                        u[0] += drx
                        u[1] += dtx
                elif why in ("impossible", "reboot", "discontinuity", "reset"):
                    q_ = quality.setdefault(sid, {}).setdefault(n, {"ignored": 0})
                    q_["ignored"] += 1
                    q_["last"], q_["at"] = why, t
                    if why == "impossible" and t - rej_said.get(key, 0) > 6 * 3600:
                        rej_said[key] = t
                        evs.append(ev("traffic", "info", f"Ignored an impossible traffic reading on {port_name.get(key, n)}: "
                                                         f"{mbps_txt(max(rx, tx))} on a {speeds.get(i, 0)} Mbps link",
                                      next((x["id"] for x in data["nodes"] if x.get("port_ref") == [sid, str(n)]
                                            and x["kind"] != "switch"), None)))
        for q in flap_t.values():
            while q and q[0] < t - 3600:
                q.popleft()
        flaps = {}
        for (sid, n), q in flap_t.items():
            if q:
                flaps.setdefault(sid, {})[n] = len(q) // 2 + len(q) % 2
        for sid, pp in ports.items():
            for n, v in pp.items():
                lst = hist["ports"].setdefault(sid, {}).setdefault(n, [])
                lst.append([t, round(v["rx"]), round(v["tx"])])
                del lst[:-RT_KEEP]
        root = next((n for n in data["nodes"] if n.get("kind") == "switch" and n.get("flip") and n.get("port_ref")), None)
        if update_hints(hints, list(changes), t):
            save_json(HINTS_FILE, hints)
        for sid_, pp in ports.items():
            for pid_, v in pp.items():
                if v.get("rx") is not None:
                    series.setdefault((sid_, pid_), deque(maxlen=12)).append((t, v["rx"], v["tx"]))
        if t - last_corr >= 30 and setting("topo_traffic"):
            last_corr = t
            meta, linked = {}, set()
            for n_ in data["nodes"]:
                if n_.get("kind") == "switch" and n_.get("port_ref"):
                    linked.add(frozenset((n_["sw"], n_["port_ref"][0])))
            known = {tuple(n_["port_ref"]) for n_ in data["nodes"] if n_.get("kind") == "switch" and n_.get("port_ref")}
            for sid_, x in (data.get("switches") or {}).items():
                for p in x.get("ports", []):
                    pid_ = str(p.get("id", p.get("n")))
                    meta[(sid_, pid_)] = {"speed": p.get("speed_mbps"), "up": p.get("status") == "up",
                                          "known": (sid_, pid_) in known or p.get("kind") == "lag"}
            if update_traffic_hints(hints, {k: list(v) for k, v in series.items()}, meta, linked, t):
                save_json(HINTS_FILE, hints)
        sw_wan = ports.get(root["port_ref"][0], {}).get(str(root["port_ref"][1])) if root else None
        wan, wan_src, wan_err = measure_wan(S, data, hosts, sw_wan, upnp, rsnmp, usage, t0)
        # tell once when the WAN cannot be measured (after a minute), and when it can again
        if S["wan_source"] == "router":
            if wan_src is None and not wan_bad.get("since"):
                wan_bad["since"] = t0
            if wan_src is None and t0 - wan_bad.get("since", t0) >= 60 and not wan_bad.get("said"):
                wan_bad["said"] = True
                evs.append(ev("internet", "warn", f"WAN throughput cannot be measured: {wan_err}", "inet"))
            if wan_src is not None:
                if wan_bad.get("said"):
                    evs.append(ev("internet", "ok", f"WAN throughput is measured again (router, {wan_src.upper()})", "inet"))
                wan_bad.clear()
        if wan:
            hist["wan"].append([t, round(wan["rx"]), round(wan["tx"])])
            del hist["wan"][:-RT_KEEP]

        # internet + gateway: latency, packet loss, jitter, outages (every 15 s)
        if t0 - last_lat >= 15:
            gw = next((n for n in data["nodes"] if n.get("kind") == "router" and n.get("ip")), None)
            tl = ([("Gateway", gw["ip"])] if gw else []) + LAT_TARGETS
            with cf.ThreadPoolExecutor(len(tl)) as ex:
                targets = dict(zip([x[0] for x in tl], ex.map(lambda x: ping_stats(x[1]), tl)))
            last_lat = t0
            net = [v for k, v in targets.items() if k != "Gateway"]
            up = any(v["ms"] is not None for v in net)
            if not up:                                  # all failed: check again right away before calling it an outage
                with cf.ThreadPoolExecutor(len(LAT_TARGETS)) as ex:
                    up = any(r["ms"] is not None for r in ex.map(lambda x: ping_stats(x[1], 3), LAT_TARGETS))
            loss = sum(v["loss"] for v in net) / max(1, len(net))
            jit = [v["jit"] for v in net if v["jit"] is not None]
            ms = [v["ms"] for v in net if v["ms"] is not None]
            hist["lat"].append([t, round(sum(ms) / len(ms), 1) if ms else None, round(loss, 1),
                                round(sum(jit) / len(jit), 1) if jit else None])
            del hist["lat"][:-240]
            change = inet_update(inet, up, t)
            if change == "down":
                evs.append(ev("internet", "bad", "Internet connection lost", "inet"))
            elif change:
                evs.append(ev("internet", "ok", f"Internet back after {_dur(change[1])}", "inet"))
            gwv = targets.get("Gateway")
            if gwv:
                gw_fails, change = gateway_step(gw_fails, gwv["ms"] is not None)
                if change == "down":
                    evs.append(ev("internet", "bad", f"Gateway {gw['name']} is not responding", gw["id"]))
                if change == "up":
                    evs.append(ev("internet", "ok", f"Gateway {gw['name']} is responding again", gw["id"]))
            if up:
                loss_win.append(loss)
            if len(loss_win) == loss_win.maxlen:
                avg = sum(loss_win) / len(loss_win)
                if avg >= 5 and not inet.get("loss_warn"):
                    inet["loss_warn"] = True
                    evs.append(ev("internet", "warn", f"Packet loss to the internet: {avg:.0f}% over the last 2 minutes", "inet"))
                if inet.get("loss_warn") and max(loss_win) < 1:
                    inet["loss_warn"] = False
                    evs.append(ev("internet", "ok", "Packet loss back to normal", "inet"))
            inet["loss"] = round(sum(loss_win) / len(loss_win), 1) if loss_win else 0
            save_json(INET_FILE, inet)

        # speed test: on request from the page, or daily at the time set in devices.json
        cfg = load_json(DEVICES_FILE, {}).get("settings", {})
        daily = cfg.get("speedtest_daily", "")
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        due = speedtest_due(daily, datetime.datetime.now(), daily_done)
        if (os.path.exists(SPEED_REQ) or due) and not speed["running"]:
            req = {}
            try:
                with open(SPEED_REQ, encoding="utf-8") as f:
                    req = json.loads(f.read() or "{}")
            except (OSError, ValueError):
                pass
            try:
                os.remove(SPEED_REQ)          # always, also when it could not be read: otherwise it would repeat
            except FileNotFoundError:
                pass
            by = req.get("by") if isinstance(req, dict) else None
            if by:
                audit("speedtest", by, {})
                evs.append(ev("audit", "info", f"Speed test started by {by}", "inet"))
            if due:
                daily_done = today
                save_json(SCHED_FILE, {"speedtest_daily_last": today})
            zh = next((h for h in hosts if h["_role"] == "zabbix"), None)
            threading.Thread(target=run_speedtest, args=(api, zh, speed), daemon=True).start()
        add_events(evs)
        if t0 - last_save > 60:
            save_json(USAGE_FILE, usage)
            last_save = t0
        sh = load_json(SPEED_FILE, [])
        upt, outs, cov = month_uptime(inet, t)
        save_json(os.path.join(DATA_DIR, RT), {
            "t": t, "every": every, "ports": ports, "flaps": flaps,
            "wan": {"down": wan["rx"], "up": wan["tx"], "src": wan_src} if wan else None,
            "suggestions": link_suggestions(hints, data, load_json(DEVICES_FILE, {}).get("links", [])),
            "wan_state": {"mode": S["wan_source"], "via": wan_src, "error": wan_err,
                          "router": rsnmp.get("router") if wan_src == "snmp" else (upnp["svc"] or {}).get("name", ""),
                          "interface": rsnmp.get("ifname", "") if wan_src == "snmp" else ""},
            "latency": {k: v["ms"] for k, v in targets.items() if k != "Gateway"}, "targets": targets,
            "inet": {"up": not inet.get("down_since"), "down_since": inet.get("down_since"),
                     "last_outage": inet["outages"][-1] if inet["outages"] else None,
                     "outages_month": outs[-30:], "uptime_month": upt, "since": inet.get("since"), "monitored": cov,
                     "loss_warn": inet.get("loss_warn", False), "loss": inet.get("loss", 0)},
            "usage": {"day": usage.get("day"), "month": usage.get("month"), "d": usage.get("d", {}), "m": usage.get("m", {}),
                      "wan": usage.get("wan"), "wan_since": usage.get("wan_since"),
                      "since": usage.get("since")},
            "quality": quality,
            "hist": hist, "speedtest": {"running": speed["running"], "started": speed.get("started"),
                                        "error": speed.get("error", ""), "last": sh[-1] if sh else None,
                                        "history": sh[-12:]}})
        time.sleep(max(0.5, every - (time.time() - t0)))


def service_unit(u, hardened=True):
    """systemd unit for the SpiderMap service. u: pwd entry of the service user, or None for root."""
    who = f"User={u.pw_name}\nGroup={u.pw_name}\nEnvironment=HOME={u.pw_dir}\n" if u else "Environment=HOME=/root\n"
    caps = ("# only what ping needs\nAmbientCapabilities=CAP_NET_RAW\nCapabilityBoundingSet=CAP_NET_RAW\n"
            "NoNewPrivileges=yes\n") if u else ""
    fs = ("ProtectSystem=strict\nReadWritePaths=/var/lib/spidermap /etc/spidermap\nProtectHome=yes\nPrivateTmp=yes\n"
          if hardened else "")
    return f"""[Unit]
Description=SpiderMap network map (scans, live traffic, speed tests)
After=network-online.target
Wants=network-online.target

[Service]
{who}WorkingDirectory={HERE}
ExecStart={sys.executable} {os.path.join(HERE, "spidermap.py")} --service
Restart=always
RestartSec=10
UMask=0027
{caps}{fs}
[Install]
WantedBy=multi-user.target
"""


def _systemctl(*a):
    return subprocess.run(["systemctl", *a], capture_output=True, text=True)


def install_service():
    """Install or update the service (root). Removes the older spidermap-rt service; falls back to a unit without
    file-system protection if this container does not support it."""
    u = service_user()
    if _systemctl("list-unit-files", OLD_SERVICE).stdout.count(OLD_SERVICE):
        _systemctl("disable", "--now", OLD_SERVICE)
        for f in ("/etc/systemd/system/" + OLD_SERVICE,):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        shutil.rmtree("/etc/systemd/system/" + OLD_SERVICE + ".d", ignore_errors=True)
        print("  old spidermap-rt service replaced by spidermap")
    for hardened in (True, False):
        with open(SERVICE, "w", encoding="utf-8") as f:
            f.write(service_unit(u, hardened))
        _systemctl("daemon-reload")
        _systemctl("enable", "spidermap.service")
        _systemctl("restart", "spidermap.service")
        time.sleep(6)
        if _systemctl("is-active", "spidermap.service").stdout.strip() == "active":
            print(f"SpiderMap service running as {u.pw_name if u else 'root'}"
                  + ("" if hardened else " (without file-system protection: not supported in this container)"))
            return True
        if hardened:
            print("  the service did not start with file-system protection - trying without")
    print("!! The SpiderMap service does not start. See: journalctl -u spidermap -n 30")
    return False


# --------------------------------------------------------------------------- #
# Zabbix integration: trapper items + triggers on the Zabbix server host
# --------------------------------------------------------------------------- #
EVENT_ITEMS = [("autotopo.new", "Network: new devices", 2, "Network: new device(s): {ITEM.LASTVALUE1}"),
               ("autotopo.changed", "Network: changed devices", 1, "Network: device moved or changed IP: {ITEM.LASTVALUE1}"),
               ("autotopo.down", "Network: important devices down", 4, "Network: important device(s) down: {ITEM.LASTVALUE1}"),
               ("autotopo.warnings", "Network: active warnings", 2, "Network warning: {ITEM.LASTVALUE1}")]


def zabbix_host(hosts):
    return next((h for h in hosts if h["_role"] == "zabbix"), None)


def ensure_event_items(api, zh):
    ids = {}
    for key, name, prio, desc in EVENT_ITEMS:
        it = api.call("item.get", {"hostids": zh["hostid"], "output": ["itemid"], "filter": {"key_": key}})
        if it:
            ids[key] = it[0]["itemid"]
            continue
        ids[key] = api.call("item.create", {"hostid": zh["hostid"], "name": name, "key_": key, "type": 2,
                                            "value_type": 4, "history": "30d",
                                            "tags": [{"tag": "source", "value": "autotopo"}]})["itemids"][0]
        api.call("trigger.create", {"description": desc, "priority": prio, "manual_close": 1,
                                    "expression": f'last(/{zh["host"]}/{key})<>"none"',
                                    "tags": [{"tag": "source", "value": "autotopo"}]})
        print(f"  + item and trigger {key}")
    return ids


def push_events(api, hosts, events, down):
    zh = zabbix_host(hosts)
    if not zh:
        return
    ids = ensure_event_items(api, zh)
    vals = {"autotopo.new": "; ".join(events["new"]) or "none",
            "autotopo.changed": "; ".join(events["changed"]) or "none",
            "autotopo.down": ", ".join(down) or "none"}
    vals = {k: (v[:480] + "…" if len(v) > 480 else v) for k, v in vals.items()}
    try:
        api.call("history.push", [{"itemid": ids[k], "value": v} for k, v in vals.items()])
    except Exception as e:  # noqa: BLE001
        if shutil.which("zabbix_sender"):
            for k, v in vals.items():
                subprocess.run(["zabbix_sender", "-z", "127.0.0.1", "-s", zh["host"], "-k", k, "-o", v],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print(f"  ! could not send events to Zabbix: {e}")


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
def download(url, path):
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200 and r.content:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(r.content)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


GATEWAY = "spidermap.php"   # the only SpiderMap entry point in the web folder

REDIRECT_MARK = "<!-- SpiderMap redirect -->"


def write_if_changed(path, text, mode=0o644):
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


def save_json_text(path, text):
    """Write a text file into the protected data folder (root + web server group only)."""
    ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o640)
    gid = _web_gid()
    if gid is not None:
        try:
            os.chown(tmp, -1, gid)
        except OSError:
            pass
    os.replace(tmp, path)


def redirect_stub(target):
    return (f'{REDIRECT_MARK}<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={target}">'
            f'<a href="{target}">SpiderMap has moved here</a>')


def web_user():
    import pwd
    for u in ("www-data", "nginx", "apache", "http"):
        try:
            return pwd.getpwnam(u)
        except KeyError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Settings that can be changed from the map (Settings panel). One table drives the validation, the defaults and
# the form on the page. Other keys in devices.json "settings" (port_media, aliases, ...) are kept as they are.
# --------------------------------------------------------------------------- #


_SET_CACHE = {"mtime": None, "eff": None, "raw": {}}


def current_settings():
    """(effective schema settings, raw settings) from devices.json, re-read only when the file changed."""
    try:
        mt = os.path.getmtime(DEVICES_FILE)
    except OSError:
        mt = None
    if _SET_CACHE["eff"] is None or mt != _SET_CACHE["mtime"]:
        raw = load_json(DEVICES_FILE, {}).get("settings", {}) or {}
        _SET_CACHE.update(mtime=mt, raw=raw, eff=effective_settings(raw))
    return _SET_CACHE["eff"], _SET_CACHE["raw"]


def setting(key):
    return current_settings()[0][key]


def _get(settings, key, default=None):
    if "." in key:
        a, b = key.split(".", 1)
        return (settings.get(a) or {}).get(b, default)
    return settings.get(key, default)


def install_data_page():
    """The page itself, in the protected data folder (served by spidermap.php). The service can do this."""
    ensure_data_dir()
    src = os.path.join(HERE, PAGE)
    if os.path.isfile(src):
        with open(src, encoding="utf-8") as f:
            text = f.read()
        try:
            with open(os.path.join(DATA_DIR, PAGE), encoding="utf-8") as f:
                if f.read() == text:
                    return
        except OSError:
            pass
        save_json_text(os.path.join(DATA_DIR, PAGE), text)


def install_page(webd):
    """Everything in the Zabbix web folder (root, from install.sh): spidermap.php, icons, redirects."""
    install_data_page()
    adir = os.path.join(webd, ASSETS)                         # icons only - nothing about your network
    os.makedirs(adir, exist_ok=True)
    os.chmod(adir, 0o755)
    for f in ("spidermap-icon.svg", "spidermap-icon-dark.svg", "spidermap-favicon.svg"):
        if os.path.isfile(os.path.join(HERE, "assets", f)):
            shutil.copyfile(os.path.join(HERE, "assets", f), os.path.join(adir, f))
            os.chmod(os.path.join(adir, f), 0o644)
    code = gateway_code(load_json(DEVICES_FILE, {}).get("settings", {}), at.ZABBIX_URL)
    write_if_changed(os.path.join(webd, GATEWAY), code)
    # old entry points become redirects, so bookmarks and dashboard widgets keep working
    write_if_changed(os.path.join(webd, PAGE), redirect_stub(GATEWAY))
    sec = os.path.join(webd, "spider-secure.php")
    if os.path.isfile(sec):
        with open(sec, encoding="utf-8", errors="ignore") as f:
            own = REDIRECT_MARK in f.read()
        if not own:
            os.makedirs(os.path.join(EDITS_DIR, "backup"), exist_ok=True)
            shutil.copy2(sec, os.path.join(EDITS_DIR, "backup", "spider-secure.php"))
            print(f"  spider-secure.php replaced by spidermap.php (backup: {EDITS_DIR}/backup/spider-secure.php)")
        write_if_changed(sec, "<?php " + "/* " + REDIRECT_MARK + " */ header('Location: " + GATEWAY + "', true, 302); exit;\n")
    # earlier versions served data or helpers straight from the web folder: remove them
    for f in ("spidermap_api.php", "spidermap_edit.php", "spidermap.json", "spidermap_live.json", "spidermap_rt.json",
              "spidermap_events.json", "spidermap_presence.json"):
        try:
            os.remove(os.path.join(webd, f))
        except FileNotFoundError:
            pass
    ensure_data_dir()
    os.makedirs(EDITS_DIR, exist_ok=True)
    os.makedirs(os.path.join(INBOX, "sess"), exist_ok=True)
    old = os.path.join(EDITS_DIR, "edits.json")              # earlier versions kept edits one folder up
    if os.path.isfile(old) and not os.path.isfile(EDITS_FILE):
        shutil.move(old, EDITS_FILE)
    if not os.path.isfile(EDITS_FILE):
        with open(EDITS_FILE, "w", encoding="utf-8") as f:
            f.write("{}")
    publish_settings()                    # permissions for spidermap.php, the Settings panel
    fix_ownership()


def migrate_old_install(api, webd):
    """Clean up after zabbix-netmap (the old name) so nothing is left behind."""
    for d in api.call("dashboard.get", {"output": ["dashboardid"], "filter": {"name": OLD_DASHBOARDS}}):
        api.call("dashboard.delete", [d["dashboardid"]])
        print("  removed old dashboard (replaced by 'SpiderMap')")
    old_page = os.path.join(webd, "netmap.html")
    if os.path.isfile(old_page):
        with open(old_page, "w", encoding="utf-8") as f:   # old bookmarks keep working
            f.write(redirect_stub(GATEWAY))
        for f in ("netmap.json", "netmap_live.json", "netmap_rt.json", "netmap_events.json",
                  "netmap_presence.json", "netmap_edit.php"):
            try:
                os.remove(os.path.join(webd, f))
            except FileNotFoundError:
                pass
        shutil.rmtree(os.path.join(webd, "netmap-assets"), ignore_errors=True)
        print("  old netmap.html now redirects to spidermap.php")
    old_speed = "/var/lib/netmap/speedtest.json"
    if os.path.isfile(old_speed) and not os.path.isfile(SPEED_FILE):
        os.makedirs(EDITS_DIR, exist_ok=True)
        shutil.copyfile(old_speed, SPEED_FILE)


def setup(api):
    webd = webdir()
    if not webd:
        raise SystemExit("Zabbix web folder not found.")
    if not os.path.isfile(DEVICES_FILE):
        save_json(DEVICES_FILE, DEFAULT_DEVICES)
        print(f"Created {DEVICES_FILE} (your names, icons, pins)")
    a = os.path.join(webd, ASSETS)
    print("Downloading icons (once, stored locally)...")
    ok = download(TABLER + "tabler-icons.min.css", os.path.join(a, "tabler-icons.min.css"))
    for ext in ("woff2", "woff", "ttf"):
        download(TABLER + f"fonts/tabler-icons.{ext}", os.path.join(a, "fonts", f"tabler-icons.{ext}"))
    print("  device icons:", "ok" if ok else "FAILED (the page will fall back to the internet copy)")
    got = [s for s in sorted(set(LOGOS.values())) if download(LOGO_URL.format(s), os.path.join(a, "logos", s + ".svg"))]
    print(f"  app logos: {len(got)} ({', '.join(got)})")
    for root, dirs, files in os.walk(a):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)
    os.chmod(a, 0o755)
    install_page(webd)
    hosts = at.load_hosts(api)
    zh = zabbix_host(hosts)
    if zh:
        ensure_event_items(api, zh)
    migrate_old_install(api, webd)
    if not api.call("dashboard.get", {"output": ["dashboardid"], "filter": {"name": DASHBOARD_NAME}}):
        d = at.Dash(api, DASHBOARD_NAME, ["Topology"])
        d.add("Topology", "url", "Network", 0, 0, 72, 20, [{"url": (1, GATEWAY)}])
        print(f"Dashboard created: {DASHBOARD_NAME}")
    if not os.path.isfile(OOKLA) and not shutil.which("speedtest-cli"):
        print("For speed tests: bash install.sh --update (installs a speed test program)")
    fix_ownership()
    install_service()
    print("\nSetup done. The service runs the first scan now; the map appears within a few minutes.")


OPER = {"1": "up", "2": "down", "3": "testing", "5": "dormant", "6": "not present", "7": "lower layer down"}


def check_ports_report(sw_name, names, types, speeds, opers, hw_media=None, media_override=None):
    """Text report for one switch: every interface, its SpiderMap label (or why it is not a port),
    and the interfaces that shared one identity in SpiderMap 0.2.x."""
    order = sorted(names, key=lambda x: int(x) if x.isdigit() else 0)
    ports = []
    for idx in order:
        kind, _ = zp.iface_kind(names[idx], types.get(idx, ""))
        if kind:
            ports.append({"ifindex": idx, "name": names[idx], "kind": kind, "media_hw": (hw_media or {}).get(idx, "")})
    label_ports(ports, media_override)
    labels = {p["ifindex"]: p["label"] for p in ports}
    lines = [f"{sw_name} - {len(ports)} ports",
             f"  {'ifIndex':>8}  {'Interface':30} {'Type':>5}  {'Status':17} {'Speed':>9}   SpiderMap"]
    for idx in order:
        kind, why = zp.iface_kind(names[idx], types.get(idx, ""))
        sp = int(zp.num(speeds.get(idx, "")) or 0)
        spd = (f"{sp / 1000:g} Gbps" if sp >= 1000 else f"{sp} Mbps") if sp else "-"
        st = OPER.get(zp.num(opers.get(idx, "")), zp.num(opers.get(idx, "")) or "-")
        lines.append(f"  {idx:>8}  {names[idx][:30]:30} {zp.num(types.get(idx, '')):>5}  {st:17} {spd:>9}   "
                     + (labels[idx] if kind else f"- ({why})"))
    old = {}                     # 0.2.x: Ethernet/LAG types, identified by the digits at the end of the name
    for idx in order:
        if zp.num(types.get(idx, "")) in zp.PHYS_TYPES:
            old.setdefault(port_num(names[idx]), []).append(names[idx])
    clashes = [v for v in old.values() if len(v) > 1]
    if clashes:
        lines.append("  Fixed in 0.3.0 - these interfaces shared one identity in 0.2.x (wrong traffic and totals):")
        lines += [f"    {' + '.join(v)}" for v in clashes]
    else:
        lines.append("  No conflicts.")
    return "\n".join(lines), bool(clashes)


def check_ports(api, community=None):
    hosts = [h for h in at.load_hosts(api) if h["_snmp"] and str(h["status"]) == "0"]
    if not hosts:
        print("No enabled hosts with an SNMP interface in Zabbix.")
    for h in hosts:
        try:
            auth, target = at.snmp_auth(h["_snmp"], h["_macros"], community)
            auth = auth + ["-Oe"]
            w = lambda k: at.snmpwalk(auth, target, zp.OID[k])  # noqa: E731
            names, types, speeds, opers = w("name"), w("type"), w("speed"), w("oper")
            hw = zp.entity_media(auth, target)
        except Exception as e:  # noqa: BLE001
            print(f"\n{h['name']}: cannot read it over SNMP ({e})")
            continue
        if not names:
            print(f"\n{h['name']}: no interfaces returned (check SNMP access to IF-MIB)")
            continue
        pm = load_json(DEVICES_FILE, {}).get("settings", {}).get("port_media", {})
        text, _ = check_ports_report(f"{h['name']} ({target.split(':')[1]})", names, types, speeds, opers, hw,
                                     pm.get(h["name"]) or pm.get(h["host"]) or {})
        print("\n" + text)


# --------------------------------------------------------------------------- #
# Uninstall (called by install.sh --uninstall): only what SpiderMap itself created
# --------------------------------------------------------------------------- #
ZBX_DASHBOARDS = (DASHBOARD_NAME, at.DASHBOARD_NAME)      # "SpiderMap", and the classic tool's dashboard
ZBX_MAPS = (at.MAP_NAME,)                                 # the classic tool's map


def uninstall_zabbix(api, dry=False):
    """Items with keys "autotopo.*" (their triggers go with them), SpiderMap's dashboards and map. Returns problems."""
    items = api.call("item.get", {"output": ["itemid", "key_", "hostid"], "search": {"key_": "autotopo."},
                                  "startSearch": True, "templated": False})
    dash = api.call("dashboard.get", {"output": ["dashboardid", "name"], "filter": {"name": list(ZBX_DASHBOARDS)}})
    maps = api.call("map.get", {"output": ["sysmapid", "name"], "filter": {"name": list(ZBX_MAPS)}})
    print(f"  Zabbix: {len(items)} items (and their triggers): {', '.join(sorted({i['key_'] for i in items})) or '-'}")
    print(f"  Zabbix: dashboards: {', '.join(d['name'] for d in dash) or '-'}; maps: {', '.join(m['name'] for m in maps) or '-'}")
    if dry:
        return []
    problems = []
    for method, ids in (("item.delete", [i["itemid"] for i in items]), ("dashboard.delete", [d["dashboardid"] for d in dash]),
                        ("map.delete", [m["sysmapid"] for m in maps])):
        if ids:
            try:
                api.call(method, ids)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{method}: {e} - remove them in Zabbix by hand, or allow {method} for the API token")
    return problems


def uninstall_web(webd, dry=False):
    """spidermap.php, the icons, and the redirect files SpiderMap wrote (recognised by their mark, nothing else)."""
    gone = []
    for name in (GATEWAY, PAGE, "netmap.html", "spider-secure.php"):
        f = os.path.join(webd, name)
        if not os.path.isfile(f):
            continue
        with open(f, encoding="utf-8", errors="ignore") as fh:
            text = fh.read(200_000)
        if name == GATEWAY and "SpiderMap" not in text:
            continue                                     # not ours
        if name != GATEWAY and REDIRECT_MARK not in text:
            continue
        gone.append(f)
    assets = os.path.join(webd, ASSETS)
    if os.path.isdir(assets):
        gone.append(assets + "/")
    print("  web folder: " + (", ".join(os.path.basename(g.rstrip("/")) for g in gone) or "nothing of SpiderMap's"))
    if not dry:
        for g in gone:
            if g.endswith("/"):
                shutil.rmtree(g, ignore_errors=True)
            else:
                os.remove(g)
    return gone


def job_lock(name, wait):
    """One job of each kind at a time. wait=False: return None if it is already running."""
    import fcntl
    fh = open(os.path.join(STATE_DIR, f".spidermap-{name}.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        fh.close()
        return None
    return fh


# ---- the parts kept in their own modules (they use the helpers above) ----
from spidermap_infer import mac_switch_links, CLASSIFY, DEVICE_CATEGORIES, DEVICE_CATEGORY_KEYS, HINTS_FILE, PLACE_KINDS, VIA_GUESSED, VIA_TEXT, classify, link_state_pairs, link_suggestions, traffic_match, update_hints, update_traffic_hints  # noqa: E402,F401
from spidermap_settings import SECRETS_FILE, SECRET_KEYS, SETTINGS_BY_KEY, SETTINGS_KEYS, SETTINGS_SCHEMA, _S, effective_settings, load_secrets, permissions, save_secrets, scan_target_count, scan_targets, validate_settings  # noqa: E402,F401
from spidermap_speed import CF_BASE, FAST_API, FAST_HOME, OOKLA_SERVERS, SPEED_SECONDS, _latency, _speedtest_cloudflare, _speedtest_fast, _speedtest_once, _transfer, ookla_servers  # noqa: E402,F401
from spidermap_wan import upstream_router, GAP_S, UPNP_SEARCH, UPNP_STUCK, _overlap, inet_start, inet_update, measure_wan, month_uptime, read_router_snmp, read_upnp, router_wan_ifindex, unifi_fetch, upnp_call, upnp_counters, upnp_discover, upnp_link_mbps, upnp_wan_service  # noqa: E402,F401
from spidermap_gateway import GATEWAY_PHP, gateway_code  # noqa: E402,F401


def main():
    ap = argparse.ArgumentParser(description="UniFi-style live network topology for Zabbix")
    ap.add_argument("--setup", action="store_true", help="install page, icons, Zabbix items, dashboard")
    ap.add_argument("--live", action="store_true", help="refresh live status and traffic only")
    ap.add_argument("--service", "--rt", dest="rt", action="store_true", help="run the service (scheduler, live traffic)")
    ap.add_argument("--install-web", action="store_true", help="install spidermap.php and icons in the web folder (root)")
    ap.add_argument("--uninstall-zabbix", action="store_true", help="remove SpiderMap's items, triggers, dashboards and map from Zabbix")
    ap.add_argument("--uninstall-web", action="store_true", help="remove SpiderMap's files from the Zabbix web folder (root)")
    ap.add_argument("--dry-run", action="store_true", help="with --uninstall-*: only show what would be removed")
    ap.add_argument("--version", action="version", version=f"SpiderMap {__version__}")
    ap.add_argument("--check-ports", action="store_true", help="show how SpiderMap sees every switch port (read-only)")
    ap.add_argument("--install-service", action="store_true", help="install/restart the real-time service")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--community", help="SNMP v1/v2c community override")
    args = ap.parse_args()
    if args.uninstall_web:                  # needs no token: it only removes files
        webd = webdir()
        if webd:
            uninstall_web(webd, args.dry_run)
        return
    if not at.ZABBIX_TOKEN:
        raise SystemExit(f"No Zabbix API token: check {os.path.join(CONF_DIR, 'zabbix.env')}")
    if args.uninstall_zabbix:
        problems = uninstall_zabbix(at.Zabbix(at.ZABBIX_URL, at.ZABBIX_TOKEN, at.VERIFY_TLS), args.dry_run)
        for p_ in problems:
            print("  !", p_)
        raise SystemExit(1 if problems else 0)
    if args.install_web:
        webd = webdir()
        if not webd:
            raise SystemExit("Zabbix web folder not found.")
        install_page(webd)
        print(f"Web files installed in {webd}")
        return
    if args.install_service:
        install_service()
        return
    api = at.Zabbix(at.ZABBIX_URL, at.ZABBIX_TOKEN, at.VERIFY_TLS)
    if args.setup:
        setup(api)
        return
    drop_privileges()                    # everything below runs as the service user
    if args.check_ports:
        check_ports(api, args.community)
        return
    if args.rt:
        rt_loop(api)
        return
    if args.live:
        lock = job_lock("live", wait=False)
        if lock is None:
            return                       # the previous minute's refresh is still running
        with lock:
            live_refresh(api)
        return
    # cron (--quiet) skips a scan while the previous one still runs; by hand / installer waits for it
    lock = job_lock("scan", wait=False)
    if lock is None:
        if args.quiet:
            print(f"{datetime.datetime.now():%Y-%m-%d %H:%M} previous scan still running - skipped", flush=True)
            return
        print("Another scan is running - waiting for it to finish...", flush=True)
        lock = job_lock("scan", wait=True)
    with lock:
        events, down, hosts = full_scan(api, args.community, args.quiet)
    push_events(api, hosts, events, down)
    lock = job_lock("live", wait=False)
    if lock is not None:
        with lock:
            live_refresh(api)


if __name__ == "__main__":
    main()
