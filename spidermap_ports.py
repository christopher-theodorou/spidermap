#!/usr/bin/env python3
"""
spidermap_ports.py
---------------
For every port on every SNMP switch in Zabbix:
  status (up / down / disabled), speed, description,
  LLDP neighbour, and the devices learned on that port (MAC -> IP -> name).

Results:
  * printed in the terminal
  * an HTML page next to the Zabbix frontend (autotopo_ports.html)
  * a dashboard "Switch ports (auto)" with that page + live port-status honeycombs

Needs spidermap_zabbix.py in the same folder (it reuses its API/SNMP code).
    cd /opt/autotopo && source ./zabbix.env && python3 spidermap_ports.py
"""
import argparse
import concurrent.futures as cf
import datetime
import html
import ipaddress
import os
import re
import socket
import subprocess
import time
from urllib.parse import urlparse

import spidermap_zabbix as at

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
SCAN_SUBNETS = list(at._LS.get("scan_subnets", []))   # devices.json settings; empty = the /24 of each switch
MAX_LIST = 8               # list devices one by one up to this many per port
DASHBOARD_NAME = "Switch ports (auto)"
HTML_NAME = "autotopo_ports.html"
WEB_DIRS = ["/usr/share/zabbix/ui", "/usr/share/zabbix", "/var/www/html/zabbix", "/var/www/html"]
PHYS_TYPES = {"6", "117", "161"}   # ethernet, gigabitEthernet, LAG (Eth-Trunk)
# Interfaces that are never physical switch ports, whatever ifType the device reports
# (TP-Link, for example, reports its VLAN interface as Ethernet).
VIRTUAL_RX = re.compile(r"(?i)^(vlan|vlanif|vlan-interface|bridge|br-|lo$|loopback|inloopback|null|tunnel|tun\d|"
                        r"meth|mgmt|management|stackport|stack-port|appgigabit|cpu|internal|wlan|wifi|veth|"
                        r"docker|virtual|sit\d|gre|ip6tnl)")
LAG_RX = re.compile(r"(?i)^(port-channel|po\d|eth-trunk|bond|lag|trk|ae\d|aggregat)")

