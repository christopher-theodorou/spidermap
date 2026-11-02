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
import subprocess
import time

import requests

import spidermap_zabbix as at
import spidermap_ports as zp

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICES_FILE = os.path.join(HERE, "devices.json")
STATE_FILE = os.path.join(HERE, "spidermap_state.json")
DATA_DIR = "/var/lib/spidermap/www"   # topology data: NOT in the web folder, served only to logged-in Zabbix users
PAGE, DATA, LIVE, ASSETS = "spidermap.html", "spidermap.json", "spidermap_live.json", "spidermap-assets"
DASHBOARD_NAME = "SpiderMap"
OLD_DASHBOARDS = ["Network topology (live)"]   # earlier versions (zabbix-netmap)

# Optional read-only Proxmox API token (gives names + state of every VM / container)
PVE_URL = os.environ.get("PVE_URL", "")                 # e.g. https://192.168.1.10:8006
PVE_TOKEN_ID = os.environ.get("PVE_TOKEN_ID", "")       # e.g. spidermap@pve!spidermap
PVE_TOKEN_SECRET = os.environ.get("PVE_TOKEN_SECRET", "")

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
             "vm": "device-desktop", "lxc": "box", "guest": "box", "unknown": "help-circle"}

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
                 "core_switch": "", "manual_links": [], "aliases": {}, "scan_subnets": []},
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


EVENTS_FILE = os.path.join(HERE, "spidermap_events_all.json")
PRESENCE_FILE = os.path.join(HERE, "spidermap_presence_all.json")
INET_FILE = os.path.join(HERE, "spidermap_inet.json")
USAGE_FILE = os.path.join(HERE, "spidermap_usage.json")
LOCK_FILE = os.path.join(HERE, ".spidermap.lock")


def ev(kind, sev, text, node=None, t=None):
    """One activity-feed entry. sev: info | ok | warn | bad"""
    return {"t": t or now(), "type": kind, "sev": sev, "text": text, "node": node}


