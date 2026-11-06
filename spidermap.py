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

import spidermap_zabbix as at
import spidermap_ports as zp

__version__ = "0.6.0"   # keep in sync with PAGE_VERSION in spidermap.html and CHANGELOG.md

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

# Device categories (Settings > Device types). Specific ones come before general ones: the first match wins.
# key, types it gives, title, examples, how it is detected, regex on "name dns-name manufacturer"
DEVICE_CATEGORIES = [
    ("oob", "oob", "Out-of-band management", "iLO, iDRAC, IPMI, console servers",
     "Name (ilo-, idrac-, ipmi, bmc) and manufacturer (Opengear, Avocent, Lantronix)",
     r"\bilo\b|ilo[-_]|idrac|ipmi|\bbmc\b|bmc[-_]|xclarity|\bimm[-_]|opengear|avocent|lantronix|console.?server"),
    ("firewall", "firewall", "Firewalls & gateways", "pfSense, OPNsense, FortiGate, Sophos",
     "Name and manufacturer (Fortinet, Sophos, SonicWall, WatchGuard, Netgate)",
     r"pfsense|opnsense|fortigate|fortinet|sophos|sonicwall|watchguard|netgate|firewall|\bfw\b|fw[-_]"),
    ("ont", "ont", "ONT / provider modem", "Fibre routers, VDSL, cable",
     "Name (ont, modem) and manufacturer (Sagemcom, Technicolor, ZTE, AVM, Genexis, Nokia)",
     r"\bont\b|ont[-_]|modem|fritz|sagemcom|technicolor|genexis|\bzte\b|nokia.*(g-|ont)"),
    ("ups", "ups", "UPS & smart PDU", "APC, Eaton, CyberPower, Riello, Raritan",
     "Name (ups, pdu) and manufacturer (APC / Schneider, Eaton, CyberPower, Riello, Raritan, Vertiv)",
     r"\bups\b|ups[-_]|american power|\bapc\b|eaton|cyberpower|riello|\bpdu\b|pdu[-_]|raritan|vertiv|netbotz"),
    ("camera", "camera", "IP cameras & NVR", "Hikvision, Dahua, Axis, UniView, Reolink",
     "Name (cam, ipc, nvr, dvr) and manufacturer",
     r"\bipc\b|ipc[-_.]|cam|cctv|\bnvr\b|nvr[-_]|\bdvr\b|hikvision|dahua|reolink|tapo|vigi|imou|ezviz|axis comm|uniview|amcrest|hanwha"),
    ("voip", "voip", "IP phones & PBX", "Yealink, Grandstream, Snom, 3CX",
     "Name (sip, voip, pbx) and manufacturer (Yealink, Grandstream, Snom, Fanvil, Poly, Gigaset)",
     r"yealink|grandstream|snom|fanvil|polycom|\bpoly\b|gigaset|cisco.?ip.?phone|\bsip[-_]|voip|\bpbx\b|pbx[-_]|3cx|asterisk|freepbx"),
    ("printer", "printer", "Printers", "All network printers", "Name and manufacturer (Epson, Brother, Canon, HP, Lexmark)",
     r"epson|printer|brother|canon|lexmark|laserjet|officejet|deskjet|kyocera|ricoh|xerox"),
    ("nas", "nas", "NAS & storage", "Synology, QNAP, TrueNAS, OpenMediaVault",
     "Name and manufacturer (Synology, QNAP)", r"\bomv\b|openmediavault|nas\b|synology|qnap|truenas|unraid"),
    ("hypervisor", "hypervisor", "Hypervisors", "Proxmox, VMware ESXi, Hyper-V, XCP-ng",
     "Name, manufacturer (VMware) and the Proxmox API: many VMs behind one physical port",
     r"proxmox|\bpve\d*\b|pve[-_]|esxi|vmware|vsphere|hyper-?v|xcp-?ng|xenserver|nutanix"),
    ("industrial", "industrial", "Industrial & IoT", "PLC, BMS, sensors (read only)",
     "Name and manufacturer (Siemens SIMATIC, Schneider, WAGO, Beckhoff, Moxa); only ever read, never written to",
     r"simatic|siemens.?s7|\bs7-|\bplc\b|modbus|bacnet|wago|beckhoff|phoenix.?contact|moxa|advantech|loytec|schneider elec"),
    ("mediaconv", "mediaconv", "Powerline & media converters", "devolo, TP-Link powerline, managed converters",
     "Name and manufacturer (devolo, powerline adapters, media converters)",
     r"devolo|powerline|homeplug|tl-?pa\d|tl-?wpa|media.?converter"),
]
DEVICE_CATEGORY_KEYS = [c[0] for c in DEVICE_CATEGORIES]

CLASSIFY = [  # (regex on "name vendor", type) - first match wins
    (r"cloud.?key|unifi|ubiquiti", "unifi"),
    (r"\bipc\b|ipc[-_.]|cam|cctv|hikvision|dahua|reolink|tapo|vigi|imou|ezviz", "camera"),
    (r"\btv\b|tv[-_.]|bravia|webos|tizen|chromecast|roku|firetv|fire-tv|appletv", "tv"),
    (r"laptop|macbook|notebook|thinkpad", "laptop"),
    (r"desktop|imac|workstation|\bpc\b", "desktop"),
    (r"iphone|ipad|android|oneplus|galaxy|pixel|xiaomi|redmi|poco|phone", "phone"),
    (r"epson|printer|brother|canon|lexmark|laserjet|officejet|deskjet", "printer"),
    (r"washer|dishwasher|bosch|dryer|fridge|oven|miele|siemens|lg-|whirlpool", "appliance"),
    (r"espressif|esp[-_]|esp\d|shelly|tasmota|sonoff|tuya|esphome|wled|tasmota", "iot"),
    (r"\bomv\b|openmediavault|nas\b|synology|qnap|truenas|unraid", "nas"),
    (r"proxmox|\bpve\b|server|dell|hewlett|\bhpe?\b|supermicro", "server"),
]
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