OID = {
    "name": "1.3.6.1.2.1.31.1.1.1.1", "alias": "1.3.6.1.2.1.31.1.1.1.18",
    "type": "1.3.6.1.2.1.2.2.1.3", "oper": "1.3.6.1.2.1.2.2.1.8",
    "admin": "1.3.6.1.2.1.2.2.1.7", "speed": "1.3.6.1.2.1.31.1.1.1.15",
    "bport": "1.3.6.1.2.1.17.1.4.1.2", "qfdb": "1.3.6.1.2.1.17.7.1.2.2.1.2",
    "dfdb": "1.3.6.1.2.1.17.4.3.1.2", "arp": "1.3.6.1.2.1.4.22.1.2",
    # VLANs (Q-BRIDGE-MIB) and link aggregation (IEEE8023-LAG-MIB); switches without them simply have none
    "vname": "1.3.6.1.2.1.17.7.1.4.3.1.1", "vegress": "1.3.6.1.2.1.17.7.1.4.3.1.2",
    "vuntag": "1.3.6.1.2.1.17.7.1.4.3.1.4", "pvid": "1.3.6.1.2.1.17.7.1.4.5.1.1",
    "vfdb": "1.3.6.1.2.1.17.7.1.4.2.1.3", "lagof": "1.2.840.10006.300.43.1.2.1.1.13",
}
VLANS = {}          # hostid -> {vlan id: name}, from the last scan of each switch
CDP_ON = False      # set from Settings > Topology > CDP
BRIDGE = {}         # hostid -> {"mac": bridge MAC, "root": root bridge MAC, "rootport": bridge port, "bport": {bp: ifIndex}}
V6 = {}             # hostid -> {mac: [IPv6 addresses]} from the switch's / router's neighbour table
POE_TOTAL = {}      # hostid -> watts the switch delivers over PoE
TTL = {}            # ip -> TTL of its ping reply, from the last sweep
IFMACS = {}         # hostid -> the MACs of the switch's own interfaces (ifPhysAddress): who it is in other MAC tables
ROLE = {}           # hostid -> ("ap", why) for a host that is an access point (Wi-Fi interfaces, its own description)
WIFI_IF = re.compile(r"^(wlan|wifi|ath|ra|rai|wl|wlp|radio|apcli|wdev|mlan)\d", re.I)
# Access point models, by family - for the system's own description, LLDP descriptions, and devices without SNMP
# (their DHCP / DNS name). With thanks to Nikolaos Karanikolas's multi-vendor review. A family with a vendor pattern
# is ambiguous on its own ("AP-515", "WAP571", "R550") and counts only when that vendor is named too. The brand alone
# never counts: the same vendors make switches, controllers and routers (AP_NOT).
AP_FAMILIES = (
    ("TP-Link EAP", r"eap[0-9]{3,4}(?:[-_](?:outdoor|wall|hd))?", None),
    ("TP-Link CAP", r"cap[0-9]{3,4}", r"tp[- ]?link"),
    ("UniFi AP", r"uap(?:[-_](?:ac|nano|iw|hd|lr|pro|mesh|m|xg)){0,4}|u[67][-_](?:pro|lite|lr|mesh|enterprise|iw|in[-_]wall"
                 r"|outdoor|plus|long[-_]range)(?:[-_][a-z0-9]+)*|uk[-_]ultra|unifi ap|unifi u[67]", None),
    ("Grandstream AP", r"gwn76[0-9]{2}[a-z]{0,3}", None),
    ("Cisco Aironet", r"air[-_](?:ap|cap|lap)[0-9]{3,4}[a-z0-9]*(?:[-_][a-z0-9]+)*|aironet", None),
    ("Cisco Catalyst AP", r"c91[0-9]{2}[a-z0-9]*(?:[-_][a-z0-9]+)*|cw91[0-9]{2}[a-z0-9]*(?:[-_][a-z0-9]+)*", None),
    ("Cisco Business AP", r"cbw[0-9]{3}[a-z]{0,3}|wap[0-9]{3}[a-z]{0,2}", r"cisco"),
    ("Cisco Meraki", r"mr[0-9]{2}[a-z]?", r"meraki|cisco"),
    ("D-Link AP", r"dap[-_](?:[23][0-9]{3}|x(?:28|30)[0-9]{2}|e36[0-9]{2})[a-z]*|dba[-_][a-z]?[0-9]{3,4}[a-z]*|dwl[-_][0-9]{3,4}ap", None),
    ("NETGEAR / Zyxel AP", r"wa[xc][0-9]{3}[a-z]{0,2}|wndap[0-9]{3}", r"netgear|zyxel"),
    ("Zyxel AP", r"nwa[0-9]{2,4}[a-z0-9-]*", None),
    ("Huawei AP", r"airengine[-_ ]?[0-9]{4}[a-z0-9]*(?:[-_][a-z0-9]+)*", None),
    ("MikroTik AP", r"cap(?:[-_ ]?ac|[-_ ]?ax|[-_ ]?xl|[-_ ]?lite)?|wap(?:[-_ ]?ac|[-_ ]?ax|[-_ ]?lte)?", r"mikrotik"),
    ("Aruba AP", r"(?:iap|ap)[-_][0-9]{3}[a-z]{0,2}|ap[0-9]{2}", r"aruba|instant.?on"),
    ("Ruckus AP", r"[rth][0-9]{3}[a-z]{0,2}|zoneflex", r"ruckus|zoneflex"),
    ("EnGenius AP", r"ecw[0-9]{3}|ews[0-9]{3}ap", None),
    ("Cambium AP", r"cnpilot|e[0-9]{3}[a-z]?|xv?2[-_][0-9]{2,3}[a-z]?", r"cambium|cnpilot"),
    ("Fortinet AP", r"fap[-_][0-9]{2,4}[a-z]{0,3}|fortiap(?:[-_ ][a-z0-9]+)*", None),
    ("Juniper Mist AP", r"mist ap[0-9]*", None),
    ("access point", r"access[ _-]?point|wireless ap", None),
)
AP_NOT = re.compile(r"(?<![a-z0-9])(usw|unifi switch|us-[0-9]|udm|ucg|uxg|uck|cloud ?key|oc[0-9]{3}|controller|manager|wlc|"
                    r"gwn70[0-9]{2}|gwn78[0-9]{2}|tl-sg|sg[0-9]{3,4}|er[0-9]{3,4}|catalyst 9[2-6]|crs[0-9]{3}|"
                    r"fortigate|fortiswitch|fgt|fsw-|switch|eap-tls|eap-ttls|eap-peap)(?![a-z0-9])", re.I)


def ap_model(text, vendor=""):
    """The access-point family named in a text (a description, a host or DHCP name), or None - never for a switch,
    controller or router of the same vendor; an ambiguous model only when its vendor is named (text or vendor)."""
    t = str(text or "")
    if not t or AP_NOT.search(t):
        return None
    context = f"{t} {vendor or ''}"
    for family, pattern, need in AP_FAMILIES:
        if need and not re.search(need, context, re.I):
            continue
        m = re.search(r"(?<![a-z0-9])(?:" + pattern + r")(?![a-z0-9])", t, re.I)
        if m:
            return m.group(0) if family != "access point" else "access point"
    return None


def lldp_says_ap(caps):
    """LLDP 'system capabilities enabled' includes WLAN Access Point (bit 3) - text or hex, as snmpwalk prints it."""
    c = str(caps or "").strip()
    if "(" in c or re.search(r"[g-zG-Z]{3,}", c.split(":", 1)[-1] if c.lower().startswith(("hex", "bits")) else c):
        low = c.lower().replace(" ", "")                             # named bits: only the names count
        return "wlanaccesspoint" in low and "router(" not in low      # router + AP: a Wi-Fi router, not a plain AP
    h = c.split(":", 1)[-1] if ":" in c else c
    h = re.sub(r"[^0-9a-fA-F]", "", re.sub(r"(?i)\b0x", "", h))
    if len(h) < 2:
        return False
    b = int(h[:2], 16)
    return bool(b & 0x10) and not b & 0x08                         # WLAN access point (bit 3), not also a router (bit 4)


