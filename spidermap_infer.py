"""Topology inference: device types, link-state and traffic correlation, suggested links.

Part of SpiderMap (see spidermap.py and docs/ARCHITECTURE.md)."""
import os
import re

from spidermap_ports import ap_model  # noqa: E402
from spidermap import STATE_DIR, is_private_mac  # noqa: E402 - spidermap.py loads this module at its end


PLACE_KINDS = {"switch": ("Unmanaged switch", "server-2"), "ap": ("Access point", "access-point"),
               "hypervisor": ("Hypervisor", "server-cog"), "bridge": ("Wi-Fi bridge", "antenna"), "hub": ("Hub", "sitemap"),
               "other": ("Other device", "box")}


VIA_TEXT = {"stp": "spanning tree (the switch names its neighbour)", "lacp": "LACP (the link aggregation partner)",
            "unifi": "UniFi controller (a wireless client of this access point)","lldp": "LLDP (the device announced itself)", "mac": "MAC table of the switch", "manual": "pinned in devices.json",
            "proxmox": "Proxmox API", "arp": "guessed: seen only through the gateway (Wi-Fi or its LAN ports)",
            "behind": "guessed: behind another device", "remembered": "guessed: where it was last seen"}


VIA_GUESSED = ("arp", "behind", "remembered")


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


def classify(name, vendor, mac, off=()):
    """Device type from its name / DNS name and manufacturer. Categories switched off in Settings > Device types
    are skipped: their devices get the next matching type, or "unknown"."""
    text = f"{name} {vendor}".lower()
    if mac.startswith("bc:24:11"):
        return "guest"
    if ap_model(name, vendor):                           # an access point by its model name, from any vendor
        return "ap"
    for key, typ, _t, _e, _d, rx in DEVICE_CATEGORIES:
        if key not in off and re.search(rx, text):
            return typ
    for rx, t in CLASSIFY:
        if t not in ("camera", "printer", "nas") and re.search(rx, text):     # those three are categories above
            return t
    return "phone" if is_private_mac(mac) else "unknown"


HINTS_FILE = os.path.join(STATE_DIR, "spidermap_link_hints.json")


def link_state_pairs(changes, window=8, mass=6):
    """Pairs of ports on different switches that went down or up together (same direction, within `window` s).
    Moments when more than `mass` ports change at once (a power cut, a restart) prove nothing and are skipped."""
    pairs = set()
    for t, s1, p1, st1 in changes:
        near = [c for c in changes if abs(c[0] - t) <= window]
        if len(near) > mass:
            continue
        for _t2, s2, p2, st2 in near:
            if s2 != s1 and st2 == st1:
                pairs.add(tuple(sorted([(s1, p1), (s2, p2)])))
    return pairs