def classify(name, vendor, mac, off=()):
    """Device type from its name / DNS name and manufacturer. Categories switched off in Settings > Device types
    are skipped: their devices get the next matching type, or "unknown"."""
    text = f"{name} {vendor}".lower()
    if mac.startswith("bc:24:11"):
        return "guest"
    for key, typ, _t, _e, _d, rx in DEVICE_CATEGORIES:
        if key not in off and re.search(rx, text):
            return typ
    for rx, t in CLASSIFY:
        if t not in ("camera", "printer", "nas") and re.search(rx, text):     # those three are categories above
            return t
    return "phone" if is_private_mac(mac) else "unknown"


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
    at.SNMP_WALK.update(timeout_s=S["snmp_timeout_ms"] / 1000, retries=S["snmp_retries"], max_rep=S["snmp_max_repetitions"])
    nets = S["scan_subnets"] or sorted({str(ipaddress.ip_network(f"{h['_snmp']['ip']}/24", strict=False))
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
    vlan_names, mac_vlan = {}, {}
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
    ipmap = {m: v[0] for m, v in arp.items()}
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
    root_sw = gw_loc[0] if gw_loc else (next(iter(sw)) if sw else None)

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

    def device_node(mac, parent, port_ref=None, port_lbl=""):
        d, c, g = sdev.get(mac, {}), dcfg.get(mac, {}), guest_by_mac.get(mac)
        ip, name = d.get("ip", ""), dname(mac)
        vendor = vendor_of(mac)
        typ = c.get("type") or (g and g["kind"]) or classify(f"{name} {d.get('dns', '')}", vendor, mac, S["device_types_off"])
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
                port=port_lbl, port_ref=port_ref, vendor=vendor, conn=conn or "-", vlan=mac_vlan.get(mac),
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

    # switches in BFS order from the root switch
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
    for sid in order:
        h = sw[sid]["host"]
        up = sum(p["status"] == "up" for p in sw[sid]["ports"])
        if sid in parent_of:
            par, pa, pb = parent_of[sid]
            rb = next((p for p in sw[sid]["ports"] if zp.norm(p["name"]) == zp.norm(pb)), None)
            parent, lbl = "s-" + par, f"{plabel(par, pa)} ↔ {rb['label'].replace('Port ', '') if rb else pb}"
            pref = [par, pid_of(par, pa)]
            # a link aggregation: name the LAG, and draw the link as one thicker link
            mine = _pinfo.get((par, pa), {})
            agg = next((q for q in sw[par]["ports"] if mine.get("lag") and str(q.get("ifindex")) == str(mine["lag"])), None) \
                or (mine if mine.get("kind") == "lag" else None)
            if agg:
                members = [q["label"].replace("Port ", "") for q in sw[par]["ports"] if str(q.get("lag")) == str(agg.get("ifindex"))]
                lbl = f"{agg['label']}" + (f" ({'+'.join(members)})" if members else "") + f" ↔ {rb['label'].replace('Port ', '') if rb else pb}"
                link_kind = "lag"
            else:
                link_kind = "fiber" if mine.get("media") else ""
        else:
            link_kind = "fiber" if gw_loc and sid == root_sw and _pinfo.get((sid, gw_loc[1]), {}).get("media") else ""
            parent, lbl = gw_id, (plabel(sid, gw_loc[1]) if gw_loc and sid == root_sw else "")
            pref = [sid, pid_of(sid, gw_loc[1])] if gw_loc and sid == root_sw else None
        add("s-" + sid, parent, name=h["name"], sub="." + h["_snmp"]["ip"].split(".")[-1], icon="server-2",
            kind="switch", sw=sid, status="up", port=lbl, port_ref=pref, zbx=sid, flip=sid not in parent_of, link=link_kind,
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
        if len(live_macs) > 1 and (sid, pname) not in host_port:
            gid = f"g-{sid}-{pid_of(sid, pname)}"
            add(gid, "s-" + sid, name=f"{lbl} devices", sub=f"{len(macs)} devices", icon="sitemap", kind="group",
                status="up", port=lbl, port_ref=pr, details=[["Connection", f"{sw_name[sid]} · {lbl}"]])
            for m in macs:
                device_node(m, gid)
                placed.add(m)
        else:
            for m in macs:
                n = device_node(m, "s-" + sid, pr, lbl)
                port = next((p for p in sw[sid]["ports"] if p["name"] == pname), None)
                if n["status"] == "off" and port and port["status"] == "up":
                    n["status"], n["sub"] = "asleep", "asleep"
                placed.add(m)

    # devices behind another device (gateway Wi-Fi, Proxmox guests, ...)
    for host_mac, macs in behind.items():
        parent = "wifi" if host_mac == gw_mac else by_mac.get(host_mac)
        if not parent:
            continue
        for m in sorted(macs, key=lambda x: dname(x).lower()):
            if m not in placed:
                device_node(m, parent)
                placed.add(m)

    # stopped Proxmox guests the network never saw
    pve_node = next((n for n in nodes.values() if n.get("type") == "server" and "proxmox" in n["name"].lower()), None)
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
             "media": p.get("media", ""), "vlans": p.get("vlans") if S["topo_vlans"] else None,
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


AUDIT_FIELDS = ("name", "type", "url", "pin", "important")


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
            if v in ("", None, False) and k != "important":
                d.pop(k, None)
            else:
                d[k] = v
        if d.get("important") is False:
            d.pop("important")
        changes = {k: [before[k], d.get(k)] for k in AUDIT_FIELDS if (before[k] or None) != (d.get(k) or None)}
        if changes:
            by = e.get("by") or "?"
            audit("edit", by, {"mac": mac.lower(), "changes": changes})
            what = ", ".join(f"{k} “{old or '-'}” → “{new or '-'}”" if k != "important" else
                             ("marked important" if new else "no longer important") for k, (old, new) in changes.items())
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
UPNP_SEARCH = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1", "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
               "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1")


def upnp_discover(gateway_ip, timeout=3.0, port=1900, mcast="239.255.255.250"):
    """URL of the gateway's UPnP description, or None. Answers from any other address are ignored."""
    import socket
    from urllib.parse import urlparse
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    try:
        for st in UPNP_SEARCH:
            msg = (f'M-SEARCH * HTTP/1.1\r\nHOST: {mcast}:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\nST: {st}\r\n\r\n').encode()
            for dst in ((mcast, port), (gateway_ip, port)):
                try:
                    s.sendto(msg, dst)
                except OSError:
                    pass
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(8192)
            except socket.timeout:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if addr[0] == gateway_ip and m:
                loc = m.group(1).decode(errors="ignore")
                if urlparse(loc).hostname == gateway_ip and urlparse(loc).scheme in ("http", "https"):
                    return loc
    finally:
        s.close()
    return None


def upnp_wan_service(location):
    """The router's WANCommonInterfaceConfig service: {"type", "url", "name"} or None (same device only)."""
    from urllib.parse import urljoin, urlparse
    xml = requests.get(location, timeout=5).text
    base = re.search(r"(?is)<URLBase>\s*(.*?)\s*</URLBase>", xml)
    name = re.search(r"(?is)<modelName>\s*(.*?)\s*</modelName>", xml)
    for blk in re.findall(r"(?is)<service>(.*?)</service>", xml):
        st = re.search(r"(?is)<serviceType>\s*(.*?)\s*</serviceType>", blk)
        ctl = re.search(r"(?is)<controlURL>\s*(.*?)\s*</controlURL>", blk)
        if st and ctl and "WANCommonInterfaceConfig" in st.group(1):
            root = base.group(1) if base else location
            url = urljoin(root, ctl.group(1))
            if urlparse(url).hostname != urlparse(location).hostname:
                continue
            svc = {"type": st.group(1), "url": url, "name": name.group(1)[:60] if name else "",
                   "rx": "GetTotalBytesReceived", "tx": "GetTotalBytesSent"}
            # a 64-bit byte counter, where the router offers one (it does not stop at 2 or 4 GB)
            scpd = re.search(r"(?is)<SCPDURL>\s*(.*?)\s*</SCPDURL>", blk)
            if scpd:
                try:
                    actions = re.findall(r"(?is)<action>\s*<name>\s*([^<\s]+)\s*</name>",
                                         requests.get(urljoin(root, scpd.group(1)), timeout=5).text)
                except requests.RequestException:
                    actions = []
                for key, rx in (("rx", r"(?i)bytes.*rec.*64|64.*bytes.*rec"), ("tx", r"(?i)bytes.*sent.*64|64.*bytes.*sent")):
                    hit = next((a for a in actions if re.search(rx, a)), None)
                    if hit:
                        svc[key] = hit
                svc["actions"] = actions[:40]
            return svc
    return None


def upnp_call(svc, action):
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{svc["type"]}"/></s:Body></s:Envelope>')
    r = requests.post(svc["url"], data=body.encode(), timeout=3,
                      headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{svc["type"]}#{action}"'})
    r.raise_for_status()
    return r.text


UPNP_STUCK = {2 ** 31 - 1, 2 ** 32 - 1}     # where some routers' 32-bit counters stop for good


def upnp_counters(svc):
    """[bytes received, bytes sent] on the router's WAN side (the 64-bit actions where the router has them)."""
    out = []
    for action in (svc.get("rx", "GetTotalBytesReceived"), svc.get("tx", "GetTotalBytesSent")):
        m = re.search(r"<(New[A-Za-z0-9_]*)>\s*(\d+)\s*</\1>", upnp_call(svc, action))
        if not m:
            raise RuntimeError(f"the router did not answer {action}")
        out.append(int(m.group(2)))
    return out


def upnp_link_mbps(svc):
    """The line speed the router reports (for the impossible-value check), or 0."""
    try:
        x = upnp_call(svc, "GetCommonLinkProperties")
    except Exception:  # noqa: BLE001
        return 0
    v = [int(n) for n in re.findall(r"<NewLayer1(?:Downstream|Upstream)MaxBitRate>\s*(\d+)\s*<", x)]
    return max(v) / 1e6 if v else 0


def router_wan_ifindex(auth, target, name=""):
    """(ifIndex, name) of the router's WAN: the interface called `name`, or the one with the default route."""
    names = {k: str(v).strip('"') for k, v in (at.snmpwalk(auth, target, "1.3.6.1.2.1.31.1.1.1.1")
                                                  or at.snmpwalk(auth, target, "1.3.6.1.2.1.2.2.1.2")).items()}
    if name:
        idx = next((i for i, n in names.items() if n.lower() == name.strip().lower()), None)
        if not idx:
            raise RuntimeError(f'the router has no interface "{name}" (it has: {", ".join(list(names.values())[:12])})')
        return idx, names[idx]
    for base, prefix in (("1.3.6.1.2.1.4.24.7.1.7", "1.4.0.0.0.0.0."),       # inetCidrRouteIfIndex: 0.0.0.0/0
                         ("1.3.6.1.2.1.4.24.4.1.5", "0.0.0.0.0.0.0.0.")):    # ipCidrRouteIfIndex: 0.0.0.0 mask 0.0.0.0
        rows = at.snmpwalk(auth, target, base)
        hit = next((str(v).strip() for k, v in rows.items() if k.startswith(prefix) and str(v).strip().isdigit()
                    and str(v).strip() != "0"), None)
        if hit:
            return hit, names.get(hit, "")
    raise RuntimeError("the router does not say which interface has the default route: "
                       "set the router's WAN interface in Settings > Measurements")


def read_router_snmp(rs, h, t0, usage, wan_name=""):
    """One reading of the router's WAN interface over SNMP: {"rx", "tx"} in bit/s, or None (rs["error"] says why)."""
    try:
        auth, target = at.snmp_auth(h["_snmp"], h["_macros"])
        if not rs.get("idx") or t0 >= rs.get("redetect", 0) or rs.get("name_set") != wan_name:
            idx, nm = router_wan_ifindex(auth, target, wan_name)
            sp = snmp_get(auth, target, [f"1.3.6.1.2.1.31.1.1.1.15.{idx}"]).get(f"1.3.6.1.2.1.31.1.1.1.15.{idx}", 0)
            rs.update(idx=idx, ifname=nm, speed=sp, redetect=t0 + 3600, name_set=wan_name, prev=None, router=h["name"])
        i = rs["idx"]
        oid = {"up": "1.3.6.1.2.1.1.3.0", "in": f"1.3.6.1.2.1.31.1.1.1.6.{i}", "out": f"1.3.6.1.2.1.31.1.1.1.10.{i}",
               "in32": f"1.3.6.1.2.1.2.2.1.10.{i}", "out32": f"1.3.6.1.2.1.2.2.1.16.{i}", "disc": f"1.3.6.1.2.1.31.1.1.1.19.{i}"}
        v = snmp_get(auth, target, list(oid.values()))
    except Exception as e:  # noqa: BLE001
        rs.update(error=f"router {h['name']} over SNMP: {str(e)[:160]}", idx=None, redetect=t0 + 60)
        return None
    is32 = v.get(oid["in"]) is None
    rx, tx = (v.get(oid["in32"]), v.get(oid["out32"])) if is32 else (v.get(oid["in"]), v.get(oid["out"]))
    if rx is None or tx is None:
        rs.update(error=f"router {h['name']} does not answer for its WAN interface {rs.get('ifname') or rs['idx']}",
                  idx=None, redetect=t0 + 60)
        return None
    cur = (time.monotonic(), rx, tx, v.get(oid["up"]), v.get(oid["disc"]), is32)
    r_rx, r_tx, drx, dtx, why = counter_rate(rs.get("prev"), cur, rs.get("speed", 0))
    rs.update(prev=cur, error="")
    if why is not None:
        return None
    w = usage.setdefault("wan", {})
    for per in ("d", "m"):
        u = w.setdefault(per, [0, 0])
        u[0] += drx
        u[1] += dtx
    usage.setdefault("wan_since", now())
    return {"rx": r_rx, "tx": r_tx}


def measure_wan(S, data, hosts, sw_wan, upnp, rsnmp, usage, t0):
    """The WAN throughput: (rate or None, "snmp" | "upnp" | "switch" | None, reason when there is no rate).
    The router's WAN by default - SNMP when the gateway is an SNMP host in Zabbix, else UPnP. No other numbers are
    put in its place: without a router reading the result is None, with the reason."""
    if S["wan_source"] == "switch":
        return sw_wan, ("switch" if sw_wan else None), ("" if sw_wan else "no traffic data for the switch port to the gateway")
    gw_ip = next((n.get("ip") for n in data["nodes"] if n.get("kind") == "router" and n.get("ip")), None)
    gh = next((h for h in hosts if h.get("_snmp") and gw_ip and gw_ip in (h.get("_ips") or [h["_snmp"].get("ip")])), None)
    why = []
    if gh:
        r = read_router_snmp(rsnmp, gh, t0, usage, S["wan_interface"])
        if r:
            return r, "snmp", ""
        if rsnmp.get("prev") and not rsnmp.get("error"):
            return None, "snmp", "first reading, measuring…"        # a baseline, not a problem
        why.append(rsnmp.get("error") or "the router's SNMP reading failed")
    r = read_upnp(upnp, data, usage, "router", t0, max(sw_wan["rx"], sw_wan["tx"]) if sw_wan else None)
    if r:
        return r, "upnp", ""
    if upnp.get("svc") and upnp.get("prev") and not upnp.get("stuck_until"):
        return None, "upnp", "first reading, measuring…"
    why.append(upnp.get("error") or "the router does not answer UPnP")
    if not gh:
        why.append("for exact WAN figures add the gateway to Zabbix with an SNMP interface")
    return None, None, "; ".join(why)


def read_upnp(upnp, data, usage, source, t0, switch_rate=None):
    """One reading of the router's WAN counters. Returns {"rx", "tx"} in bit/s, or None. Finds the router again when
    needed (every 10 min while it does not answer), and adds the traffic to the day / month totals."""
    gw_ip = next((n.get("ip") for n in data["nodes"] if n.get("kind") == "router" and n.get("ip")), None)
    if gw_ip and not upnp["svc"] and t0 >= upnp["next"]:
        upnp["next"] = t0 + 600
        try:
            loc = upnp_discover(gw_ip)
            upnp["svc"] = upnp_wan_service(loc) if loc else None
            upnp["error"] = "" if upnp["svc"] else f"the gateway {gw_ip} does not answer UPnP (turn UPnP on in the router)"
            upnp["link"] = upnp_link_mbps(upnp["svc"]) if upnp["svc"] else 0
        except Exception as e:  # noqa: BLE001
            upnp["error"] = str(e)[:200]
    if not upnp["svc"] or t0 < upnp.get("stuck_until", 0):
        return None
    try:
        rx_b, tx_b = upnp_counters(upnp["svc"])
    except Exception as e:  # noqa: BLE001
        upnp["fails"] += 1
        upnp["error"] = str(e)[:200]
        if upnp["fails"] >= 3:                         # find it again (new address, router restarted)
            upnp.update(svc=None, prev=None, next=t0 + 60)
        return None
    stuck = None
    if rx_b in UPNP_STUCK or tx_b in UPNP_STUCK:
        stuck = f"its byte counters stopped at {max(rx_b, tx_b):,}: the router's firmware stops counting after a few GB"
    elif upnp["prev"] and (rx_b, tx_b) == upnp["prev"][1:3] and switch_rate and switch_rate > 1e6:
        upnp["still"] = upnp.get("still", 0) + 1                      # the switch sees traffic, the router counts none
        if upnp["still"] >= 12:
            stuck = "its byte counters do not change while the switch sees traffic"
    else:
        upnp["still"] = 0
    if stuck:
        upnp.update(prev=None, still=0, stuck_until=t0 + 600,
                    error=f"router not used: {stuck} - throughput from the switch port (checked again in 10 min)")
        return None
    upnp.pop("stuck_until", None)
    cur = (time.monotonic(), rx_b, tx_b, None, None, max(rx_b, tx_b) < 2 ** 32)   # many routers count in 32 bits
    rx, tx, drx, dtx, why = counter_rate(upnp["prev"], cur, upnp["link"])
    upnp.update(prev=cur, fails=0, error="")
    if why is not None:
        return None
    w = usage.setdefault("wan", {})
    for per in ("d", "m"):
        u = w.setdefault(per, [0, 0])
        u[0] += drx
        u[1] += dtx
    usage.setdefault("wan_since", now())
    return {"rx": rx, "tx": tx}


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

    save_json(os.path.join(DATA_DIR, LIVE), {
        "generated": t, "ports": ports, "nodes": nstat, "stats": stats, "cable": cable, "unusual": unusual,
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


def _speedtest_once(server=""):
    """Run one speed test. Prefers Ookla's official CLI (accurate on fast lines), falls back to speedtest-cli."""
    if os.path.isfile(OOKLA):
        cmd = [OOKLA, "--accept-license", "--accept-gdpr", "-f", "json", "-p", "no"] + (["-s", str(server)] if server else [])
        env = dict(os.environ, HOME=os.environ.get("HOME") or "/root")   # Ookla crashes without HOME
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
        j = None
        for line in (r.stdout or "").splitlines():
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if x.get("type") == "result":
                j = x
        if not j:
            msg = ""
            for line in (r.stderr or "").splitlines() + (r.stdout or "").splitlines():
                try:
                    msg = json.loads(line).get("message", msg)
                except ValueError:
                    msg = line or msg
            if "basic_string" in msg or "terminate called" in msg:
                msg = "Ookla speed test crashed (missing HOME) - run: bash install.sh --update"
            raise RuntimeError((msg or "speed test failed")[:200])
        d, u, p = j.get("download", {}), j.get("upload", {}), j.get("ping", {})
        srv = j.get("server", {})
        return {"down": d.get("bandwidth", 0) * 8, "up": u.get("bandwidth", 0) * 8,
                "ping": round(p.get("latency", 0), 1), "jitter": round(p.get("jitter", 0), 1),
                "load_down": round((d.get("latency") or {}).get("iqm", 0), 1) or None,
                "load_up": round((u.get("latency") or {}).get("iqm", 0), 1) or None,
                "loss": j.get("packetLoss"), "server": f"{srv.get('name', '')}, {srv.get('location', '')}".strip(", "),
                "isp": j.get("isp", ""), "url": (j.get("result") or {}).get("url", ""), "engine": "Ookla"}
    if shutil.which("speedtest-cli"):
        cmd = ["speedtest-cli", "--secure", "--json"] + (["--server", str(server)] if server else [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        j = json.loads(r.stdout or "{}")
        if not j.get("download"):
            raise RuntimeError((r.stderr or "speed test failed").strip().splitlines()[-1][:200])
        sv = j.get("server") or {}
        return {"down": j["download"], "up": j["upload"], "ping": round(j.get("ping", 0), 1),
                "server": f"{sv.get('sponsor', '')}, {sv.get('name', '')}", "isp": (j.get("client") or {}).get("isp", ""),
                "engine": "speedtest-cli"}
    raise RuntimeError("No speed test program found - run: bash install.sh --update")


# --------------------------------------------------------------------------- #
# Speed test providers without an extra program: Cloudflare (speed.cloudflare.com) and Fast.com (Netflix)
# --------------------------------------------------------------------------- #
CF_BASE = "https://speed.cloudflare.com"
FAST_HOME, FAST_API = "https://fast.com", "https://api.fast.com/netflix/speedtest/v2"
SPEED_SECONDS = 10


def _latency(urls, n=10):
    """Median and jitter (ms) of small requests: time until the answer's headers arrive."""
    s, ms = requests.Session(), []
    for i in range(n):
        try:
            r = s.get(urls[i % len(urls)], timeout=5)
            ms.append(r.elapsed.total_seconds() * 1000)
        except requests.RequestException:
            pass
    if not ms:
        raise RuntimeError("the speed test server does not answer")
    ms.sort()
    med = ms[len(ms) // 2]
    jit = sum(abs(a - b) for a, b in zip(ms, ms[1:])) / max(1, len(ms) - 1)
    return round(med, 1), round(jit, 1)


def _transfer(urls, upload=False, seconds=SPEED_SECONDS, streams=4, size=None):
    """bits/s over several parallel connections, and the ping to 1.1.1.1 meanwhile (latency under load)."""
    import threading
    stop, lock, total, errors, loaded = time.monotonic() + seconds, threading.Lock(), [0], [], []
    body = os.urandom(65536) * 16 if upload else None          # 1 MiB, sent again and again

    def worker(i):
        sess, k = requests.Session(), i
        while time.monotonic() < stop:
            url = urls[k % len(urls)]
            k += 1
            try:
                if upload:
                    sess.post(url, data=body, timeout=15).raise_for_status()
                    with lock:
                        total[0] += len(body)
                else:
                    with sess.get(url, stream=True, timeout=15) as r:
                        r.raise_for_status()
                        for chunk in r.iter_content(65536):
                            with lock:
                                total[0] += len(chunk)
                            if time.monotonic() >= stop:
                                break
            except requests.RequestException as e:
                errors.append(str(e)[:150])
                time.sleep(0.3)

    def pinger():
        while time.monotonic() < stop - 0.5:
            v = ping_stats("1.1.1.1", 1)["ms"]
            if v is not None:
                loaded.append(v)
            time.sleep(0.4)
    t0 = time.monotonic()
    th = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(streams)]
    th.append(threading.Thread(target=pinger, daemon=True))
    for x in th:
        x.start()
    for x in th:
        x.join(seconds + 20)
    if not total[0]:
        raise RuntimeError(errors[-1] if errors else "no data could be transferred")
    loaded.sort()
    return total[0] * 8 / (time.monotonic() - t0), (loaded[len(loaded) // 2] if loaded else None)


def _speedtest_cloudflare():
    try:
        meta = requests.get(CF_BASE + "/meta", timeout=8).json()
    except (requests.RequestException, ValueError):
        meta = {}
    ping, jit = _latency([CF_BASE + "/__down?bytes=0"])
    down, ld = _transfer([CF_BASE + "/__down?bytes=25000000"])
    up, lu = _transfer([CF_BASE + "/__up"], upload=True)
    where = ", ".join(x for x in (meta.get("city"), meta.get("country")) if x)
    return {"down": down, "up": up, "ping": ping, "jitter": jit, "load_down": ld, "load_up": lu,
            "server": "Cloudflare" + (f" {meta['colo']}" if meta.get("colo") else "") + (f", {where}" if where else ""),
            "isp": meta.get("asOrganization", ""), "url": "", "engine": "Cloudflare"}


def _speedtest_fast():
    """Fast.com (Netflix). Its API is not official: a token is read from fast.com's own script."""
    try:
        home = requests.get(FAST_HOME + "/", timeout=10).text
        js = re.search(r'<script src="(/app-[^"]+\.js)"', home).group(1)
        token = re.search(r'token:"([A-Za-z0-9]+)"', requests.get(FAST_HOME + js, timeout=10).text).group(1)
        j = requests.get(FAST_API, params={"https": "true", "token": token, "urlCount": 5}, timeout=10).json()
        targets = [t["url"] for t in j["targets"]]
    except (requests.RequestException, AttributeError, KeyError, ValueError) as e:
        raise RuntimeError(f"Fast.com cannot be reached or has changed ({type(e).__name__}) - try Ookla or Cloudflare")
    rng = lambda u, a, b: u.replace("/speedtest", f"/speedtest/range/{a}-{b}", 1)  # noqa: E731
    ping, jit = _latency([rng(u, 0, 0) for u in targets])
    down, ld = _transfer([rng(u, 0, 26214400) for u in targets])
    up, lu = _transfer([rng(u, 0, 0) for u in targets], upload=True)
    loc = (j["targets"][0].get("location") or {}) if j.get("targets") else {}
    cl = j.get("client") or {}
    return {"down": down, "up": up, "ping": ping, "jitter": jit, "load_down": ld, "load_up": lu,
            "server": "Fast.com, " + ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
            "isp": cl.get("isp", ""), "url": "", "engine": "Fast.com"}


OOKLA_SERVERS = os.path.join(STATE_DIR, "spidermap_ookla_servers.json")


def ookla_servers(refresh=False):
    """Nearby Ookla servers for the Settings list (asked once a day)."""
    cur = load_json(OOKLA_SERVERS, {})
    if not refresh and now() - cur.get("t", 0) < 86400 or not os.path.isfile(OOKLA):
        return cur.get("servers", [])
    try:
        env = dict(os.environ, HOME=os.environ.get("HOME") or "/root")
        r = subprocess.run([OOKLA, "--accept-license", "--accept-gdpr", "--servers", "-f", "json"],
                           capture_output=True, text=True, timeout=60, env=env)
        servers = [{"id": str(x["id"]), "name": f"{x.get('name', '')}, {x.get('location', '')}".strip(", ")}
                   for x in json.loads(r.stdout or "{}").get("servers", [])][:20]
        save_json(OOKLA_SERVERS, {"t": now(), "servers": servers})
        return servers
    except (OSError, ValueError, subprocess.SubprocessError):
        return cur.get("servers", [])


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


GAP_S = 120          # no internet check for this long = "not monitored" (service stopped, server off)


def inet_start(inet, t):
    """When the service starts: the time since the last check was not monitored. An outage that was going on is
    closed at the last moment we know about - we cannot tell when it ended."""
    inet.setdefault("since", t)
    inet.setdefault("outages", [])
    last = inet.get("seen")
    if last and t - last > GAP_S:
        if inet.get("down_since"):
            inet["outages"] = (inet["outages"] + [[inet.pop("down_since"), last]])[-500:]
        inet["gaps"] = (inet.get("gaps", []) + [[last, t]])[-500:]
    inet["seen"] = t


def inet_update(inet, up, t):
    """One internet check (all targets failing has already been confirmed by a second try).
    Returns "down", ("up", seconds) or None."""
    last = inet.get("seen")
    if last and t - last > GAP_S:                    # the service was stuck or the clock jumped: not monitored
        inet["gaps"] = (inet.get("gaps", []) + [[last, t]])[-500:]
    inet["seen"] = t
    if not up and not inet.get("down_since"):
        inet["down_since"] = t
        return "down"
    if up and inet.get("down_since"):
        s0 = inet.pop("down_since")
        inet["outages"] = (inet["outages"] + [[s0, t]])[-500:]
        return ("up", t - s0)
    return None


def _overlap(spans, a, b):
    return sum(max(0, min(e, b) - max(s_, a)) for s_, e in spans)


def month_uptime(inet, t, month_start=None):
    """(uptime % of the monitored time this month, outages this month, % of the month that was monitored).
    Time without monitoring counts neither as up nor as down."""
    ms = month_start or int(datetime.datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    since = max(ms, inet.get("since", t))
    if t - since < 60:
        return None, [], None
    outs = [o for o in inet.get("outages", []) if o[1] >= since]
    spans = outs + ([[inet["down_since"], t]] if inet.get("down_since") else [])
    gaps = [g for g in inet.get("gaps", []) if g[1] >= since]
    monitored = (t - since) - _overlap(gaps, since, t)
    if monitored < 60:
        return None, outs, 0.0
    down = _overlap(spans, since, t)
    return round(100 * (1 - down / monitored), 3), outs, round(100 * monitored / (t - since), 1)


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
DEVICE_FIELDS = {"name": str, "type": str, "url": str, "pin": str, "important": bool}
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
        changed = {k: [old[k], v] for k, v in effective_settings(ns).items() if old[k] != v}
        if not changed:
            res.update(ok=True, message="Nothing changed.")
            return res
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
        res.update(ok=True, restart=True, message="Map reset. A new scan starts now; the map appears within a few minutes.")
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
        "values": effective_settings(st), "effective_permissions": permissions(st),
        "advanced": {k: st[k] for k in ADVANCED_SETTINGS if k in st},
        "device_categories": [{"key": k, "title": t, "examples": e, "detect": d, "icon": TYPE_ICON.get(ty, "help-circle")}
                              for k, ty, t, e, d, _rx in DEVICE_CATEGORIES],
        "ookla_servers": load_json(OOKLA_SERVERS, {}).get("servers", []),
        "devices": cfg.get("devices", {}), "history": config_history()})
    save_json(os.path.join(DATA_DIR, ACCESS), permissions(st))


DISPLAY_KEYS = ("wheel", "map_name", "page_refresh_s", "show_offline", "ip_labels", "link_thickness", "units", "load_warn", "load_crit")


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
    return {"api": api_state, "switches": switches, "scan": {k: rep.get(k) for k in
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
    use32, quality, rej_said = {}, {}, {}
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
            speeds = {str(p["ifindex"]): p.get("speed_mbps", 0) for p in plist}
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
GATEWAY_PHP = r"""<?php
/*
 * SpiderMap gateway - the only SpiderMap entry point in the Zabbix web folder.
 * Generated by spidermap.py (https://github.com/christopher-theodorou/spidermap). Changes are overwritten on update.
 * License: AGPL-3.0.
 *
 *   GET  spidermap.php              -> the map page          (logged-in Zabbix users, see VIEW_MIN_TYPE)
 *   GET  spidermap.php?data=<name>  -> map data as JSON      (same check)
 *   POST spidermap.php              -> edits / speed tests   (Zabbix admins, same-origin requests only)
 *
 * The page and the data live outside the web folder (DATA_DIR). The Zabbix session is checked through the public
 * Zabbix API, not internal frontend classes, so Zabbix upgrades do not break this file.
 */
const DATA_DIR = '/var/lib/spidermap/www';
const STATE_DIR = '/var/lib/spidermap/inbox';      // the only folder the web server may write to
const VIEW_MIN_TYPE = __VIEW_MIN_TYPE__;            // 1 = Zabbix user, 2 = admin, 3 = super admin
const EDIT_MIN_TYPE = __EDIT_MIN_TYPE__;            // who may rename devices, set types, pins, web addresses (2 or 3)
const SPEED_MIN_TYPE = __SPEED_MIN_TYPE__;          // who may start a speed test (1, 2 or 3)
const SETTINGS_MIN_TYPE = __SETTINGS_MIN_TYPE__;    // who may change settings and start scans (2 or 3)
// set in devices.json: "permissions": {"view": 3, "edit": 2, "speedtest": 2}
const ZBX_API = '__API_URL__';                      // checked by the installer from this server
const CACHE_TTL = 10;                               // seconds a session check is reused
const FILES = ['map' => 'spidermap.json', 'live' => 'spidermap_live.json', 'rt' => 'spidermap_rt.json',
               'events' => 'spidermap_events.json', 'presence' => 'spidermap_presence.json',
               'status' => 'spidermap_status.json', 'settings' => 'spidermap_settings.json'];

// Permissions changed in the Settings panel apply at once: the SpiderMap service writes them to access.json.
// The values above are the defaults; the lower limits are enforced here again.
function perm($what) {
  static $acc = null;
  if ($acc === null) {
    $j = @json_decode((string)@file_get_contents(DATA_DIR . '/access.json'), true);
    $acc = is_array($j) ? $j : [];
  }
  $def = ['view' => VIEW_MIN_TYPE, 'edit' => EDIT_MIN_TYPE, 'speedtest' => SPEED_MIN_TYPE, 'settings' => SETTINGS_MIN_TYPE];
  $lo = ['view' => 1, 'edit' => 2, 'speedtest' => 1, 'settings' => 2];
  $v = isset($acc[$what]) && is_numeric($acc[$what]) ? (int)$acc[$what] : $def[$what];
  return max($lo[$what], min(3, $v));
}

// A request for the SpiderMap service, readable by its group (0660).
function write_request($file, $body) {
  $path = STATE_DIR . '/' . $file;
  if (@file_put_contents($path, json_encode($body), LOCK_EX) === false) deny(500, 'Cannot write ' . STATE_DIR);
  @chmod($path, 0660);
}

$IS_PAGE = ($_SERVER['REQUEST_METHOD'] ?? '') === 'GET' && !isset($_GET['data']);

header('Cache-Control: private, no-store, max-age=0');
header('X-Content-Type-Options: nosniff');
header('Referrer-Policy: same-origin');
header('X-Frame-Options: SAMEORIGIN');               // only Zabbix itself may show SpiderMap in a frame

function login_page($title, $msg, $show_login) {
  $t = htmlspecialchars($title, ENT_QUOTES); $m = htmlspecialchars($msg, ENT_QUOTES);
  $btn = $show_login ? '<a href="index.php" target="_top">Open the Zabbix login</a>' : '';
  return "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    . "<title>SpiderMap</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
    . "font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f5f6f8;color:#1d2433}"
    . "@media (prefers-color-scheme:dark){body{background:#14171c;color:#e7eaf0}.b{background:#1d2128!important;border-color:#2c323c!important}}"
    . ".b{max-width:380px;text-align:center;background:#fff;border:1px solid #e3e6ec;border-radius:16px;padding:28px}"
    . "h1{font-size:18px;margin:8px 0}p{color:#6b7385}a{display:inline-block;margin-top:10px;padding:8px 16px;border:1px solid #4f5bd5;"
    . "border-radius:8px;color:#4f5bd5;text-decoration:none}.l{font-size:30px;color:#4f5bd5}</style></head><body><div class=\"b\">"
    . "<div class=\"l\">&#128274;</div><h1>$t</h1><p>$m</p>$btn</div></body></html>";
}

function deny($code, $msg, $login = false) {
  global $IS_PAGE;
  http_response_code($code);
  if ($IS_PAGE) {
    header('Content-Type: text/html; charset=UTF-8');
    echo $login ? login_page('Log in to Zabbix', 'SpiderMap shows your network only to logged-in Zabbix users.', true)
                : login_page('No access', $msg, false);
  } else {
    header('Content-Type: application/json');
    echo json_encode(['ok' => false, 'error' => $login ? 'login' : $msg]);
  }
  exit;
}

function api_call($method, $params, $sid = null) {
  $url = ZBX_API;
  if ($url === '' || $url[0] === '_') {   // not filled in: same web server
    $https = !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off';
    $url = ($https ? 'https' : 'http') . '://127.0.0.1:' . $_SERVER['SERVER_PORT']
         . rtrim(dirname($_SERVER['SCRIPT_NAME']), '/') . '/api_jsonrpc.php';
  }
  $body = ['jsonrpc' => '2.0', 'method' => $method, 'params' => $params, 'id' => 1];
  $hdr = "Content-Type: application/json-rpc\r\n" . ($sid ? "Authorization: Bearer $sid\r\n" : '');
  $ctx = stream_context_create([
    'http' => ['method' => 'POST', 'header' => $hdr, 'timeout' => 5, 'ignore_errors' => true, 'content' => json_encode($body)],
    'ssl' => ['verify_peer' => false, 'verify_peer_name' => false]]);
  $r = json_decode((string)@file_get_contents($url, false, $ctx), true);
  if ($sid && isset($r['error'])) {        // Zabbix before 6.4: session in the request body
    $body['auth'] = $sid;
    $ctx = stream_context_create(['http' => ['method' => 'POST', 'header' => "Content-Type: application/json-rpc\r\n",
      'timeout' => 5, 'ignore_errors' => true, 'content' => json_encode($body)],
      'ssl' => ['verify_peer' => false, 'verify_peer_name' => false]]);
    $r = json_decode((string)@file_get_contents($url, false, $ctx), true);
  }
  return is_array($r) ? $r : [];
}

function session_id_from_cookie() {
  if (isset($_COOKIE['zbx_session'])) {     // Zabbix 5.4 and newer
    $raw = strtr((string)$_COOKIE['zbx_session'], ' ', '+');
    $s = json_decode((string)base64_decode($raw, true), true);
    $sid = is_array($s) ? (string)($s['sessionid'] ?? '') : '';
  } else {                                  // older Zabbix versions
    $sid = (string)($_COOKIE['zbx_sessionid'] ?? '');
  }
  return preg_match('/^[A-Za-z0-9]{16,128}$/', $sid) ? $sid : '';
}

// Check the session with Zabbix (extend=false: SpiderMap never keeps an expired session alive)
function zabbix_user() {
  $sid = session_id_from_cookie();
  if ($sid === '') deny(401, 'login', true);
  $cdir = STATE_DIR . '/sess';
  $cache = $cdir . '/' . hash('sha256', $sid);
  if (is_file($cache) && filemtime($cache) > time() - CACHE_TTL) {
    $u = json_decode((string)@file_get_contents($cache), true);
    if (is_array($u)) return $u;
  }
  $r = api_call('user.checkAuthentication', ['sessionid' => $sid, 'extend' => false]);
  if (empty($r['result']['userid'])) { @unlink($cache); deny(401, 'login', true); }
  $type = (int)($r['result']['type'] ?? 0);
  if (!$type) {
    $q = api_call('user.get', ['output' => ['userid'], 'userids' => [$r['result']['userid']], 'selectRole' => ['type']], $sid);
    $type = (int)($q['result'][0]['role']['type'] ?? $q['result'][0]['type'] ?? 1);
  }
  $u = ['id' => (string)$r['result']['userid'], 'name' => (string)($r['result']['username'] ?? $r['result']['alias'] ?? ''), 'type' => $type];
  if (!is_dir($cdir)) @mkdir($cdir, 0700, true);
  @file_put_contents($cache, json_encode($u), LOCK_EX);
  if (mt_rand(1, 50) === 1) foreach (glob($cdir . '/*') ?: [] as $f) if (filemtime($f) < time() - 600) @unlink($f);
  return $u;
}

$user = zabbix_user();
if (strtolower($user['name']) === 'guest') deny(403, 'The Zabbix guest account cannot open SpiderMap. Log in with your own user.');
if ($user['type'] < perm('view'))
  deny(403, perm('view') >= 3 ? 'SpiderMap is available to Zabbix Super admins only.' : 'Your Zabbix user is not allowed to open SpiderMap.');

// ---- the page
if ($IS_PAGE) {
  $page = DATA_DIR . '/spidermap.html';
  if (!is_file($page)) deny(503, 'SpiderMap is not installed completely yet. Run: bash install.sh --update');
  header('Content-Type: text/html; charset=UTF-8');
  header("Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
       . "font-src 'self' data: https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'");
  readfile($page);
  exit;
}

// ---- the data
if ($_SERVER['REQUEST_METHOD'] === 'GET') {
  header('Content-Type: application/json');
  if ((string)$_GET['data'] === 'me') {                // what this user may do, so the page can grey out buttons
    echo json_encode(['name' => $user['name'], 'type' => $user['type'],
                      'can' => ['edit' => $user['type'] >= perm('edit'), 'speedtest' => $user['type'] >= perm('speedtest'),
                                'settings' => $user['type'] >= perm('settings')],
                      'need' => ['edit' => perm('edit'), 'speedtest' => perm('speedtest'), 'settings' => perm('settings')]]);
    exit;
  }
  if ((string)$_GET['data'] === 'settings' && $user['type'] < perm('settings'))
    deny(403, 'You may not see the settings.');
  $f = FILES[(string)$_GET['data']] ?? null;
  if (!$f) deny(400, 'Unknown data set');
  $path = DATA_DIR . '/' . $f;
  if (!is_file($path)) deny(404, 'No data yet');
  readfile($path);
  exit;
}

// ---- changes: same-origin only, Zabbix admins only
header('Content-Type: application/json');
if ($_SERVER['REQUEST_METHOD'] !== 'POST') { header('Allow: GET, POST'); deny(405, 'GET or POST only'); }
if (($_SERVER['HTTP_X_SPIDERMAP'] ?? '') !== '1') deny(403, 'Missing request header');   // blocks cross-site forms (CSRF)
$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
if ($origin !== '' && parse_url($origin, PHP_URL_HOST) !== parse_url('//' . ($_SERVER['HTTP_HOST'] ?? ''), PHP_URL_HOST))
  deny(403, 'Cross-site request refused');
$raw = (string)file_get_contents('php://input', false, null, 0, 4194305);
if (strlen($raw) > 4194304) deny(413, 'Request too large');
$in = json_decode($raw, true);
if (!is_array($in)) deny(400, 'Bad request');
$ROLE = [1 => 'Zabbix users', 2 => 'Zabbix admins', 3 => 'Zabbix Super admins'];

$act = (string)($in['action'] ?? '');
if ($act === 'speedtest') {
  if ($user['type'] < perm('speedtest')) deny(403, 'Only ' . $ROLE[perm('speedtest')] . ' can start a speed test.');
  // who asked is recorded in the activity feed and the audit log
  write_request('speedtest.request', ['by' => $user['name'], 'at' => time()]);
  echo json_encode(['ok' => true]); exit;
}

// scans and settings: carried out by the SpiderMap service, which validates everything again
if (in_array($act, ['scan', 'cancel', 'pause', 'resume', 'settings', 'import', 'restore', 'reset'], true)) {
  if ($user['type'] < perm('settings')) deny(403, 'Only ' . $ROLE[perm('settings')] . ' can change settings or start scans.');
  $who = ['by' => $user['name'], 'at' => time()];
  $rid = (string)($in['rid'] ?? '');                 // lets the page find the answer to its own request
  if (preg_match('/^[a-z0-9]{6,24}$/', $rid)) $who['rid'] = $rid;
  if ($act === 'scan' || $act === 'cancel') {
    write_request($act === 'scan' ? 'scan.request' : 'scan.cancel', $who);
  } else {
    if ($act === 'pause' || $act === 'resume') $body = ['settings' => ['auto_scan' => $act === 'resume']];
    elseif ($act === 'settings') { if (!is_array($in['settings'] ?? null)) deny(400, 'Bad request'); $body = ['settings' => $in['settings']]; }
    elseif ($act === 'import') { if (!is_array($in['import'] ?? null)) deny(400, 'Bad request'); $body = ['import' => $in['import']]; }
    elseif ($act === 'reset') {
      $r = is_array($in['reset'] ?? null) ? $in['reset'] : [];
      $body = ['reset' => ['keep_devices' => ($r['keep_devices'] ?? true) !== false, 'keep_speedtests' => ($r['keep_speedtests'] ?? true) !== false]];
    }
    else {
      $r = (string)($in['restore'] ?? '');
      if (!preg_match('/^devices-[0-9]+\.json$/', $r)) deny(400, 'Bad request');
      $body = ['restore' => $r];
    }
    write_request('settings.request', $body + $who);
  }
  echo json_encode(['ok' => true, 'queued' => $act]); exit;
}

if ($user['type'] < perm('edit')) deny(403, 'Only ' . $ROLE[perm('edit')] . ' can make changes.');

$mac = strtolower((string)($in['mac'] ?? ''));
if (!preg_match('/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/', $mac)) deny(400, 'Bad MAC address');
$e = [];
foreach (['name', 'type', 'url', 'pin'] as $k)
  if (array_key_exists($k, $in)) {
    $v = trim((string)$in[$k]);
    $e[$k] = function_exists('mb_substr') ? mb_substr($v, 0, 200) : substr($v, 0, 200);
  }
if (isset($e['url']) && $e['url'] !== '' && !preg_match('#^https?://#i', $e['url'])) deny(400, 'Web UI address must start with http:// or https://');
if (array_key_exists('important', $in)) $e['important'] = (bool)$in['important'];
$fh = @fopen(STATE_DIR . '/edits.json', 'c+');
if (!$fh) deny(500, 'Cannot write ' . STATE_DIR . '/edits.json');
flock($fh, LOCK_EX);
$cur = json_decode((string)stream_get_contents($fh), true) ?: [];
$cur[$mac] = array_merge($cur[$mac] ?? [], $e, ['by' => $user['name'], 'at' => time()]);
ftruncate($fh, 0); rewind($fh); fwrite($fh, json_encode($cur, JSON_PRETTY_PRINT)); fflush($fh);
flock($fh, LOCK_UN); fclose($fh);
@chmod(STATE_DIR . '/edits.json', 0660);
echo json_encode(['ok' => true, 'by' => $user['name']]);
"""

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
# Each setting: key, type, default, tab, group, label, and optionally: range, unit, choices, help, preview (shown,
# not active yet), locked (always on). Types: bool, int, choice, str, time, digits, cidrs, ips, level.
def _S(key, typ, default, tab, group, label, **kw):
    return {"key": key, "type": typ, "default": default, "tab": tab, "group": group, "label": label, **kw}


_LEVEL3 = [[1, "Zabbix users"], [2, "Zabbix admins"], [3, "Zabbix Super admins"]]
SETTINGS_SCHEMA = [
    # ---- General
    _S("map_name", "str", "", "General", "General settings", "Map name", maxlen=60, help="Shown in the header and the browser tab."),
    _S("default_view", "choice", "physical", "General", "General settings", "Default view",
       choices=[["physical", "Physical topology"], ["l3", "Logical topology (L3)", "planned"], ["vlan", "By VLAN", "planned"]]),
    _S("page_refresh_s", "choice", 60, "General", "General settings", "Map refresh",
       choices=[[10, "10 seconds"], [30, "30 seconds"], [60, "1 minute"], [0, "Manually"]],
       help="How often open pages reload the map. Live traffic keeps its own interval (Measurements)."),
    _S("wheel", "choice", "zoom", "General", "General settings", "Mouse wheel",
       choices=[["zoom", "Zooms the map"], ["pan", "Scrolls the map"]],
       help="Whatever you choose: Ctrl + wheel or a pinch always zooms, Shift + wheel and two-finger scrolling always move the map."),
    _S("show_offline", "bool", True, "General", "General settings", "Show offline devices",
       help="Devices that do not answer are shown in grey instead of being hidden from the map."),
    _S("ip_labels", "bool", True, "General", "General settings", "IP labels on nodes"),
    _S("zabbix_triggers", "bool", False, "General", "General settings", "Link with Zabbix triggers", preview="0.7",
       help="Nodes change colour with the severity of the host's active problems."),
    _S("flag_hours", "int", 24, "General", "Devices", "Mark new and moved devices for", range=[1, 720], unit="h"),
    _S("forget_after_days", "int", 30, "General", "Devices", "Forget unknown devices after", range=[1, 365], unit="days"),
    # ---- Scans
    _S("auto_scan", "bool", True, "Scans", "Scan schedule", "Automatic scan",
       help="When off, scans start only with the Scan now button. Traffic and status keep updating."),
    _S("icmp_discovery", "bool", True, "Scans", "Scan schedule", "Discovery via ICMP",
       help="Ping every address of the allowed networks to find active devices."),
    _S("snmp_collection", "bool", True, "Scans", "Scan schedule", "SNMP collection",
       help="Read LLDP, ARP and MAC tables from the switches. When off, the switches keep their last data."),
    _S("scan_interval_min", "int", 5, "Scans", "Scan schedule", "Full scan", range=[2, 1440], unit="min",
       choices=[[5, "5 minutes"], [15, "15 minutes"], [30, "30 minutes"], [60, "1 hour"], [360, "6 hours"]],
       help="How often all allowed networks are scanned again."),
    _S("rt_interval_s", "int", 5, "Scans", "Scan schedule", "Traffic measurements", range=[5, 300], unit="s",
       choices=[[5, "5 seconds"], [10, "10 seconds"], [30, "30 seconds"], [60, "1 minute"], [300, "5 minutes"]],
       help="How often the interface counters are read, for live traffic and link thickness."),
    _S("ping_size", "int", 56, "Scans", "Scan schedule", "ICMP payload size", range=[0, 65500], unit="bytes",
       help="Data bytes per ping packet (0-65500). Affects ICMP only, not SNMP."),
    _S("ping_timeout_ms", "int", 1000, "Scans", "Scan schedule", "Timeout", range=[100, 10000], unit="ms",
       help="How long to wait for each answer (100-10000 ms)."),
    _S("ping_parallel", "int", 64, "Scans", "Scan schedule", "Parallel checks", range=[1, 128], stepper=True,
       help="How many addresses are checked at the same time (1-128)."),
    _S("scan_subnets", "cidrs", [], "Scans", "Scan schedule", "Allowed networks",
       help="Networks in CIDR form, e.g. 10.0.0.0/16. Press Enter or comma to add. Empty: the /24 of each switch."),
    _S("scan_exclude", "ips", [], "Scans", "Scan schedule", "Never scan", help="Addresses or networks that are never pinged."),
    # ---- SNMP (the credentials stay on each host's SNMP interface in Zabbix)
    _S("snmp_max_repetitions", "int", 1, "SNMP", "Packet size", "Max-repetitions", range=[1, 100],
       help="How many rows each GETBULK asks for. 1 = one row per request (GETNEXT, the safest). Larger values mean "
            "fewer requests but bigger packets. SNMPv1 always uses GETNEXT."),
    _S("snmp_retries", "int", 1, "SNMP", "Packet size", "Retries", range=[0, 10]),
    _S("snmp_timeout_ms", "int", 3000, "SNMP", "Packet size", "Timeout", range=[500, 10000], unit="ms"),
    # ---- Topology
    _S("topo_lldp", "bool", True, "Topology", "Link sources", "LLDP"),
    _S("topo_cdp", "bool", False, "Topology", "Link sources", "CDP (Cisco)", preview="0.7"),
    _S("topo_vlans", "bool", True, "Topology", "Link sources", "VLANs (Q-BRIDGE-MIB)",
       help="Reads the VLANs of each switch port and of each device, for the VLAN filter on the map. Switches without "
            "Q-BRIDGE-MIB simply show no VLANs."),
    _S("topo_fdb", "bool", True, "Topology", "Link sources", "MAC tables (FDB)",
       help="Places devices without LLDP through the MAC addresses the switches learn. When off, devices are not "
            "placed on switch ports."),
    _S("topo_arp", "bool", True, "Topology", "Link sources", "ARP tables",
       help="IP addresses from the switches' and routers' ARP tables, in addition to this server's own."),
    _S("layout", "choice", "tree", "Topology", "Layout", "Map layout",
       choices=[["tree", "Hierarchical (tree)"], ["force", "Free (force-directed)", "planned"], ["radial", "Radial", "planned"]]),
    _S("group_subnet", "bool", False, "Topology", "Layout", "Group by subnet", preview="0.7"),
    _S("lock_positions", "bool", False, "Topology", "Layout", "Lock node positions", preview="0.7",
       help="Nodes you moved by hand keep their place after a new scan."),
    # ---- Device types (the tab shows a card with a switch per category)
    _S("device_types_off", "list", [], "Device types", "Device types", "Categories switched off"),
    # ---- Measurements
    _S("wan_source", "choice", "router", "Measurements", "Traffic", "Throughput source",
       choices=[["router", "The router's WAN (SNMP, else UPnP)"], ["switch", "Switch port to the gateway"]],
       help="The router's WAN: read over SNMP when the gateway is a Zabbix host with an SNMP interface (best: 64-bit "
            "counters), else over UPnP. When neither works, the card shows a warning instead of other numbers."),
    _S("wan_interface", "str", "", "Measurements", "Traffic", "Router's WAN interface", maxlen=40,
       help="SNMP only. The interface name on the router, e.g. ether1, pppoe-out1, igb0. Empty: the interface of the "
            "default route."),
    _S("link_thickness", "bool", False, "Measurements", "Traffic", "Link thickness by traffic"),
    _S("units", "choice", "bits", "Measurements", "Traffic", "Units",
       choices=[["bits", "bits/s"], ["bytes", "bytes/s"], ["percent", "% of capacity"]]),
    _S("load_warn", "int", 70, "Measurements", "Traffic", "Load warning", range=[1, 100], unit="%"),
    _S("load_crit", "int", 90, "Measurements", "Traffic", "Load critical", range=[1, 100], unit="%",
       help="Must be higher than the warning."),
    _S("live_interval_s", "int", 60, "Measurements", "Traffic", "Status refresh", range=[30, 600], unit="s",
       choices=[[30, "30 seconds"], [60, "1 minute"], [120, "2 minutes"], [300, "5 minutes"]]),
    _S("events_keep_days", "int", 60, "Measurements", "History", "Keep history", range=[7, 365], unit="days",
       choices=[[7, "7 days"], [30, "30 days"], [60, "60 days"], [90, "90 days"], [365, "1 year"]]),
    _S("presence_keep_days", "int", 30, "Measurements", "History", "Keep who-was-online", range=[1, 90], unit="days"),
    _S("speedtest_daily", "time", "", "Measurements", "Speed test", "Daily speed test at", help="HH:MM, empty = only on request."),
    _S("speedtest_provider", "choice", "ookla", "Measurements", "Speed test", "Speed test provider",
       choices=[["ookla", "Ookla (speedtest.net)"], ["cloudflare", "Cloudflare (speed.cloudflare.com)"], ["fast", "Fast.com (Netflix)"]],
       help="Fast.com has no official API: if Netflix changes it, its tests may stop until SpiderMap is updated. "
            "nPerf cannot be used: it has no public interface."),
    _S("speedtest_server", "digits", "", "Measurements", "Speed test", "Ookla server", help="Empty = the nearest server. Ookla only."),
    # ---- Security
    _S("snmp_readonly", "bool", True, "Security", "Security", "Read-only SNMP", locked=True,
       help="SpiderMap never sends SET, even if the community may write. Always on."),
    _S("offpeak", "bool", False, "Security", "Security", "Scan only off-peak",
       help="Automatic full scans only inside this window. Scan now always works."),
    _S("offpeak_start", "time", "22:00", "Security", "Security", "Scan window from"),
    _S("offpeak_end", "time", "06:00", "Security", "Security", "Scan window until"),
    _S("scan_pps", "int", 500, "Security", "Security", "Packet limit", range=[10, 10000], unit="pps",
       help="Most ping packets per second for the whole scan, so that intrusion detection is not triggered."),
    _S("max_targets", "int", 1024, "Security", "Security", "Most addresses per scan", range=[16, 65536],
       help="A safety limit: a scan with more addresses is refused."),
    _S("permissions.settings", "level", 3, "Security", "Access", "Access to settings", range=[2, 3],
       choices=[[3, "Super admins only"], [2, "Admins and Super admins"]], help="Also: Scan now, Pause."),
    _S("permissions.view", "level", 3, "Security", "Access", "Who may open SpiderMap", range=[1, 3], choices=_LEVEL3),
    _S("permissions.edit", "level", 2, "Security", "Access", "Who may change devices", range=[2, 3], choices=_LEVEL3[1:],
       help="Never below Zabbix admins."),
    _S("permissions.speedtest", "level", 2, "Security", "Access", "Who may start speed tests", range=[1, 3], choices=_LEVEL3),
    # ---- Logging
    _S("log_level", "choice", "info", "Logging", "Logging", "Log level",
       choices=[["error", "Errors only"], ["warning", "Warnings"], ["info", "Information"], ["debug", "Debug"]]),
    _S("log_keep_days", "int", 14, "Logging", "Logging", "Keep log files", range=[1, 90], unit="days",
       choices=[[7, "7 days"], [14, "14 days"], [30, "30 days"]]),
]
SETTINGS_BY_KEY = {f["key"]: f for f in SETTINGS_SCHEMA}
SETTINGS_KEYS = set(SETTINGS_BY_KEY)



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


def effective_settings(settings):
    """Every schema setting with its current value (or default)."""
    out = {}
    for f in SETTINGS_SCHEMA:
        key = f["key"]
        v = _get(settings, key, None)
        if key == "permissions.view" and v is None:
            v = settings.get("view_min_user_type")                     # before 0.4.0
        if key == "wan_source" and v == "auto":
            v = "router"                                               # 0.5.1: "auto" meant the router first
        if key == "auto_scan" and v is None and "discovery_paused" in settings:
            v = not settings["discovery_paused"]                       # first 0.5.0 drafts
        if f.get("locked"):
            v = f["default"]
        out[key] = f["default"] if v is None else v
    return out


def validate_settings(new, current=None):
    """Check settings sent from the page. Returns (clean dict in devices.json shape, errors).
    Only schema keys are accepted; nothing is written when there is an error."""
    errors, clean = [], {}
    if not isinstance(new, dict):
        return {}, ["Settings must be an object."]
    unknown = sorted(k for k in new if k not in SETTINGS_KEYS)
    if unknown:
        errors.append("Unknown setting(s): " + ", ".join(unknown[:5]))
    for f in SETTINGS_SCHEMA:
        key, typ, label, rng = f["key"], f["type"], f["label"], f.get("range")
        if key not in new:
            continue
        v = new[key]
        try:
            if f.get("locked") or f.get("preview"):
                if v != f["default"]:
                    raise ValueError("cannot be changed" + (f" yet (planned for {f['preview']})" if f.get("preview") else ""))
            if typ == "bool":
                if not isinstance(v, bool):
                    raise ValueError("must be on or off")
            elif typ == "list":
                if not isinstance(v, list) or any(x not in DEVICE_CATEGORY_KEYS for x in v):
                    raise ValueError("unknown category")
                v = sorted(set(v))
            elif typ == "choice":
                ok = [c[0] for c in f["choices"] if len(c) < 3]          # options marked "planned" are not accepted
                if v not in ok:
                    raise ValueError("is not one of the options")
            elif typ == "str":
                v = str(v).strip()
                if len(v) > f.get("maxlen", 200) or re.search(r"[<>\x00-\x1f]", v):
                    raise ValueError(f"at most {f.get('maxlen', 200)} characters, no < > or control characters")
            elif typ in ("int", "level"):
                if isinstance(v, bool) or not isinstance(v, (int, str)) or not re.fullmatch(r"\d{1,6}", str(v).strip()):
                    raise ValueError("must be a whole number")
                v = int(str(v).strip())
                if not rng[0] <= v <= rng[1]:
                    raise ValueError(f"must be between {rng[0]} and {rng[1]}")
            elif typ in ("cidrs", "ips"):
                if isinstance(v, str):
                    v = [x for x in re.split(r"[\s,]+", v) if x]
                if not isinstance(v, list) or len(v) > 256:
                    raise ValueError("must be a list (at most 256 entries)")
                out = []
                for x in v:
                    net = ipaddress.ip_network(str(x).strip(), strict=False)
                    if net.version != 4:
                        raise ValueError(f"{x}: only IPv4 is scanned")
                    if typ == "cidrs" and net.prefixlen < 16:
                        raise ValueError(f"{x}: too large (at most /16)")
                    out.append(str(net) if "/" in str(x) or typ == "cidrs" else str(net.network_address))
                v = out
            elif typ == "time":
                v = str(v).strip()
                m = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
                if v and not (m and int(m.group(1)) < 24 and int(m.group(2)) < 60):
                    raise ValueError("must be HH:MM, e.g. 03:30, or empty")
                if m:
                    v = f"{int(m.group(1)):02d}:{m.group(2)}"
            elif typ == "digits":
                v = str(v).strip()
                if v and not re.fullmatch(r"\d{1,8}", v):
                    raise ValueError("must be a number, or empty")
        except ValueError as e:
            errors.append(f"{label}: {e}")
            continue
        if "." in key:
            a, b = key.split(".", 1)
            clean.setdefault(a, {})[b] = v
        else:
            clean[key] = v
    if not errors:
        merged = effective_settings({**(current or {}), **clean,
                                     "permissions": {**((current or {}).get("permissions") or {}), **clean.get("permissions", {})}})
        n = scan_target_count(merged["scan_subnets"], merged["scan_exclude"])
        if n > merged["max_targets"]:
            errors.append(f"Allowed networks: {n} addresses, more than the limit of {merged['max_targets']} (Security).")
        if merged["load_crit"] <= merged["load_warn"]:
            errors.append("Load critical must be higher than Load warning.")
        if merged["offpeak"] and merged["offpeak_start"] == merged["offpeak_end"]:
            errors.append("The scan window needs different start and end times.")
        if merged["offpeak"] and (not merged["offpeak_start"] or not merged["offpeak_end"]):
            errors.append("The scan window needs a start and an end time.")
    return (clean, errors) if not errors else ({}, errors)


def scan_targets(subnets, exclude):
    """The addresses a scan will ping (without network and broadcast addresses)."""
    ex = [ipaddress.ip_network(x, strict=False) for x in exclude or []]
    for n in subnets:
        for ip in ipaddress.ip_network(n, strict=False).hosts():
            if not any(ip in e for e in ex):
                yield str(ip)


def scan_target_count(subnets, exclude):
    if not exclude:
        return sum(max(ipaddress.ip_network(n, strict=False).num_addresses - 2, 1) for n in subnets)
    return sum(1 for _ in scan_targets(subnets, exclude))


def permissions(settings):
    """Minimum Zabbix user type per action: 1 = user, 2 = admin, 3 = super admin.
    "permissions": {"view": 3, "edit": 2, "speedtest": 2}; the older "view_min_user_type" still works."""
    p = settings.get("permissions") or {}

    def lvl(key, default, lo):
        try:
            return max(lo, min(3, int(p.get(key, default))))
        except (TypeError, ValueError):
            return default
    view = lvl("view", settings.get("view_min_user_type", 3) or 3, 1)
    # changes never below admin; nobody may do more than they may see (lower values are raised, not refused)
    return {"view": view, "edit": max(view, lvl("edit", 2, 2)), "speedtest": max(view, lvl("speedtest", 2, 1)),
            "settings": max(view, lvl("settings", 3, 2))}


def gateway_code(settings, api_url):
    """spidermap.php as installed, from the settings in devices.json."""
    pm = permissions(settings)
    return (GATEWAY_PHP.replace("__VIEW_MIN_TYPE__", str(pm["view"])).replace("__EDIT_MIN_TYPE__", str(pm["edit"]))
            .replace("__SPEED_MIN_TYPE__", str(pm["speedtest"])).replace("__SETTINGS_MIN_TYPE__", str(pm["settings"]))
            .replace("__API_URL__", api_url.replace("'", "") if api_url else ""))


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