def ap_evidence(iftypes, descrs, sysdescr="", own_caps=""):
    """Why a host is an access point, or None. iftypes / descrs: {ifIndex: value} from IF-MIB.
    Wi-Fi interfaces (ifType 71, ieee80211) are the proof; Wi-Fi interface names or the system's own description
    (EAP..., UAP, "access point") are the next best thing."""
    wifi = sum(1 for v in iftypes.values() if re.search(r"(^|\D)71(\D|$)|ieee80211", str(v)))
    if wifi:
        return f"{wifi} Wi-Fi interface{'s' if wifi > 1 else ''}"
    named = sorted({str(v).strip('" ') for v in descrs.values() if WIFI_IF.match(str(v).strip('" '))})
    if named:
        return "Wi-Fi interfaces " + ", ".join(named[:3])
    m = ap_model(sysdescr)
    if m:
        return f"it describes itself as {m!r}"
    return "its own LLDP: WLAN access point" if lldp_says_ap(own_caps) else None
INVENTORY = {}      # hostid -> {"model", "serial", "firmware", "hardware", "vendor", "descr", "location", "contact", "object_id"}
ENTITY = "1.3.6.1.2.1.47.1.1.1.1"   # ENTITY-MIB entPhysicalTable
SYSTEM = {"descr": "1.3.6.1.2.1.1.1", "object_id": "1.3.6.1.2.1.1.2", "contact": "1.3.6.1.2.1.1.4", "location": "1.3.6.1.2.1.1.6"}


def inventory_from(entity, system):
    """Model, serial, firmware ... of a switch / router, from ENTITY-MIB rows ({"col.index": value}) and the system
    group. The chassis (class 3) is preferred, else the first entry that has a serial number or a model."""
    clean = lambda v: str(v or "").strip().strip('"').strip()  # noqa: E731
    rows = {}
    for k, v in entity.items():
        col, _, idx = str(k).partition(".")
        rows.setdefault(idx, {})[col] = clean(v)
    pick = next((r for r in rows.values() if r.get("5") == "3" and (r.get("11") or r.get("13"))), None) \
        or next((r for r in rows.values() if r.get("11") or r.get("13")), {})
    sysv = {k: clean(next(iter(v.values()), "")) if isinstance(v, dict) else clean(v) for k, v in system.items()}
    sw_rev = pick.get("10") or pick.get("9") or ""
    return {"model": pick.get("13") or pick.get("2") or "", "serial": pick.get("11", ""), "firmware": sw_rev,
            "hardware": pick.get("8", ""), "vendor": pick.get("12", ""), "descr": sysv.get("descr", "")[:200],
            "location": sysv.get("location", "")[:120], "contact": sysv.get("contact", "")[:120], "object_id": sysv.get("object_id", "")}
FACTS = {           # optional standard tables: duplex, STP, LACP partner, PoE, IPv6 neighbours
    "duplex": "1.3.6.1.2.1.10.7.2.1.19", "stp_base": "1.3.6.1.2.1.17.1.1", "stp_root": "1.3.6.1.2.1.17.2.5",
    "stp_rootport": "1.3.6.1.2.1.17.2.7", "stp_state": "1.3.6.1.2.1.17.2.15.1.3", "stp_dbridge": "1.3.6.1.2.1.17.2.15.1.8",
    "stp_dport": "1.3.6.1.2.1.17.2.15.1.9", "lacp_psys": "1.2.840.10006.300.43.1.2.1.1.9",
    "lacp_pport": "1.2.840.10006.300.43.1.2.1.1.17", "poe_status": "1.3.6.1.2.1.105.1.1.1.6",
    "poe_class": "1.3.6.1.2.1.105.1.1.1.10", "poe_total": "1.3.6.1.2.1.105.1.3.1.1.4", "ip6": "1.3.6.1.2.1.4.35.1.4",
    "ifmac": "1.3.6.1.2.1.2.2.1.6",
}
STP_STATE = {"1": "disabled", "2": "blocking", "3": "listening", "4": "learning", "5": "forwarding", "6": "broken"}


def hexbytes(v):
    return [int(x, 16) for x in re.findall(r"[0-9A-Fa-f]{2}", str(v or ""))]


def mac_of(v):
    """A MAC from a hex value: 6 bytes, or the last 6 of a bridge ID (2 bytes priority + MAC)."""
    b = hexbytes(v)
    return ":".join(f"{x:02x}" for x in b[-6:]) if len(b) >= 6 else ""


def ip6_neighbours(rows):
    """ipNetToPhysicalPhysAddress rows (index ifIndex.2.16.<16 bytes>) -> {mac: [IPv6]}."""
    import ipaddress
    out = {}
    for suffix, val in rows.items():
        parts = str(suffix).split(".")
        if len(parts) >= 19 and parts[1] == "2" and parts[2] == "16":
            try:
                ip = str(ipaddress.IPv6Address(bytes(int(x) for x in parts[3:19])))
            except ValueError:
                continue
            m = mac_of(val)
            if m and not ip.startswith("ff"):
                out.setdefault(m, []).append(ip)
    return out
CDP = {"addr": "1.3.6.1.4.1.9.9.23.1.2.1.1.4", "id": "1.3.6.1.4.1.9.9.23.1.2.1.1.6", "port": "1.3.6.1.4.1.9.9.23.1.2.1.1.7"}