def add_events(evs):
    if not evs:
        return
    import fcntl
    with open(LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        allv = load_json(EVENTS_FILE, [])
        allv.extend(evs)
        cut = now() - 60 * 86400
        allv = [e for e in allv if e["t"] >= cut][-3000:]
        save_json(EVENTS_FILE, allv)
        ensure_data_dir()
        save_json(os.path.join(DATA_DIR, "spidermap_events.json"), allv[-400:])


def mbps_txt(bps):
    m = (bps or 0) / 1e6
    return f"{m / 1000:.2f} Gbps" if m >= 1000 else f"{m:.0f} Mbps" if m >= 10 else f"{m:.2f} Mbps"


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.chmod(DATA_DIR, 0o750)
    gid = _web_gid()
    if gid is not None:
        try:
            os.chown(DATA_DIR, 0, gid)
        except OSError:
            pass


def webdir():
    return next((d for d in zp.WEB_DIRS if os.path.isfile(os.path.join(d, "api_jsonrpc.php"))), None)


def is_private_mac(mac):
    return bool(int(mac[1], 16) & 2)


def port_num(name):
    m = re.search(r"(\d+)$", name or "")
    return int(m.group(1)) if m else 0


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


def classify(name, vendor, mac):
    text = f"{name} {vendor}".lower()
    if mac.startswith("bc:24:11"):
        return "guest"
    for rx, t in CLASSIFY:
        if re.search(rx, text):
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


def pve_guests():
    if not (PVE_URL and PVE_TOKEN_ID and PVE_TOKEN_SECRET):
        return []
    requests.packages.urllib3.disable_warnings()
    s = requests.Session()
    s.verify = False
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

    hosts = at.load_hosts(api)
    switches = [h for h in hosts if h["_snmp"] and str(h["status"]) == "0"]
    host_by_ip = {ip: h for h in hosts for ip in h["_ips"]}

    nets = zp.SCAN_SUBNETS or sorted({str(ipaddress.ip_network(f"{h['_snmp']['ip']}/24", strict=False))
                                      for h in switches if h["_snmp"].get("ip")})
    say(f"Ping sweep {', '.join(nets)}...")
    zp.sweep(nets)
    arp = arp_table()

    sw = {}
    for h in switches:
        try:
            ports, sarp = zp.scan_switch(h, community, hosts)
        except Exception as e:  # noqa: BLE001
            say(f"  ! {h['name']}: {e}")
            continue
        sw[h["hostid"]] = {"host": h, "ports": ports}
        for mac, ip in sarp.items():
            arp.setdefault(mac, (ip, "STALE"))
        say(f"  {h['name']}: {len(ports)} ports, {sum(p['status'] == 'up' for p in ports)} up")
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

    def loc_text(loc):
        if not loc:
            return "unknown"
        if loc["k"] == "gw":
            return "gateway"
        if loc["k"] == "behind":
            return "Wi-Fi / gateway LAN" if loc["of"] == gw_mac else "via " + dname(loc["of"])
        return f"{sw_name.get(loc['sw'], '?')} {port_label(loc['port'])}"

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

    def dname(mac):
        d, c, g = sdev.get(mac, {}), dcfg.get(mac, {}), guest_by_mac.get(mac)
        h = host_by_ip.get(d.get("ip", ""))
        return (c.get("name") or (g and g["name"]) or (h and h["name"])
                or (d.get("dns", "").split(".")[0] if d.get("dns") else "") or d.get("ip") or mac)

    def add(nid, parent, **kw):
        nodes[nid] = {"id": nid, "parent": parent, "flags": [], "details": [], **kw}
        return nodes[nid]

    add("inet", None, name="Internet", sub=public_ip or "public IP", icon="world", kind="inet",
        status="up" if public_ip else "down", details=[["Public IP", public_ip or "unknown"]])

    def device_node(mac, parent, port_ref=None, port_lbl=""):
        d, c, g = sdev.get(mac, {}), dcfg.get(mac, {}), guest_by_mac.get(mac)
        ip, name = d.get("ip", ""), dname(mac)
        vendor = vendor_of(mac)
        typ = c.get("type") or (g and g["kind"]) or classify(f"{name} {d.get('dns', '')}", vendor, mac)
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
            pinfo = next((p for p in sw[port_ref[0]]["ports"] if port_num(p["name"]) == port_ref[1]), None)
            conn = f"{sw_name[port_ref[0]]} · {port_label(pinfo['name']) if pinfo else port_lbl}" + \
                   (f" ({pinfo['name']}) · {pinfo['speed']}" if pinfo and pinfo["speed"] else "")
        elif parent in nodes:
            conn = "via " + nodes[parent]["name"]
        det = [["IP", ip or "-"], ["MAC", mac], ["Manufacturer", vendor or "-"], ["Type", typ],
               ["Connection", conn or "-"]]
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
                port=port_lbl, port_ref=port_ref, vendor=vendor, conn=conn or "-",
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
            parent, lbl = "s-" + par, f"{port_label(pa)} ↔ {port_num(pb)}"
            pref = [par, port_num(pa)]
        else:
            parent, lbl = gw_id, (port_label(gw_loc[1]) if gw_loc and sid == root_sw else "")
            pref = [sid, port_num(gw_loc[1])] if gw_loc and sid == root_sw else None
        add("s-" + sid, parent, name=h["name"], sub="." + h["_snmp"]["ip"].split(".")[-1], icon="server-2",
            kind="switch", sw=sid, status="up", port=lbl, port_ref=pref, zbx=sid, flip=sid not in parent_of,
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
            p = next((p for p in sw[sid]["ports"] if port_num(p["name"]) == int(pn)), None) if sid else None
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
    for (sid, pname), macs in sorted(port_groups.items(), key=lambda x: (x[0][0], port_num(x[0][1]))):
        pr, lbl = [sid, port_num(pname)], port_label(pname)
        live_macs = [m for m in macs if m in online]
        if len(live_macs) > 1 and (sid, pname) not in host_port:
            gid = f"g-{sid}-{port_num(pname)}"
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
    rest = [m for m in set(dcfg) | set(sdev) if m not in placed and m != gw_mac
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
            clients.setdefault(port_num(gw_loc[1]), []).insert(0, nodes[by_mac[gw_mac]]["name"] + " (gateway)")
        for (a, b), (pa, _pb) in sw_link.items():
            if a == sid:
                clients.setdefault(port_num(pa), []).append(sw_name[b])
        switches_out[sid] = {"name": sw_name[sid], "ports": [
            {"n": port_num(p["name"]), "label": port_label(p["name"]), "full": p["name"], "status": p["status"],
             "speed": p["speed"], "alias": p["alias"], "ifindex": p.get("ifindex"),
             "downgrade": downgrade.get(f"{sid}:{p['name']}", ""),
             "clients": clients.get(port_num(p["name"]), [])}
            for p in sw[sid]["ports"]]}

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
                evs.append(ev("security", "warn", f"Unknown device plugged into {loc_text(loc)}: {who} [{mac}]", nid(mac)))
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
        pres[mac] = [x for x in pres[mac] if x[1] >= t - 30 * 86400]
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
            "switches": switches_out, "zabbix_url": "zabbix.php"}
    if webd:
        install_page(webd)
    ensure_data_dir()
    save_json(os.path.join(DATA_DIR, DATA), data)
    say(f"{len(nodes)} nodes, {len(online)} devices online, {len(events['new'])} new, "
        f"{len(events['changed'])} changed, {len(down)} important down")
    if not baseline:
        say("First run: everything seen now is the baseline, so nothing is flagged as new.")
    return events, down, hosts


# --------------------------------------------------------------------------- #
# Live refresh (every minute): port status + traffic from Zabbix, ping important devices
# --------------------------------------------------------------------------- #
EDITS_DIR = "/var/lib/spidermap"                         # root only
INBOX = os.path.join(EDITS_DIR, "inbox")                  # the only place the web server may write
EDITS_FILE = os.path.join(INBOX, "edits.json")
HIST_FILE = os.path.join(HERE, "spidermap_hist.json")
HIST_LEN = 90                                     # minutes of history kept for the small graphs
LAT_TARGETS = [("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8"), ("Quad9", "9.9.9.9")]
GUEST_RX = r"(?i)(qemu|lxc|vm)\s*\["
STAT_RULES = [("cpu", "CPU", r"(?i)\bcpu\b.*(util|usage)"),
              ("mem", "Memory", r"(?i)memory.*(util|usage|used)"),
              ("disk", "Disk", r"(?i)(space utilization|root ?fs|root filesystem|filesystem|disk|storage).*(util|usage|used)"),
              ("temp", "Temperature", r"(?i)temperature"),
              ("uptime", "Uptime", r"(?i)\buptime\b")]


def apply_edits():
    """Merge edits saved from the page (spidermap_api.php) into devices.json."""
    edits = load_json(EDITS_FILE, {})
    if not edits:
        return {}
    cfg = load_json(DEVICES_FILE, DEFAULT_DEVICES)
    devs = cfg.setdefault("devices", {})
    for mac, e in edits.items():
        d = devs.setdefault(mac.lower(), {})
        for k, v in e.items():
            if k in ("by", "at"):
                continue
            if v in ("", None, False) and k != "important":
                d.pop(k, None)
            else:
                d[k] = v
        if d.get("important") is False:
            d.pop("important")
    save_json(DEVICES_FILE, cfg)
    with open(EDITS_FILE, "w", encoding="utf-8") as f:   # in place: keeps the web server's ownership
        f.write("{}")
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

    # switch ports from Zabbix (status, traffic, errors)
    names = {"Operational status": "st", "Bits received": "rx", "Bits sent": "tx",
             "Inbound packets with errors": "ein", "Outbound packets with errors": "eout"}
    ports = {}
    for sid in data["switches"]:
        for it in api.call("item.get", {"hostids": sid, "output": ["name", "lastvalue"], "filter": {"status": 0},
                                        "search": {"name": "Interface "}}):
            m = re.match(r"Interface (\S+?)\(.*\): (.+)$", it["name"])
            v = _f(it["lastvalue"])
            if m and m.group(2) in names and v is not None:
                ports.setdefault(sid, {}).setdefault(str(port_num(m.group(1))), {})[names[m.group(2)]] = v

    rt = load_json(os.path.join(DATA_DIR, RT), {}) or {}
    rt_fresh = bool(rt) and t - rt.get("t", 0) < 60
    hist = load_json(HIST_FILE, {})

    def node_on_port(sid, n):
        return next((x["id"] for x in data["nodes"] if x.get("port_ref") == [sid, int(n)] and x["kind"] != "switch"), None)

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
            n, issues = str(p["n"]), []
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
            n = str(p["n"])
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
SERVICE = "/etc/systemd/system/spidermap-rt.service"


def snmp_get(auth, target, oids):
    out = {}
    for i in range(0, len(oids), 30):                      # keep each request small
        r = subprocess.run(["snmpget", "-t", "2", "-r", "0", "-On", "-Oq", "-Oe", *auth, target, *oids[i:i + 30]],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            oid, _, val = line.partition(" ")
            v = _f(re.sub(r"[^\d.]", "", val))
            if v is not None:
                out[oid.lstrip(".")] = v
    return out


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


def run_speedtest(api, zh, state):
    state.update({"running": True, "started": now(), "error": ""})
    try:
        server = load_json(DEVICES_FILE, {}).get("settings", {}).get("speedtest_server", "")
        res = dict(_speedtest_once(server), at=now())
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


def month_uptime(inet, t):
    """uptime % since the start of the month (or since tracking began)"""
    ms = int(datetime.datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    since = max(ms, inet.get("since", t))
    if t - since < 60:
        return None, []
    outs = [o for o in inet.get("outages", []) if o[1] >= since]
    down = sum(min(e, t) - max(s_, since) for s_, e in outs)
    if inet.get("down_since"):
        down += t - max(inet["down_since"], since)
    return round(100 * (1 - down / (t - since)), 3), outs


def rt_loop(api):
    import threading
    from collections import deque
    webd = webdir()
    hosts, data, last_cfg, prev = [], None, 0, {}
    hist = {"wan": [], "ports": {}, "lat": []}
    targets, last_lat, speed = {}, 0, {"running": False, "error": ""}
    daily_done, last_save = "", 0
    prev_st, flap_t = {}, {}
    inet = load_json(INET_FILE, {})
    inet.setdefault("since", now())
    inet.setdefault("outages", [])
    fails, gw_fails, loss_win = 0, 0, deque(maxlen=8)
    usage = load_json(USAGE_FILE, {})
    while True:
        t0 = time.time()
        if t0 - last_cfg > 300 or data is None:
            try:
                hosts = at.load_hosts(api)
                data = load_json(os.path.join(DATA_DIR, DATA), None)
            except Exception as e:  # noqa: BLE001
                print("config reload failed:", e, flush=True)
            last_cfg = t0
        if not data:
            time.sleep(RT_EVERY)
            continue
        t = int(t0)
        evs = []
        day, month = datetime.datetime.now().strftime("%Y-%m-%d"), datetime.datetime.now().strftime("%Y-%m")
        if usage.get("day") != day:
            usage.update({"day": day, "d": {}})
        if usage.get("month") != month:
            usage.update({"month": month, "m": {}})
        hb = {h["hostid"]: h for h in hosts}
        port_name = {}
        ports = {}
        for sid, sw in data["switches"].items():
            h = hb.get(sid)
            if not h or not h["_snmp"]:
                continue
            idx = {str(p["ifindex"]): str(p["n"]) for p in sw["ports"] if p.get("ifindex")}
            for p in sw["ports"]:
                port_name[(sid, str(p["n"]))] = f"{sw['name']} {p['label']}" + \
                    (f" ({', '.join(p['clients'])})" if p["clients"] else "")
            oids = [f"1.3.6.1.2.1.31.1.1.1.{c}.{i}" for i in idx for c in (6, 10)] + \
                   [f"1.3.6.1.2.1.2.2.1.8.{i}" for i in idx]
            try:
                auth, target = at.snmp_auth(h["_snmp"], h["_macros"])
                vals = snmp_get(auth, target, oids)
            except Exception as e:  # noqa: BLE001
                print(f"{h['name']}: {e}", flush=True)
                continue
            now_t = time.time()
            for i, n in idx.items():
                rx_o, tx_o = vals.get(f"1.3.6.1.2.1.31.1.1.1.6.{i}"), vals.get(f"1.3.6.1.2.1.31.1.1.1.10.{i}")
                st = vals.get(f"1.3.6.1.2.1.2.2.1.8.{i}")
                key = (sid, n)
                # port up/down changes -> activity feed + flap counter
                if st in (1, 2):
                    was = prev_st.get(key)
                    if was in (1, 2) and was != st:
                        flap_t.setdefault(key, deque()).append(t)
                        imp = any("important" in x.get("flags", []) for x in data["nodes"]
                                  if x.get("port_ref") == [sid, int(n)])
                        evs.append(ev("port", "ok" if st == 1 else ("bad" if imp else "info"),
                                      f"{port_name.get(key, n)} is {'up' if st == 1 else 'down'}",
                                      next((x["id"] for x in data["nodes"] if x.get("port_ref") == [sid, int(n)]
                                            and x["kind"] != "switch"), None)))
                    prev_st[key] = st
                if rx_o is None or tx_o is None:
                    continue
                if key in prev:
                    pt, prx, ptx = prev[key]
                    dt = now_t - pt
                    if dt > 0 and rx_o >= prx and tx_o >= ptx:
                        ports.setdefault(sid, {})[n] = {"rx": (rx_o - prx) * 8 / dt, "tx": (tx_o - ptx) * 8 / dt,
                                                        "st": st or 0}
                        for per in ("d", "m"):
                            u = usage.setdefault(per, {}).setdefault(sid, {}).setdefault(n, [0, 0])
                            u[0] += rx_o - prx
                            u[1] += tx_o - ptx
                prev[key] = (now_t, rx_o, tx_o)
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
        wan = ports.get(root["port_ref"][0], {}).get(str(root["port_ref"][1])) if root else None
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
            loss = sum(v["loss"] for v in net) / max(1, len(net))
            jit = [v["jit"] for v in net if v["jit"] is not None]
            ms = [v["ms"] for v in net if v["ms"] is not None]
            hist["lat"].append([t, round(sum(ms) / len(ms), 1) if ms else None, round(loss, 1),
                                round(sum(jit) / len(jit), 1) if jit else None])
            del hist["lat"][:-240]
            fails = 0 if up else fails + 1
            if fails == 2 and not inet.get("down_since"):
                inet["down_since"] = t - 15
                evs.append(ev("internet", "bad", "Internet connection lost", "inet"))
            if up and inet.get("down_since"):
                s0 = inet.pop("down_since")
                inet["outages"] = (inet["outages"] + [[s0, t]])[-200:]
                evs.append(ev("internet", "ok", f"Internet back after {_dur(t - s0)}", "inet"))
            gwv = targets.get("Gateway")
            if gwv:
                gw_fails = gw_fails + 1 if gwv["ms"] is None else 0
                if gw_fails == 2:
                    evs.append(ev("internet", "bad", f"Gateway {gw['name']} is not responding", gw["id"]))
                if gwv["ms"] is not None and gw_fails >= 2:
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
        due = daily and datetime.datetime.now().strftime("%H:%M") >= daily and daily_done != today
        if (os.path.exists(SPEED_REQ) or due) and not speed["running"]:
            try:
                os.remove(SPEED_REQ)
            except FileNotFoundError:
                pass
            if due:
                daily_done = today
            zh = next((h for h in hosts if h["_role"] == "zabbix"), None)
            threading.Thread(target=run_speedtest, args=(api, zh, speed), daemon=True).start()
        add_events(evs)
        if t0 - last_save > 60:
            save_json(USAGE_FILE, usage)
            last_save = t0
        sh = load_json(SPEED_FILE, [])
        upt, outs = month_uptime(inet, t)
        save_json(os.path.join(DATA_DIR, RT), {
            "t": t, "every": RT_EVERY, "ports": ports, "flaps": flaps,
            "wan": {"down": wan["rx"], "up": wan["tx"]} if wan else None,
            "latency": {k: v["ms"] for k, v in targets.items() if k != "Gateway"}, "targets": targets,
            "inet": {"up": not inet.get("down_since"), "down_since": inet.get("down_since"),
                     "last_outage": inet["outages"][-1] if inet["outages"] else None,
                     "outages_month": outs[-30:], "uptime_month": upt, "since": inet.get("since"),
                     "loss_warn": inet.get("loss_warn", False), "loss": inet.get("loss", 0)},
            "usage": {"day": usage.get("day"), "month": usage.get("month"), "d": usage.get("d", {}), "m": usage.get("m", {})},
            "hist": hist, "speedtest": {"running": speed["running"], "started": speed.get("started"),
                                        "error": speed.get("error", ""), "last": sh[-1] if sh else None,
                                        "history": sh[-12:]}})
        time.sleep(max(0.5, RT_EVERY - (time.time() - t0)))


def install_service():
    unit = f"""[Unit]
Description=SpiderMap real-time collector (port traffic every {RT_EVERY}s, speed tests)
After=network-online.target

[Service]
WorkingDirectory={HERE}
Environment=HOME=/root
ExecStart=/bin/bash -c '. ./zabbix.env && exec python3 spidermap.py --rt'
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    with open(SERVICE, "w", encoding="utf-8") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"])
    subprocess.run(["systemctl", "enable", "--now", "spidermap-rt.service"])
    subprocess.run(["systemctl", "restart", "spidermap-rt.service"])
    print("Real-time service installed and started (systemctl status spidermap-rt)")


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


API_PHP = r"""<?php
// SpiderMap API - the map data is only for logged-in Zabbix users, changes only for Zabbix admins.
// Generated by spidermap.py: changes here are overwritten on update.
header('Content-Type: application/json');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
header('Referrer-Policy: same-origin');
const DATA_DIR = '/var/lib/spidermap/www';
const STATE_DIR = '/var/lib/spidermap/inbox';   // the only folder the web server may write to
const VIEW_MIN_TYPE = __VIEW_MIN_TYPE__;   // 1 = any Zabbix user, 2 = admins, 3 = super admins (devices.json: view_min_user_type)
const FILES = ['map' => 'spidermap.json', 'live' => 'spidermap_live.json', 'rt' => 'spidermap_rt.json',
               'events' => 'spidermap_events.json', 'presence' => 'spidermap_presence.json'];

function fail($code, $msg) { http_response_code($code); echo json_encode(['ok' => false, 'error' => $msg]); exit; }

function api_call($method, $params, $bearer = null) {
  $https = !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off';
  $url = ($https ? 'https' : 'http') . '://127.0.0.1:' . $_SERVER['SERVER_PORT']
       . rtrim(dirname($_SERVER['SCRIPT_NAME']), '/') . '/api_jsonrpc.php';
  $hdr = "Content-Type: application/json-rpc\r\n" . ($bearer ? "Authorization: Bearer $bearer\r\n" : '');
  $ctx = stream_context_create([
    'http' => ['method' => 'POST', 'header' => $hdr, 'timeout' => 5, 'ignore_errors' => true,
               'content' => json_encode(['jsonrpc' => '2.0', 'method' => $method, 'params' => $params, 'id' => 1])],
    'ssl' => ['verify_peer' => false, 'verify_peer_name' => false]]);
  return json_decode(@file_get_contents($url, false, $ctx), true);
}

// Who is logged in? Checked against the Zabbix API, cached for 60 s so the 5-second polling stays cheap.
// extend=false: polling the map never keeps an expired Zabbix session alive.
function zabbix_user() {
  $raw = strtr((string)($_COOKIE['zbx_session'] ?? ''), ' ', '+');   // some proxies turn + into a space
  $sess = json_decode(base64_decode($raw, true) ?: '', true);
  $sid = is_array($sess) ? (string)($sess['sessionid'] ?? '') : '';
  if (!preg_match('/^[A-Za-z0-9]{16,128}$/', $sid)) fail(401, 'login');
  $cdir = STATE_DIR . '/sess';
  $cache = $cdir . '/' . hash('sha256', $sid);
  if (is_file($cache) && filemtime($cache) > time() - 60) {
    $u = json_decode((string)@file_get_contents($cache), true);
    if (is_array($u)) return $u;
  }
  $r = api_call('user.checkAuthentication', ['sessionid' => $sid, 'extend' => false]);
  if (empty($r['result']['userid'])) { @unlink($cache); fail(401, 'login'); }
  $type = isset($r['result']['type']) ? (int)$r['result']['type'] : 0;
  if (!$type) {   // older answers have no type: ask for the user's role
    $q = api_call('user.get', ['output' => ['userid'], 'userids' => [$r['result']['userid']], 'selectRole' => ['type']], $sid);
    $type = (int)($q['result'][0]['role']['type'] ?? 1);
  }
  $u = ['id' => $r['result']['userid'], 'name' => (string)($r['result']['username'] ?? ''), 'type' => $type];
  if (!is_dir($cdir)) @mkdir($cdir, 0700, true);
  @file_put_contents($cache, json_encode($u), LOCK_EX);
  if (mt_rand(1, 50) === 1) foreach (glob($cdir . '/*') ?: [] as $f) if (filemtime($f) < time() - 3600) @unlink($f);
  return $u;
}

$user = zabbix_user();
if (strtolower($user['name']) === 'guest') fail(403, 'The Zabbix guest account cannot open SpiderMap. Log in with your own user.');
if ($user['type'] < VIEW_MIN_TYPE) fail(403, 'Your Zabbix user is not allowed to view SpiderMap.');

// ---- read the map data
if ($_SERVER['REQUEST_METHOD'] === 'GET') {
  $f = FILES[$_GET['data'] ?? ''] ?? null;
  if (!$f) fail(400, 'Unknown data set');
  $path = DATA_DIR . '/' . $f;
  if (!is_file($path)) fail(404, 'No data yet');
  readfile($path);
  exit;
}

// ---- changes: same-origin only, Zabbix admins only
if ($_SERVER['REQUEST_METHOD'] !== 'POST') fail(405, 'GET or POST only');
if (($_SERVER['HTTP_X_SPIDERMAP'] ?? '') !== '1') fail(403, 'Missing request header');   // blocks cross-site forms (CSRF)
$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
if ($origin !== '' && parse_url($origin, PHP_URL_HOST) !== parse_url('//' . ($_SERVER['HTTP_HOST'] ?? ''), PHP_URL_HOST))
  fail(403, 'Cross-site request refused');
if ($user['type'] < 2) fail(403, 'Only Zabbix admins can make changes.');
$in = json_decode(file_get_contents('php://input'), true);
if (!is_array($in)) fail(400, 'Bad request');

if (($in['action'] ?? '') === 'speedtest') {
  if (!@touch(STATE_DIR . '/speedtest.request')) fail(500, 'Cannot write ' . STATE_DIR);
  echo json_encode(['ok' => true]); exit;
}

$mac = strtolower((string)($in['mac'] ?? ''));
if (!preg_match('/^([0-9a-f]{2}:){5}[0-9a-f]{2}$/', $mac)) fail(400, 'Bad MAC address');
$e = [];
foreach (['name', 'type', 'url', 'pin'] as $k)
  if (array_key_exists($k, $in)) $e[$k] = function_exists('mb_substr') ? mb_substr(trim((string)$in[$k]), 0, 200)
                                                                      : substr(trim((string)$in[$k]), 0, 200);
if (isset($e['url']) && $e['url'] !== '' && !preg_match('#^https?://#i', $e['url'])) fail(400, 'Web UI address must start with http:// or https://');
if (array_key_exists('important', $in)) $e['important'] = (bool)$in['important'];
$fh = @fopen(STATE_DIR . '/edits.json', 'c+');
if (!$fh) fail(500, 'Cannot write ' . STATE_DIR . '/edits.json');
flock($fh, LOCK_EX);
$cur = json_decode(stream_get_contents($fh), true) ?: [];
$cur[$mac] = array_merge($cur[$mac] ?? [], $e, ['by' => $user['name'], 'at' => time()]);
ftruncate($fh, 0); rewind($fh); fwrite($fh, json_encode($cur, JSON_PRETTY_PRINT)); fflush($fh);
flock($fh, LOCK_UN); fclose($fh);
echo json_encode(['ok' => true, 'by' => $user['name']]);
"""


def web_user():
    import pwd
    for u in ("www-data", "nginx", "apache", "http"):
        try:
            return pwd.getpwnam(u)
        except KeyError:
            continue
    return None


def install_page(webd):
    src = os.path.join(HERE, PAGE)
    if os.path.isfile(src):
        shutil.copyfile(src, os.path.join(webd, PAGE))
        os.chmod(os.path.join(webd, PAGE), 0o644)
    adir = os.path.join(webd, ASSETS)
    os.makedirs(adir, exist_ok=True)
    os.chmod(adir, 0o755)
    for f in ("spidermap-icon.svg", "spidermap-icon-dark.svg", "spidermap-favicon.svg"):
        if os.path.isfile(os.path.join(HERE, "assets", f)):
            shutil.copyfile(os.path.join(HERE, "assets", f), os.path.join(adir, f))
            os.chmod(os.path.join(adir, f), 0o644)
    vmin = int(load_json(DEVICES_FILE, {}).get("settings", {}).get("view_min_user_type", 1) or 1)
    code = API_PHP.replace("__VIEW_MIN_TYPE__", str(max(1, min(3, vmin))))
    php = os.path.join(webd, "spidermap_api.php")
    if not os.path.isfile(php) or open(php, encoding="utf-8").read() != code:
        with open(php, "w", encoding="utf-8") as f:
            f.write(code)
        os.chmod(php, 0o644)
    # earlier versions served the data straight from the web folder: remove it there
    for f in ("spidermap_edit.php", "spidermap.json", "spidermap_live.json", "spidermap_rt.json",
              "spidermap_events.json", "spidermap_presence.json"):
        try:
            os.remove(os.path.join(webd, f))
        except FileNotFoundError:
            pass
    ensure_data_dir()
    os.makedirs(EDITS_DIR, exist_ok=True)
    os.chown(EDITS_DIR, 0, 0)
    os.chmod(EDITS_DIR, 0o755)
    os.makedirs(os.path.join(INBOX, "sess"), exist_ok=True)
    old = os.path.join(EDITS_DIR, "edits.json")              # earlier versions kept edits one folder up
    if os.path.isfile(old) and not os.path.isfile(EDITS_FILE):
        shutil.move(old, EDITS_FILE)
    if not os.path.isfile(EDITS_FILE):
        with open(EDITS_FILE, "w", encoding="utf-8") as f:
            f.write("{}")
    wu = web_user()
    for path in (INBOX, EDITS_FILE, os.path.join(INBOX, "sess")):
        try:
            if wu:
                os.chown(path, wu.pw_uid, wu.pw_gid)
            os.chmod(path, 0o700 if os.path.isdir(path) else 0o600)
        except OSError:
            pass


def migrate_old_install(api, webd):
    """Clean up after zabbix-netmap (the old name) so nothing is left behind."""
    for d in api.call("dashboard.get", {"output": ["dashboardid"], "filter": {"name": OLD_DASHBOARDS}}):
        api.call("dashboard.delete", [d["dashboardid"]])
        print("  removed old dashboard (replaced by 'SpiderMap')")
    old_page = os.path.join(webd, "netmap.html")
    if os.path.isfile(old_page):
        with open(old_page, "w", encoding="utf-8") as f:   # old bookmarks keep working
            f.write('<!doctype html><meta http-equiv="refresh" content="0;url=spidermap.html">'
                    '<a href="spidermap.html">SpiderMap has moved here</a>')
        for f in ("netmap.json", "netmap_live.json", "netmap_rt.json", "netmap_events.json",
                  "netmap_presence.json", "netmap_edit.php"):
            try:
                os.remove(os.path.join(webd, f))
            except FileNotFoundError:
                pass
        shutil.rmtree(os.path.join(webd, "netmap-assets"), ignore_errors=True)
        print("  old netmap.html now redirects to spidermap.html")
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
        d.add("Topology", "url", "Network", 0, 0, 72, 20, [{"url": (1, PAGE)}])
        print(f"Dashboard created: {DASHBOARD_NAME}")
    if not os.path.isfile(OOKLA) and not shutil.which("speedtest-cli"):
        print("For speed tests: bash install.sh --update (installs a speed test program)")
    install_service()
    print("\nSetup done. install.sh adds the scheduled jobs (or see README.md to add them by hand).")


def main():
    ap = argparse.ArgumentParser(description="UniFi-style live network topology for Zabbix")
    ap.add_argument("--setup", action="store_true", help="install page, icons, Zabbix items, dashboard")
    ap.add_argument("--live", action="store_true", help="refresh live status and traffic only")
    ap.add_argument("--rt", action="store_true", help="run the real-time collector (used by the service)")
    ap.add_argument("--install-service", action="store_true", help="install/restart the real-time service")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--community", help="SNMP v1/v2c community override")
    args = ap.parse_args()
    if not at.ZABBIX_TOKEN:
        raise SystemExit("Run '. ./zabbix.env' first.")
    api = at.Zabbix(at.ZABBIX_URL, at.ZABBIX_TOKEN, at.VERIFY_TLS)
    if args.setup:
        setup(api)
        args.quiet = False
    if args.rt:
        rt_loop(api)
        return
    if args.install_service:
        install_service()
        return
    if args.live:
        live_refresh(api)
        return
    events, down, hosts = full_scan(api, args.community, args.quiet)
    push_events(api, hosts, events, down)
    live_refresh(api)


if __name__ == "__main__":
    main()