def traffic_match(a, b, tol=0.05, min_bps=50e3, min_corr=0.9):
    """Do ports a and b carry the same traffic, as the two ends of one cable do? a, b: [(t, rx, tx)] rates (bit/s)
    from the same polling cycles. What leaves a must arrive at b and the other way round (totals within `tol`), and
    the traffic must rise and fall together (steady traffic proves nothing). Returns (True / False / None = no signal)."""
    pairs = [(x, y) for x in a for y in b if abs(x[0] - y[0]) <= 3]
    if len(pairs) < 6:
        return None
    out_a, in_b = sum(x[2] for x, _ in pairs), sum(y[1] for _, y in pairs)
    in_a, out_b = sum(x[1] for x, _ in pairs), sum(y[2] for _, y in pairs)
    vol = (out_a + in_a) / len(pairs)
    if vol < min_bps:
        return None
    def close(u, v):
        return abs(u - v) <= tol * max(u, v, 1)
    if not (close(out_a, in_b) and close(in_a, out_b)):
        return False
    xs = [x[1] + x[2] for x, _ in pairs]
    ys = [y[1] + y[2] for _, y in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx, vy = sum((v - mx) ** 2 for v in xs), sum((v - my) ** 2 for v in ys)
    if vx <= 0 or vy <= 0 or (vx / len(xs)) ** 0.5 < 0.05 * mx:
        return None                                    # steady traffic: the totals alone could be a coincidence
    corr = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (vx * vy) ** 0.5
    return corr >= min_corr


def update_traffic_hints(hints, series, meta, linked, t, need=3):
    """Every window: candidate pairs of ports on different switches (same speed, both up, not already explained)
    that carry the same traffic get one more match; a clear mismatch starts them again. series: {(sid, pid):
    [(t, rx, tx)]}, meta: {(sid, pid): {"speed": Mbps, "up": bool, "known": bool}}, linked: {frozenset(sid pair)}."""
    keys = [k for k, m in meta.items() if m.get("up") and not m.get("known") and len(series.get(k, [])) >= 6]
    changed = False
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if a[0] == b[0] or meta[a].get("speed") != meta[b].get("speed") or frozenset((a[0], b[0])) in linked:
                continue
            r = traffic_match(series[a], series[b])
            if r is None:
                continue
            x, y = sorted([a, b])
            k = f"T|{x[0]}|{x[1]}|{y[0]}|{y[1]}"
            h = hints.get(k, {"n": 0, "last": 0})
            if r and t - h["last"] >= 60:
                h = {"n": h["n"] + 1, "last": t}
            elif r is False and h["n"]:
                h = {"n": 0, "last": t}
            else:
                continue
            hints[k], changed = h, True
    return changed


def update_hints(hints, changes, t):
    """Counts each pair once per event (not again within 60 s). Returns True when something changed."""
    changed = False
    for (a, b) in link_state_pairs([c for c in changes if t - c[0] <= 60]):
        k = f"{a[0]}|{a[1]}|{b[0]}|{b[1]}"
        h = hints.setdefault(k, {"n": 0, "last": 0})
        if t - h["last"] > 60:
            h["n"], h["last"], changed = h["n"] + 1, t, True
    return changed


def link_suggestions(hints, data, links, min_n=2):
    """Pairs seen together at least `min_n` times, by name, without switches already linked and rejected pairs."""
    sws = data.get("switches") or {}
    linked = {frozenset((n["sw"], (n.get("port_ref") or [None])[0])) for n in data.get("nodes", [])
              if n.get("kind") == "switch" and n.get("port_ref")}
    refused = {frozenset([tuple(L["a"]), tuple(L["b"])]) for L in links if L.get("state") == "rejected"}

    def end(sid, pid):
        sw_ = sws.get(sid) or {}
        p = next((q for q in sw_.get("ports", []) if str(q.get("id", q.get("n"))) == pid), None)
        return [sw_.get("name", sid), p.get("base", p.get("label")) if p else pid]
    out = []
    for k, h in sorted(hints.items(), key=lambda x: -x[1]["n"]):
        traffic = k.startswith("T|")
        if h["n"] < (3 if traffic else min_n):
            continue
        s1, p1, s2, p2 = k.split("|")[1:] if traffic else k.split("|")
        a, b = end(s1, p1), end(s2, p2)
        if frozenset((s1, s2)) in linked or frozenset([tuple(a), tuple(b)]) in refused:
            continue
        why = (f"carried the same traffic, byte for byte (±5%), in {h['n']} measurements" if traffic
               else f"went down and up together {h['n']} times")
        if any(o["a"] == a and o["b"] == b for o in out):
            next(o for o in out if o["a"] == a and o["b"] == b)["why"] += "; " + why
            continue
        out.append({"a": a, "b": b, "n": h["n"], "why": why})
    return out[:20]


def mac_switch_links(ports, own, gw_mac, links, root):
    """Links between switches from the MAC tables, branch by branch, for whatever no better evidence (LLDP, STP,
    LACP, a confirmation) has connected to the core yet.
    ports: {switch: [port records with "name", "macs"]}; own: {switch: its own MACs (management, bridge, interfaces)};
    gw_mac: the gateway's MAC; links: existing links {(a, b)} - any direction; root: the core switch (by the gateway).
    Each round links the closest switch not yet reachable from the core - switch B is behind the port of a reachable
    switch A that sees B's MAC, is not a port toward the gateway (that one leads up) and has the fewest MACs - which
    also brings in whatever B is already linked to (a branch connected by LLDP, say). Returns
    [(A, A's port, B, B's port toward A or "")]. Without the gateway's MAC there is no "up", so there are no links."""
    if not gw_mac or root not in ports:
        return []
    adj = {}
    for a, b in links:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    def reachable():
        seen, todo = {root}, [root]
        while todo:
            for n in adj.get(todo.pop(), ()):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        return seen
    out = []
    while True:
        here, best, tie = reachable(), None, False
        for b in ports:
            mb = own.get(b) or set()
            if b in here or not mb:
                continue
            for a in here:
                for p in ports.get(a, ()):
                    macs = set(p.get("macs") or ())
                    if gw_mac in macs or not mb & macs:
                        continue                         # the port toward the gateway leads up, not down
                    if best is None or len(macs) < best[0]:
                        best, tie = (len(macs), a, p["name"], b), False
                    elif len(macs) == best[0] and b == best[3] and a != best[1]:
                        tie = True                       # two switches equally close: do not guess
        if not best or tie:
            return out
        _n, a, pa, b = best
        pb = next((p["name"] for p in ports[b] if gw_mac in (p.get("macs") or ()) or (own.get(a) or set()) & set(p.get("macs") or ())), "")
        out.append((a, pa, b, pb))
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