def cdp_records(names, dev_ids, dev_ports, addrs):
    """CDP neighbours (CISCO-CDP-MIB, index ifIndex.deviceIndex) in the same form as at.lldp_neighbors()."""
    out = []
    for suffix, dev in dev_ids.items():
        parts = str(suffix).split(".")
        if len(parts) < 2:
            continue
        ifx = parts[0]
        raw = str(dev).strip('"')
        sysname = re.sub(r"\(.*\)$", "", raw).strip()             # some devices add their serial number in brackets
        hexes = re.findall(r"[0-9A-Fa-f]{2}", str(addrs.get(suffix, "")))
        ip = ".".join(str(int(x, 16)) for x in hexes[:4]) if len(hexes) >= 4 else ""
        local = names.get(ifx, f"port {ifx}")
        out.append({"local_port": local, "local_names": [local], "sysname": sysname, "chassis": "",
                    "remote_port": str(dev_ports.get(suffix, "")).strip('"'), "mgmt_ip": ip, "via": "cdp"})
    return out
ENUM = {"up": "1", "down": "2", "testing": "3", "unknown": "4", "dormant": "5",
        "notPresent": "6", "lowerLayerDown": "7",
        "ethernetCsmacd": "6", "gigabitEthernet": "117", "ieee8023adLag": "161"}


def num(v):
    v = (v or "").strip()
    if v in ENUM:
        return ENUM[v]
    m = re.search(r"\d+", v)
    return m.group(0) if m else v


def norm(p):
    return re.sub(r"\s+", "", (p or "").lower())


# --------------------------------------------------------------------------- #
# Who is on the LAN: ping sweep + ARP + reverse DNS
# --------------------------------------------------------------------------- #
def sweep(nets):
    sweep_ips([str(ip) for n in nets for ip in ipaddress.ip_network(n, strict=False).hosts()])


def sweep_ips(ips, timeout_ms=1000, parallel=64, size=56, pps=0):
    """Ping every address once, so that live devices show up in the ARP and MAC tables.
    pps > 0: at most that many pings start per second (all threads together)."""
    import threading
    wait = f"{max(timeout_ms, 100) / 1000:g}"
    gap, lock, nxt = (1.0 / pps if pps else 0), threading.Lock(), [time.monotonic()]

    def pace():
        if not gap:
            return
        with lock:
            t = max(nxt[0], time.monotonic())
            nxt[0] = t + gap
        d = t - time.monotonic()
        if d > 0:
            time.sleep(d)

    def ping(ip):
        pace()
        try:
            r = subprocess.run(["ping", "-n", "-c", "1", "-W", wait, "-s", str(int(size)), ip],
                               capture_output=True, text=True, timeout=timeout_ms / 1000 + 3)
            m = re.search(r"ttl=(\d+)", r.stdout)
            if m:
                TTL[ip] = int(m.group(1))                 # hop count and an OS hint (64 / 128 / 255)
        except Exception:  # noqa: BLE001
            pass
    with cf.ThreadPoolExecutor(max(1, min(int(parallel), 256))) as ex:
        list(ex.map(ping, ips))


