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

OID = {
    "name": "1.3.6.1.2.1.31.1.1.1.1", "alias": "1.3.6.1.2.1.31.1.1.1.18",
    "type": "1.3.6.1.2.1.2.2.1.3", "oper": "1.3.6.1.2.1.2.2.1.8",
    "admin": "1.3.6.1.2.1.2.2.1.7", "speed": "1.3.6.1.2.1.31.1.1.1.15",
    "bport": "1.3.6.1.2.1.17.1.4.1.2", "qfdb": "1.3.6.1.2.1.17.7.1.2.2.1.2",
    "dfdb": "1.3.6.1.2.1.17.4.3.1.2", "arp": "1.3.6.1.2.1.4.22.1.2",
}
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
    ips = [str(ip) for n in nets for ip in ipaddress.ip_network(n, strict=False).hosts()]
    def ping(ip):
        try:
            subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except Exception:  # noqa: BLE001
            pass
    with cf.ThreadPoolExecutor(64) as ex:
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
def scan_switch(h, community, hosts):
    auth, target = at.snmp_auth(h["_snmp"], h["_macros"], community)
    auth = auth + ["-Oe"]                       # enums as numbers
    w = lambda k: at.snmpwalk(auth, target, OID[k])  # noqa: E731

    names, alias, types = w("name"), w("alias"), w("type")
    oper, admin, speed = w("oper"), w("admin"), w("speed")
    bport = {k: num(v) for k, v in w("bport").items()}

    macs = {}
    fdb = w("qfdb") or w("dfdb")
    for suffix, port in fdb.items():
        parts = suffix.split(".")
        port = num(port)
        if len(parts) < 6 or port in ("", "0"):
            continue
        mac = ":".join(f"{int(x):02x}" for x in parts[-6:])
        macs.setdefault(bport.get(port, port), set()).add(mac)

    arp = {}
    for suffix, mac in w("arp").items():
        if re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
            arp[mac] = ".".join(suffix.split(".")[-4:])

    lldp = {}
    for nb in at.lldp_neighbors(auth, target):
        peer = at.match_host(nb, hosts)
        for n in {norm(x) for x in (nb["local_names"] or [nb["local_port"]])}:
            lldp.setdefault(n, []).append((nb, peer))

    ports = []
    for idx in sorted(names, key=lambda x: int(x) if x.isdigit() else 0):
        if types and num(types.get(idx)) not in PHYS_TYPES:
            continue
        name = names[idx]
        if not types and re.match(r"(?i)(null|vlanif|inloop|meth|loop)", name):
            continue
        a, o = num(admin.get(idx)), num(oper.get(idx))
        status = "disabled" if a == "2" else ("up" if o == "1" else "down")
        sp = int(num(speed.get(idx)) or 0)
        ports.append({
            "ifindex": idx, "name": name, "alias": alias.get(idx, ""), "status": status,
            "speed": (f"{sp / 1000:g} Gbps" if sp >= 1000 else f"{sp} Mbps") if status == "up" and sp else "",
            "lldp": lldp.get(norm(name), []), "macs": sorted(macs.get(idx, set())),
        })
    return ports, arp


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