def local_arp():
    try:
        out = subprocess.run(["ip", "neigh"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return {}
    res = {}
    for line in out.splitlines():
        p = line.split()
        if "lladdr" in p and p[0].count(".") == 3:
            res[p[p.index("lladdr") + 1].lower()] = p[0]
    return res


def rdns_all(ips):
    socket.setdefaulttimeout(1)
    def look(ip):
        try:
            return ip, socket.gethostbyaddr(ip)[0]
        except Exception:  # noqa: BLE001
            return ip, ""
    with cf.ThreadPoolExecutor(32) as ex:
        return dict(ex.map(look, ips))


# --------------------------------------------------------------------------- #
# Switch scan
# --------------------------------------------------------------------------- #
def portlist(value):
    """Bridge port numbers in a Q-BRIDGE PortList (bit 1 = the first bit of the first byte)."""
    out = []
    for i, byte in enumerate(re.findall(r"[0-9A-Fa-f]{2}", str(value or ""))):
        b = int(byte, 16)
        out += [i * 8 + k + 1 for k in range(8) if b & (0x80 >> k)]
    return out


def vlan_tables(names, egress, untagged, pvids, fdb_ids):
    """From the Q-BRIDGE walks: ({vlan: name}, {bridge port: {"pvid", "tagged", "untagged"}}, {fdb id: vlan})."""
    vnames = {}
    for vid, nm in names.items():
        if str(vid).isdigit():
            vnames[int(vid)] = str(nm).strip('"') or f"VLAN {vid}"
    ports = {}
    for vid, plist in egress.items():
        if not str(vid).isdigit():
            continue
        unt = set(portlist(untagged.get(vid, "")))
        for bp in portlist(plist):
            e = ports.setdefault(str(bp), {"pvid": None, "tagged": [], "untagged": []})
            e["untagged" if bp in unt else "tagged"].append(int(vid))
            vnames.setdefault(int(vid), f"VLAN {vid}")
    for bp, vid in pvids.items():
        if str(vid).strip().isdigit() and int(vid):
            ports.setdefault(str(bp), {"pvid": None, "tagged": [], "untagged": []})["pvid"] = int(vid)
            vnames.setdefault(int(vid), f"VLAN {int(vid)}")
    fdb_vlan = {}
    for suffix, fid in fdb_ids.items():                  # index: time mark . vlan id  ->  fdb id
        parts = str(suffix).split(".")
        if len(parts) >= 2 and str(fid).strip().isdigit():
            fdb_vlan[str(fid).strip()] = int(parts[-1])
    return vnames, ports, fdb_vlan


def iface_kind(name, iftype):
    """What one interface is: ("port" | "lag" | None, reason when it is not a switch port)."""
    t = num(iftype) if iftype not in (None, "") else ""
    if LAG_RX.match(name or "") or t == "161":
        return "lag", ""
    if VIRTUAL_RX.match(name or ""):
        return None, "virtual interface"
    if t and t not in ("6", "117"):
        return None, f"not an Ethernet port (ifType {t})"
    return "port", ""


def media_from_text(text):
    """Pluggable port type when a name or description says it explicitly; "" otherwise (no guessing)."""
    t = (text or "").lower()
    for rx, media in ((r"qsfp-?dd", "QSFP-DD"), (r"qsfp28", "QSFP28"), (r"qsfp", "QSFP+"), (r"sfp28", "SFP28"),
                      (r"sfp\+|sfpplus|sfp-plus|sfp plus", "SFP+"), (r"\bxfp", "XFP"), (r"sfp", "SFP")):
        if re.search(rx, t):
            return media
    return ""


ENT_DESCR, ENT_NAME = "1.3.6.1.2.1.47.1.1.1.1.2", "1.3.6.1.2.1.47.1.1.1.1.7"
ENT_ALIAS = "1.3.6.1.2.1.47.1.3.2.1.2"     # entAliasMappingIdentifier: hardware entity -> ifIndex


def entity_media(auth, target):
    """ifIndex -> SFP / SFP+ / ... from the device's hardware inventory (ENTITY-MIB), when the device describes
    its ports there. Devices without ENTITY-MIB simply return nothing."""
    try:
        alias = at.snmpwalk(auth, target, ENT_ALIAS)
        if not alias:
            return {}
        descr, names = at.snmpwalk(auth, target, ENT_DESCR), at.snmpwalk(auth, target, ENT_NAME)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for suffix, val in alias.items():
        phys = suffix.split(".")[0]
        m = re.search(r"1\.3\.6\.1\.2\.1\.2\.2\.1\.1\.(\d+)$", val or "")
        media = media_from_text(f"{descr.get(phys, '')} {names.get(phys, '')}")
        if m and media:
            out[m.group(1)] = media
    return out


def scan_switch(h, community, hosts):
    auth, target = at.snmp_auth(h["_snmp"], h["_macros"], community)
    auth = auth + ["-Oe"]                       # enums as numbers
    w = lambda k: at.snmpwalk(auth, target, OID[k])  # noqa: E731

    names, alias, types = w("name"), w("alias"), w("type")
    oper, admin, speed = w("oper"), w("admin"), w("speed")
    bport = {k: num(v) for k, v in w("bport").items()}

    macs, mac_vlan = {}, {}
    qfdb = w("qfdb")
    fdb = qfdb or w("dfdb")
    try:
        wx = lambda k: at.snmpwalk(auth + ["-Ox"], target, OID[k])  # noqa: E731 - port bitmaps always as hex
        vnames, vports, fdb_vlan = vlan_tables(w("vname"), wx("vegress"), wx("vuntag"), w("pvid"), w("vfdb"))
    except Exception:  # noqa: BLE001 - VLANs are optional
        vnames, vports, fdb_vlan = {}, {}, {}
    for suffix, port in fdb.items():
        parts = suffix.split(".")
        port = num(port)
        if len(parts) < 6 or port in ("", "0"):
            continue
        mac = ":".join(f"{int(x):02x}" for x in parts[-6:])
        ifx = bport.get(port, port)
        macs.setdefault(ifx, set()).add(mac)
        if qfdb and len(parts) == 7:                     # dot1qTpFdbPort: fdb id . MAC
            vid = fdb_vlan.get(parts[0]) or (int(parts[0]) if (int(parts[0]) in vnames or not fdb_vlan) else None)
            if vid:
                mac_vlan.setdefault(ifx, {})[mac] = vid
    VLANS[h["hostid"]] = {k: v for k, v in vnames.items() if 0 < k < 4095}
    # ---- facts from optional standard tables (each one best effort)
    fx = {}
    for k in ("duplex", "stp_state", "poe_status", "poe_class", "poe_total"):
        try:
            fx[k] = at.snmpwalk(auth, target, FACTS[k])
        except Exception:  # noqa: BLE001
            fx[k] = {}
    for k in ("stp_base", "stp_root", "stp_rootport", "stp_dbridge", "stp_dport", "lacp_psys", "lacp_pport", "ip6", "ifmac"):
        try:
            fx[k] = at.snmpwalk(auth + ["-Ox"], target, FACTS[k])
        except Exception:  # noqa: BLE001
            fx[k] = {}
    own = mac_of(next(iter(fx["stp_base"].values()), ""))
    rp = hexbytes(next(iter(fx["stp_rootport"].values()), ""))
    BRIDGE[h["hostid"]] = {"mac": own, "root": mac_of(next(iter(fx["stp_root"].values()), "")),
                           "rootport": str(int.from_bytes(bytes(rp), "big")) if rp else "", "bport": dict(bport)}
    stp = {}
    for bp, st in fx["stp_state"].items():
        dp = hexbytes(fx["stp_dport"].get(bp, ""))
        stp[bport.get(bp, bp)] = {"state": STP_STATE.get(str(st).strip(), str(st)), "dbridge": mac_of(fx["stp_dbridge"].get(bp, "")),
                                  "dport": str((int.from_bytes(bytes(dp), "big") & 0x0FFF)) if dp else ""}
    V6[h["hostid"]] = ip6_neighbours(fx["ip6"])
    IFMACS[h["hostid"]] = {m for m in (mac_of(v) for v in fx["ifmac"].values())   # not zero, broadcast or multicast
                           if m and m not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") and not int(m[:2], 16) & 1}
    try:                                                      # inventory: model, serial, firmware (ENTITY-MIB)
        ent = {k: v for col in ("2", "5", "8", "9", "10", "11", "12", "13")
               for k, v in ((f"{col}.{i}", x) for i, x in at.snmpwalk(auth, target, f"{ENTITY}.{col}").items())}
        INVENTORY[h["hostid"]] = inventory_from(ent, {k: at.snmpwalk(auth, target, o) for k, o in SYSTEM.items()})
    except Exception:  # noqa: BLE001
        INVENTORY[h["hostid"]] = {}
    try:                                                      # is it an access point? (read as text, not hex)
        inv = INVENTORY.get(h["hostid"]) or {}
        why = ap_evidence(at.snmpwalk(auth, target, "1.3.6.1.2.1.2.2.1.3"), at.snmpwalk(auth, target, "1.3.6.1.2.1.2.2.1.2"),
                          f"{inv.get('descr', '')} {inv.get('model', '')} {h.get('name', '')}",
                          next(iter(at.snmpwalk(auth, target, "1.0.8802.1.1.2.1.3.6").values()), ""))   # lldpLocSysCapEnabled
    except Exception:  # noqa: BLE001
        why = None
    if why:
        ROLE[h["hostid"]] = ("ap", why)
    else:
        ROLE.pop(h["hostid"], None)
    POE_TOTAL[h["hostid"]] = sum(int(v) for v in fx["poe_total"].values() if str(v).strip().isdigit())
    poe = {}
    for suffix, st in fx["poe_status"].items():               # index: group . port (often the ifIndex)
        port = str(suffix).split(".")[-1]
        cls = fx["poe_class"].get(suffix, "")
        poe[port] = {"on": str(st).strip() == "3", "fault": str(st).strip() in ("4", "6"),
                     "class": int(cls) - 1 if str(cls).strip().isdigit() and int(cls) > 0 else None}
    by_ifx = {bport.get(bp, bp): v for bp, v in vports.items()}     # bridge port -> ifIndex
    try:
        lag_of = {k: num(v) for k, v in w("lagof").items() if num(v) not in ("", "0") and num(v) != k}
    except Exception:  # noqa: BLE001
        lag_of = {}

    arp = {}
    for suffix, mac in w("arp").items():
        if re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
            arp[mac] = ".".join(suffix.split(".")[-4:])

    hw_media = entity_media(auth, target)

    lldp = {}
    neighbours = at.lldp_neighbors(auth, target)
    if CDP_ON:                                           # Cisco: CDP neighbours too, where LLDP has none
        try:
            cdp = cdp_records(names, at.snmpwalk(auth, target, CDP["id"]), at.snmpwalk(auth, target, CDP["port"]),
                              at.snmpwalk(auth + ["-Ox"], target, CDP["addr"]))
        except Exception:  # noqa: BLE001
            cdp = []
        have = {norm(n["local_port"]) for n in neighbours}
        neighbours += [c for c in cdp if norm(c["local_port"]) not in have]
    for nb in neighbours:
        peer = at.match_host(nb, hosts)
        for n in {norm(x) for x in (nb["local_names"] or [nb["local_port"]])}:
            lldp.setdefault(n, []).append((nb, peer))

    ports = []
    for idx in sorted(names, key=lambda x: int(x) if x.isdigit() else 0):
        name = names[idx]
        kind, _why = iface_kind(name, types.get(idx, ""))
        if not kind:
            continue
        a, o = num(admin.get(idx)), num(oper.get(idx))
        status = "disabled" if a == "2" else ("up" if o == "1" else "down")
        sp = int(num(speed.get(idx)) or 0)
        ports.append({
            "ifindex": idx, "name": name, "alias": alias.get(idx, ""), "status": status, "kind": kind,
            "speed_mbps": sp if status == "up" else 0, "media_hw": hw_media.get(idx, ""),
            "speed": (f"{sp / 1000:g} Gbps" if sp >= 1000 else f"{sp} Mbps") if status == "up" and sp else "",
            "lldp": lldp.get(norm(name), []), "macs": sorted(macs.get(idx, set())),
            "vlans": by_ifx.get(idx), "mac_vlans": mac_vlan.get(idx, {}), "lag": lag_of.get(idx),
            "duplex": {"2": "half", "3": "full"}.get(str(fx["duplex"].get(idx, "")).strip()), "stp": stp.get(idx),
            "lacp_partner": (mac_of(fx["lacp_psys"].get(idx, "")) or None) if mac_of(fx["lacp_psys"].get(idx, "")) not in ("", "00:00:00:00:00:00") else None,
            "poe": poe.get(idx),
        })
    return ports, arp


def local_nets():
    """The IPv4 networks this server is on (its own addresses), at most /22 each - the network SpiderMap maps when no
    allowed networks are set. Never the networks of every SNMP host in Zabbix (those can be other sites)."""
    out = set()
    try:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=5)
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", r.stdout):
            ip, pfx = m.group(1), max(int(m.group(2)), 22)
            out.add(str(ipaddress.ip_network(f"{ip}/{pfx}", strict=False)))
    except (OSError, subprocess.SubprocessError):
        pass
    if not out:                                   # no 'ip' command (or not allowed): the kernel's routing table
        out = set(_connected_routes())
    return sorted(out)


def _connected_routes(path="/proc/net/route"):
    """Networks directly connected to this server (no gateway), from the kernel's routing table, at most /22."""
    nets = set()
    try:
        with open(path, encoding="ascii") as f:
            next(f, None)
            for line in f:
                c = line.split()
                if len(c) < 8 or c[2] != "00000000" or c[1] == "00000000":
                    continue                      # via a gateway, or the default route: not ours
                dest = ipaddress.IPv4Address(int(c[1], 16).to_bytes(4, "little"))
                pfx = bin(int(c[7], 16)).count("1")
                if dest.is_loopback or dest.is_link_local:
                    continue
                nets.add(str(ipaddress.ip_network(f"{dest}/{max(pfx, 22)}", strict=False)))
    except (OSError, ValueError):
        pass
    return sorted(nets)


def default_gateway():
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return ""
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def describe(port, ipmap, names, gw_mac=None):
    """Return list of 'connected to' lines for a port."""
    def dev(mac):
        ip = ipmap.get(mac, "")
        nm = names.get(ip, "")
        txt = " ".join(x for x in (nm, f"({ip})" if ip and nm else ip, mac) if x)
        return ("Gateway: " + txt) if mac == gw_mac else txt

    out, via_switch = [], False
    for nb, peer in port["lldp"]:
        who = peer["name"] if peer else (nb["sysname"] or nb["chassis"])
        out.append(f"{who} [{nb['remote_port']}] (LLDP)")
        via_switch |= bool(peer and peer["_role"] == "switch")
    macs, n = port["macs"], len(port["macs"])
    if n and via_switch:
        out.append(f"{n} devices behind this uplink")
    elif gw_mac and gw_mac in macs and n > 1:
        out.append(dev(gw_mac))
        out.append(f"+{n - 1} devices behind it (its Wi-Fi / LAN ports)")
    elif n > MAX_LIST:
        known = [names.get(ipmap.get(m, ""), "") or ipmap.get(m, "") for m in macs]
        known = [k for k in known if k][:3]
        out.append(f"{n} devices" + (": " + ", ".join(known) + ", \u2026" if known else ""))
    else:
        out += [dev(m) for m in macs]
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#fff;--fg:#1f2c33;--mut:#768d99;--line:#ebeef0;--up:#34af67;--down:#e45959;--off:#97aab3;--head:#f4f6f7}
@media (prefers-color-scheme:dark){:root{--bg:#2b2b2b;--fg:#f2f2f2;--mut:#a0a0a0;--line:#383838;--head:#333}}
body{margin:0;padding:12px;background:var(--bg);color:var(--fg);font:13px/1.45 Arial,Helvetica,sans-serif}
h2{font-size:15px;margin:18px 0 6px} .mut{color:var(--mut)}
.wrap{overflow-x:auto} table{border-collapse:collapse;width:100%;min-width:640px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--head);font-weight:600} td.c div{white-space:nowrap}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}
.up{background:var(--up)} .down{background:var(--down)} .disabled{background:var(--off)}
.sum span{margin-right:14px}
"""


def write_html(data, path):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>Switch ports</title>"
             f"<style>{CSS}</style></head><body><div class='mut'>Scanned {ts}</div>"]
    for sw, ports in data:
        cnt = {s: sum(p["status"] == s for p in ports) for s in ("up", "down", "disabled")}
        parts.append(f"<h2>{html.escape(sw)}</h2><div class='sum mut'>"
                     f"<span><i class='dot up'></i>{cnt['up']} up</span>"
                     f"<span><i class='dot down'></i>{cnt['down']} down</span>"
                     f"<span><i class='dot disabled'></i>{cnt['disabled']} disabled</span></div>"
                     "<div class='wrap'><table><tr><th>Port</th><th>Status</th><th>Speed</th>"
                     "<th>Description</th><th>Connected to</th></tr>")
        for p in ports:
            conn = "".join(f"<div>{html.escape(x)}</div>" for x in p["connected"]) or "<span class='mut'>—</span>"
            parts.append(f"<tr><td>{html.escape(p['name'])}</td>"
                         f"<td><i class='dot {p['status']}'></i>{p['status']}</td>"
                         f"<td>{html.escape(p['speed'])}</td><td>{html.escape(p['alias'])}</td>"
                         f"<td class='c'>{conn}</td></tr>")
        parts.append("</table></div>")
    parts.append("</body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    os.chmod(path, 0o644)


def print_table(data):
    for sw, ports in data:
        print(f"\n=== {sw} ===")
        print(f"  {'PORT':<24}{'STATUS':<10}{'SPEED':<10}{'DESCRIPTION':<20}CONNECTED TO")
        for p in ports:
            first, *rest = p["connected"] or ["-"]
            print(f"  {p['name']:<24}{p['status']:<10}{p['speed']:<10}{p['alias'][:19]:<20}{first}")
            for r in rest:
                print(f"  {'':<64}{r}")


def build_dashboard(api, switches, rel_url, abs_url):
    d = at.Dash(api, DASHBOARD_NAME, ["Ports"])
    d.add("Ports", "url", "Where every port goes", 0, 0, 72, 12,
          [{"url": (1, rel_url)}, {"url": (1, abs_url)}])
    for i, sw in enumerate(switches):
        base = {"items.0": (1, "*Operational status")}
        thr = {"thresholds.0.color": (1, "34AF67"), "thresholds.0.threshold": (1, "1"),
               "thresholds.1.color": (1, "E45959"), "thresholds.1.threshold": (1, "2")}
        variants = []
        for hk in ("hostids.0", "hostids"):
            variants += [{hk: (3, sw["hostid"]), **base, **thr}, {hk: (3, sw["hostid"]), **base}]
        d.add("Ports", "honeycomb", f"{sw['name']} – live port status",
              (i % 2) * 36, 12 + (i // 2) * 6, 36, 6, variants)
    return d.id


# --------------------------------------------------------------------------- #
def collect(api, hosts, community=None, do_sweep=True):
    """Return [(switch_host, ports)] with p['connected'] filled in."""
    switches = [h for h in hosts if h["_snmp"] and str(h["status"]) == "0"]
    nets = SCAN_SUBNETS or sorted({str(ipaddress.ip_network(f"{h['_snmp']['ip']}/24", strict=False))
                                   for h in switches if h["_snmp"].get("ip")})
    if do_sweep:
        print(f"  ping sweep {', '.join(nets)} (fills the ARP table)...")
        sweep(nets)
    data, ipmap = [], local_arp()
    for sw in switches:
        try:
            ports, arp = scan_switch(sw, community, hosts)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {sw['name']}: {e}")
            continue
        print(f"  {sw['name']}: {len(ports)} ports, {sum(p['status'] == 'up' for p in ports)} up")
        ipmap.update({k: v for k, v in arp.items() if k not in ipmap})
        data.append((sw, ports))
    names = rdns_all(sorted(set(ipmap.values())))
    for h in hosts:                                    # Zabbix names win over DNS
        for ip in h["_ips"]:
            names[ip] = h["name"]
    gw_ip = default_gateway()
    gw_mac = next((m for m, ip in ipmap.items() if ip == gw_ip), None) if gw_ip else None
    if gw_ip:
        print(f"  gateway {gw_ip} ({gw_mac or 'MAC unknown'})")
    for _, ports in data:
        for p in ports:
            p["connected"] = describe(p, ipmap, names, gw_mac)
    return data


def main():
    ap = argparse.ArgumentParser(description="Switch port inventory for Zabbix")
    ap.add_argument("--community", help="SNMP v1/v2c community override")
    ap.add_argument("--no-sweep", action="store_true", help="skip the ping sweep")
    ap.add_argument("--no-dashboard", action="store_true")
    args = ap.parse_args()

    if not at.ZABBIX_TOKEN:
        raise SystemExit("Run 'source ./zabbix.env' first.")
    api = at.Zabbix(at.ZABBIX_URL, at.ZABBIX_TOKEN, at.VERIFY_TLS)
    hosts = at.load_hosts(api)
    print("Scanning switch ports...")
    hdata = collect(api, hosts, args.community, do_sweep=not args.no_sweep)
    data = [(sw["name"], ports) for sw, ports in hdata]
    switches = [sw for sw, _ in hdata]

    print_table(data)

    webdir = next((d for d in WEB_DIRS if os.path.isfile(os.path.join(d, "api_jsonrpc.php"))), None)
    path = os.path.join(webdir or os.getcwd(), HTML_NAME)
    write_html(data, path)
    print(f"\nHTML page written: {path}")
    if not webdir:
        print("  (Zabbix web folder not found, so it is not visible in the browser)")

    if not args.no_dashboard and webdir:
        u = urlparse(at.ZABBIX_URL)
        host = u.hostname
        if host in ("127.0.0.1", "localhost"):
            zs = next((h for h in hosts if h["_role"] == "zabbix" and h["_ips"]), None)
            host = sorted(zs["_ips"])[0] if zs else host
        abs_url = f"{u.scheme}://{host}{u.path.rsplit('/', 1)[0]}/{HTML_NAME}"
        print("Building dashboard...")
        did = build_dashboard(api, switches, HTML_NAME, abs_url)
        print(f"Dashboard created: '{DASHBOARD_NAME}' (id {did})  page: {abs_url}")


if __name__ == "__main__":
    main()
