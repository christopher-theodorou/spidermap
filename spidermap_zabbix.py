#!/usr/bin/env python3
"""
spidermap_zabbix.py
------------------
1. Reads LLDP neighbour tables from every SNMP host in Zabbix (your Huawei switches)
2. Matches neighbours to Zabbix hosts (by management IP, LLDP system name or ALIASES)
3. Builds a Zabbix map with port labels, live traffic on each link and
   link colours driven by the interface triggers (link down, high bandwidth, errors)
4. Builds a multi-page dashboard (Overview / Switches / Proxmox)

Re-run it any time you change cabling: the map and dashboard are recreated.

Run it ON the Zabbix server (it needs to reach the switches over SNMP):
    apt install snmp            # provides snmpwalk
    pip install requests        # (or: apt install python3-requests)
    export ZABBIX_URL="http://127.0.0.1/zabbix/api_jsonrpc.php"
    export ZABBIX_TOKEN="your-api-token"
    python3 spidermap_zabbix.py --dry-run     # show what it found, change nothing
    python3 spidermap_zabbix.py               # build map + dashboard
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import requests

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Where things live. Since 0.5.0 the installer moves configuration to /etc/spidermap and runtime data to
# /var/lib/spidermap/state (owned by the "spidermap" user); until then everything stays next to the code.
# SPIDERMAP_CONF / SPIDERMAP_STATE override both (tests, development).
# --------------------------------------------------------------------------- #
CODE_DIR = os.path.dirname(os.path.abspath(__file__))


def _pick(env, new, markers):
    if os.environ.get(env):
        return os.environ[env]
    return new if any(os.path.exists(os.path.join(new, m)) for m in markers) else CODE_DIR


CONF_DIR = _pick("SPIDERMAP_CONF", "/etc/spidermap", ("zabbix.env", "devices.json"))
STATE_DIR = _pick("SPIDERMAP_STATE", "/var/lib/spidermap/state", (".",))


def _load_env_file(path):
    """Read export KEY="value" lines (zabbix.env) into os.environ, without overriding what is already set.
    So 'python3 spidermap.py ...' works without '. ./zabbix.env' first."""
    import shlex
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        k, sep, v = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k.strip()):
            continue
        try:
            parts = shlex.split(v, comments=True)
        except ValueError:
            continue
        os.environ.setdefault(k.strip(), parts[0] if parts else "")


_load_env_file(os.path.join(CONF_DIR, "zabbix.env"))

ZABBIX_URL = os.environ.get("ZABBIX_URL", "http://127.0.0.1/zabbix/api_jsonrpc.php")
ZABBIX_TOKEN = os.environ.get("ZABBIX_TOKEN", "")
VERIFY_TLS = os.environ.get("ZABBIX_VERIFY_TLS", "1") != "0"

# Per-network settings live in devices.json ("settings"), never in this file:
#   "aliases":      {"pve": "Proxmox"}                         LLDP name -> Zabbix host name
#   "manual_links": [["Zabbix server", "Proxmox", "VM on vmbr0"]]  links LLDP cannot see
#   "core_switch":  "SW-Core"                                  switch drawn at the top of the Zabbix map
def _local_settings():
    try:
        with open(os.path.join(CONF_DIR, "devices.json"), encoding="utf-8") as f:
            return json.load(f).get("settings", {})
    except Exception:  # noqa: BLE001
        return {}


_LS = _local_settings()
ALIASES = _LS.get("aliases", {})
MANUAL_LINKS = [tuple(x) for x in _LS.get("manual_links", [])]
CORE_SWITCH = _LS.get("core_switch", "")
INCLUDE_UNKNOWN_NEIGHBORS = True   # draw LLDP neighbours that are not Zabbix hosts

# How live traffic is written on map links.
# Zabbix map labels only accept a single {?last(/host/key)} (no maths, no rounding), so in
# "calc" mode the script creates small calculated items on the switch (bits/s -> Mbps, 2 decimals)
# and shows those. "raw" shows plain bits per second and creates nothing.
TRAFFIC_MODE = "calc"
MAP_NAME = "Network topology (auto)"
DASHBOARD_NAME = "Network & Proxmox (auto)"
MAP_W, MAP_H = 1200, 720

# Show every switch port on the map, each with a line coloured live by its link status
SHOW_PORTS = True
SHOW_DOWN_PORTS = True        # False = only ports that are up
PORT_COLS = 1                 # port columns next to each switch (1 = clean fan, labels on the right)
UP_COLOR, DOWN_COLOR = "34AF67", "B0B0B0"
MAIN_X, LEVEL_H = 120, 280    # main column and minimum vertical gap
PORT_X0, PORT_DX, PORT_DY = 320, 460, 44

# LLDP-MIB OIDs
LLDP_LOC_PORT_ID = "1.0.8802.1.1.2.1.3.7.1.3"
LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"
LLDP_REM_ENTRY = "1.0.8802.1.1.2.1.4.1.1"      # .6 portIdSubtype .7 portId .8 portDesc .9 sysName .5 chassisId
LLDP_REM_MAN_ADDR = "1.0.8802.1.1.2.1.4.2.1.3"

SEVERITY_COLORS = {5: "E45959", 4: "E97659", 3: "FFA059", 2: "FFC859", 1: "7499FF", 0: "97AAB3"}


# --------------------------------------------------------------------------- #
# Zabbix API
# --------------------------------------------------------------------------- #
class ZabbixError(Exception):
    pass


class Zabbix:
    def __init__(self, url, token, verify=True):
        self.url, self.verify, self._id = url, verify, 0
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json-rpc",
                               "Authorization": f"Bearer {token}"})

    def version(self):
        r = requests.post(self.url, json={"jsonrpc": "2.0", "method": "apiinfo.version",
                                          "params": {}, "id": 0},
                          verify=self.verify, timeout=30)
        r.raise_for_status()
        return r.json()["result"]

    def call(self, method, params=None):
        self._id += 1
        r = self.s.post(self.url, json={"jsonrpc": "2.0", "method": method,
                                        "params": params if params is not None else {},
                                        "id": self._id},
                        verify=self.verify, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            e = data["error"]
            raise ZabbixError(f"{method}: {e.get('message')} {e.get('data')}")
        return data["result"]


# --------------------------------------------------------------------------- #
# SNMP helpers
# --------------------------------------------------------------------------- #
AUTH_PROTO = {"0": "MD5", "1": "SHA", "2": "SHA-224", "3": "SHA-256", "4": "SHA-384", "5": "SHA-512"}
PRIV_PROTO = {"0": "DES", "1": "AES", "2": "AES-192", "3": "AES-256", "4": "AES-192C", "5": "AES-256C"}
SEC_LEVEL = {"0": "noAuthNoPriv", "1": "authNoPriv", "2": "authPriv"}
HEXBYTES = re.compile(r"^([0-9A-Fa-f]{2}[ :-]?){6}$")
MACRO_RX = re.compile(r"^\{\$[A-Z0-9_.]+\}$")


def resolve(value, macros):
    value = value or ""
    if MACRO_RX.match(value):
        if value in macros and macros[value] is not None:
            return macros[value]
        raise RuntimeError(f"macro {value} has no readable value (secret macro?). "
                           f"Use --community to pass it directly.")
    return value


SNMP_CONF_DIR = os.environ.get("SPIDERMAP_SNMP_DIR", "/var/lib/spidermap/snmp")


class SnmpAuth(list):
    """Options for net-snmp commands. The credentials are NOT among them: they are in a private snmp.conf
    (env SNMPCONFPATH), so they never appear on the command line, where any local user could read them (ps)."""
    env = None

    def __add__(self, other):
        r = SnmpAuth(list(self) + list(other))
        r.env = self.env
        return r


def _conf_word(v):
    """A value for snmp.conf, quoted when needed (spaces, quotes, #, backslashes)."""
    v = str(v if v is not None else "")
    if v and not re.search(r'[\s"#\\]', v):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _private_snmp_env(target, lines):
    """Write the credentials for one target to <SNMP_CONF_DIR>/<id>/snmp.conf (folders 700, file 600)."""
    content = "mibs :\n" + "".join(f"{k} {_conf_word(v)}\n" for k, v in lines)
    key = hashlib.sha256(target.encode()).hexdigest()[:16]
    for base in (SNMP_CONF_DIR, os.path.join(tempfile.gettempdir(), f"spidermap-snmp-{os.getuid()}")):
        d = os.path.join(base, key)
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(base, 0o700)
            os.chmod(d, 0o700)
            f = os.path.join(d, "snmp.conf")
            old = open(f, encoding="utf-8").read() if os.path.exists(f) else None
            if old != content:
                fd = os.open(f + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.replace(f + ".tmp", f)
            return {**os.environ, "SNMPCONFPATH": d, "SNMP_PERSISTENT_DIR": d}
        except PermissionError:
            continue
    raise RuntimeError("cannot write the private SNMP settings (check /var/lib/spidermap/snmp)")


def snmp_auth(iface, macros, community_override=None):
    d = iface.get("details") or {}
    ver = str(d.get("version", "2"))
    host = iface["ip"] if str(iface.get("useip", "1")) == "1" else iface["dns"]
    target = f"udp:{host}:{iface.get('port') or 161}"
    if ver == "3":
        lvl = str(d.get("securitylevel", "0"))
        lines = [("defVersion", "3"), ("defSecurityName", resolve(d.get("securityname"), macros)),
                 ("defSecurityLevel", SEC_LEVEL[lvl])]
        if d.get("contextname"):
            lines.append(("defContext", resolve(d["contextname"], macros)))
        if lvl in ("1", "2"):
            lines += [("defAuthType", AUTH_PROTO.get(str(d.get("authprotocol", "0")), "MD5")),
                      ("defAuthPassphrase", resolve(d.get("authpassphrase"), macros))]
        if lvl == "2":
            lines += [("defPrivType", PRIV_PROTO.get(str(d.get("privprotocol", "0")), "DES")),
                      ("defPrivPassphrase", resolve(d.get("privpassphrase"), macros))]
    else:
        community = community_override or resolve(d.get("community", "{$SNMP_COMMUNITY}"), macros)
        lines = [("defVersion", "1" if ver == "1" else "2c"), ("defCommunity", community)]
    auth = SnmpAuth()
    auth.env = _private_snmp_env(target, lines)
    return auth, target


def _clean(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        v = v[1:-1]
    v = v.strip()
    if HEXBYTES.match(v):
        return ":".join(re.findall(r"[0-9A-Fa-f]{2}", v)).lower()
    return v


def snmpwalk(auth, target, base):
    cmd = ["snmpwalk", "-t", "3", "-r", "1", "-On", "-Oq", *auth, target, base]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=getattr(auth, "env", None))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"snmpwalk timeout on {target}")
    if out.returncode != 0 and not out.stdout:
        raise RuntimeError(out.stderr.strip() or "snmpwalk failed")
    prefix, res, last = "." + base + ".", {}, None
    for line in out.stdout.splitlines():
        if line.startswith("."):
            oid, _, val = line.partition(" ")
            last = oid[len(prefix):] if oid.startswith(prefix) else None
            if last is not None:
                res[last] = val
        elif last is not None:
            res[last] += " " + line.strip()
    return {k: _clean(v) for k, v in res.items()}


def lldp_neighbors(auth, target):
    loc_id = snmpwalk(auth, target, LLDP_LOC_PORT_ID)
    loc_desc = snmpwalk(auth, target, LLDP_LOC_PORT_DESC)
    rem_raw = snmpwalk(auth, target, LLDP_REM_ENTRY)
    try:
        man_raw = snmpwalk(auth, target, LLDP_REM_MAN_ADDR)
    except RuntimeError:
        man_raw = {}

    rem = {}
    for suffix, val in rem_raw.items():
        parts = suffix.split(".")
        if len(parts) < 4:
            continue
        col, key = parts[0], tuple(parts[1:4])     # (timeMark, localPortNum, remIndex)
        rem.setdefault(key, {})[col] = val

    mgmt = {}
    for suffix in man_raw:
        p = suffix.split(".")
        # t.port.idx.addrSubtype(1=ipv4).len(4).a.b.c.d
        if len(p) >= 9 and p[3] == "1" and p[4] == "4":
            mgmt[tuple(p[0:3])] = ".".join(p[5:9])

    result = []
    for key, f in rem.items():
        port_num = key[1]
        local_names = [n for n in (loc_id.get(port_num), loc_desc.get(port_num)) if n]
        subtype, pid, pdesc = f.get("6", ""), f.get("7", ""), f.get("8", "")
        if subtype in ("3", "4", "7") or ":" in pid:      # MAC / address / local -> prefer description
            remote_port = pdesc or pid
        else:
            remote_port = pid or pdesc
        result.append({
            "local_port": local_names[0] if local_names else f"port {port_num}",
            "local_names": local_names,
            "sysname": f.get("9", ""),
            "chassis": f.get("5", ""),
            "remote_port": remote_port,
            "mgmt_ip": mgmt.get(key, ""),
        })
    return result


# --------------------------------------------------------------------------- #
# Zabbix data helpers
# --------------------------------------------------------------------------- #
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def load_hosts(api):
    hosts = api.call("host.get", {
        "output": ["hostid", "host", "name", "status"],
        "selectInterfaces": ["type", "ip", "dns", "useip", "port", "details"],
        "selectParentTemplates": ["templateid", "name"],
        "selectMacros": ["macro", "value"],
    })
    glob = {m["macro"]: m.get("value") for m in
            api.call("usermacro.get", {"globalmacro": True, "output": ["macro", "value"]})}
    tids = sorted({t["templateid"] for h in hosts for t in h["parentTemplates"]})
    tmacros = {}
    if tids:
        for m in api.call("usermacro.get", {"templateids": tids, "output": ["hostid", "macro", "value"]}):
            tmacros.setdefault(m["hostid"], {})[m["macro"]] = m.get("value")
    for h in hosts:
        macros = dict(glob)
        for t in h["parentTemplates"]:
            macros.update(tmacros.get(t["templateid"], {}))
        macros.update({m["macro"]: m.get("value") for m in h.get("macros", [])})
        h["_macros"] = macros
        tnames = " ".join(t["name"] for t in h["parentTemplates"])
        h["_snmp"] = next((i for i in h["interfaces"] if str(i["type"]) == "2"), None)
        if h["_snmp"] or re.search(r"(?i)switch|huawei|vrp|cisco|mikrotik|aruba|netgear", tnames):
            h["_role"] = "switch"
        elif re.search(r"(?i)proxmox", tnames):
            h["_role"] = "proxmox"
        elif re.search(r"(?i)zabbix server", tnames):
            h["_role"] = "zabbix"
        else:
            h["_role"] = "server"
        ips = {i["ip"] for i in h["interfaces"] if i.get("ip") and i["ip"] != "127.0.0.1"}
        for m in h.get("macros", []):
            ips.update(IPV4.findall(m.get("value") or ""))
        h["_ips"] = ips
    return hosts


def match_host(nb, hosts):
    if nb["mgmt_ip"]:
        for h in hosts:
            if nb["mgmt_ip"] in h["_ips"]:
                return h
    names = [nb["sysname"], nb["sysname"].split(".")[0]]
    for n in names:
        if n in ALIASES:
            n = ALIASES[n]
        for h in hosts:
            if n and n.lower() in (h["host"].lower(), h["name"].lower()):
                return h
    return None


def iface_objects(api, hostid, names):
    """Find Bits received / Bits sent items and interface triggers for a port name."""
    for ifname in names:
        if not ifname:
            continue
        items = api.call("item.get", {"hostids": hostid, "output": ["itemid", "key_", "name"],
                                      "search": {"name": f"Interface {ifname}("}})
        trig = api.call("trigger.get", {"hostids": hostid, "output": ["triggerid", "priority"],
                                        "search": {"description": f"Interface {ifname}("}})
        rx = next((i for i in items if i["name"].endswith("Bits received")), None)
        tx = next((i for i in items if i["name"].endswith("Bits sent")), None)
        if rx or tx or trig:
            return rx, tx, trig
    return None, None, []


def ensure_mbps_item(api, host, item, direction):
    """Create (once) a calculated item that shows an interface's traffic in Mbps."""
    m = re.search(r"\[(.*)\]", item["key_"])
    param = m.group(1) if m else item["itemid"]
    key = f"autotopo.mbps.{direction}[{param}]"
    if not api.call("item.get", {"hostids": host["hostid"], "output": ["itemid"], "filter": {"key_": key}}):
        iface = item["name"].rsplit(":", 1)[0]
        api.call("item.create", {
            "hostid": host["hostid"], "type": 15, "value_type": 0, "delay": "1m",
            "name": f"{iface}: Mbps {direction} (map)", "key_": key, "units": "Mbps",
            "params": f"round(last(//{item['key_']})/1000000,2)",
            "history": "7d", "trends": "0",
            "tags": [{"tag": "source", "value": "autotopo"}]})
        print(f"  + item {host['name']}: {key}")
    return key


def find_items(api, hostid, include, exclude=None, top=None, prefer_pct=False):
    items = api.call("item.get", {"hostids": hostid, "output": ["itemid", "name", "lastvalue", "units", "value_type"],
                                  "filter": {"value_type": [0, 3], "status": 0}})
    out = [i for i in items if re.search(include, i["name"]) and not (exclude and re.search(exclude, i["name"]))]
    if prefer_pct:
        out.sort(key=lambda i: i["units"] != "%")
    if top:
        out.sort(key=lambda i: float(i["lastvalue"] or 0), reverse=True)
        out = out[:top]
    return out


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #
def norm(p):
    return re.sub(r"\s+", "", (p or "").lower())


def build_topology(api, hosts, community):
    nodes = {h["hostid"]: {"key": h["hostid"], "host": h, "label": h["name"]} for h in hosts}
    links = []

    def add_link(a, ap, b, bp, extra):
        sa, sb = {(a, norm(ap)), (b, norm(bp))}, {a, b}
        for l in links:
            if {l["a"], l["b"]} == sb and sa & {(l["a"], norm(l["a_port"])), (l["b"], norm(l["b_port"]))}:
                return
        links.append(dict(a=a, a_port=ap, b=b, b_port=bp, **extra))

    for h in hosts:
        if not h["_snmp"] or str(h["status"]) != "0":
            continue
        try:
            auth, target = snmp_auth(h["_snmp"], h["_macros"], community)
            nbs = lldp_neighbors(auth, target)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {h['name']}: {e}")
            continue
        print(f"  {h['name']}: {len(nbs)} LLDP neighbour(s)")
        for nb in nbs:
            peer = match_host(nb, hosts)
            tag = peer["name"] if peer else "(not in Zabbix)"
            print(f"     {nb['local_port']:<26} -> {nb['sysname'] or nb['chassis']:<20} "
                  f"{nb['remote_port']:<22} {nb['mgmt_ip']:<15} => {tag}")
            if peer:
                bkey = peer["hostid"]
            elif INCLUDE_UNKNOWN_NEIGHBORS:
                bkey = "unknown:" + (nb["sysname"] or nb["chassis"] or nb["mgmt_ip"])
                nodes.setdefault(bkey, {"key": bkey, "host": None,
                                        "label": (nb["sysname"] or nb["chassis"]) +
                                                 (f"\n{nb['mgmt_ip']}" if nb["mgmt_ip"] else "")})
            else:
                continue
            add_link(h["hostid"], nb["local_port"], bkey, nb["remote_port"],
                     {"a_names": nb["local_names"]})

    by_name = {h["name"]: h["hostid"] for h in hosts} | {h["host"]: h["hostid"] for h in hosts}
    for a, b, label in MANUAL_LINKS:
        if a in by_name and b in by_name:
            ia, ib = by_name[a], by_name[b]
            if not any({l["a"], l["b"]} == {ia, ib} for l in links):
                links.append(dict(a=ia, a_port="", b=ib, b_port="", a_names=[], manual=label))
    return nodes, links


def layout(nodes, links, port_rows=None):
    """Vertical chain in BFS order from the core switch, leaving room on the right for port grids."""
    port_rows = port_rows or {}
    adj = {k: set() for k in nodes}
    for l in links:
        adj[l["a"]].add(l["b"])
        adj[l["b"]].add(l["a"])
    switches = [k for k, n in nodes.items() if n["host"] and n["host"]["_role"] == "switch"]
    core = [k for k in switches
            if CORE_SWITCH and CORE_SWITCH.lower() in (nodes[k]["host"]["host"].lower(), nodes[k]["host"]["name"].lower())]
    if core:
        roots = core[:1]
    elif switches:
        roots = [max(sorted(switches), key=lambda k: len(adj[k]))]
    else:
        roots = [max(adj, key=lambda k: len(adj[k]))] if nodes else []
    order, seen, queue = [], set(roots), list(roots)
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for nxt in sorted(adj[cur], key=lambda k: nodes[k]["label"]):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    order += [k for k in nodes if k not in seen]
    pos, y = {}, 60
    for k in order:
        pos[k] = (MAIN_X, y)
        y += max(LEVEL_H, port_rows.get(k, 0) * PORT_DY + 160)
    return pos


def pick_icon(images, prefs, suffix=None):
    for p in prefs:
        if p in images:
            return images[p]
    if suffix:
        for name, iid in sorted(images.items()):
            if name.endswith(suffix):
                return iid
    return next(iter(images.values()))


def short_if(name):
    for long, short in (("XGigabitEthernet", "XGE"), ("GigabitEthernet", "GE"),
                        ("MultiGE", "MGE"), ("FastEthernet", "FE"), ("Ethernet", "Eth")):
        if name.startswith(long):
            return short + name[len(long):]
    return name


def status_items(api, hostid):
    """Interface name -> itemid of its 'Operational status' item."""
    res = {}
    for it in api.call("item.get", {"hostids": hostid, "output": ["itemid", "name"],
                                    "search": {"name": "Operational status"}}):
        m = re.match(r"Interface (.+?)\(", it["name"])
        if m:
            res[norm(m.group(1))] = it["itemid"]
    return res


def status_indicator(itemid):
    return {"indicator_type": 2, "itemid": itemid, "color": UP_COLOR,
            "thresholds": [{"threshold": "2", "color": DOWN_COLOR}]}


def build_map(api, nodes, links, port_data=None):
    port_data = port_data or {}
    images = {i["name"]: i["imageid"] for i in
              api.call("image.get", {"output": ["imageid", "name"], "filter": {"imagetype": 1}})}
    icons = {
        "switch": pick_icon(images, ["Switch_(96)", "Switch_(64)", "Switch_(48)"]),
        "proxmox": pick_icon(images, ["Server_(96)", "Server_(64)", "Rackmountable_2U_server_3D_(96)"]),
        "zabbix": pick_icon(images, ["Zabbix_server_3D_(96)", "Server_(96)"]),
        "server": pick_icon(images, ["Server_(96)", "Server_(64)"]),
        "unknown": pick_icon(images, ["Network_(96)", "Network_(64)", "Cloud_(96)"]),
        "port_dev": pick_icon(images, ["Workstation_(24)", "Notebook_(24)", "Network_(24)"], "_(24)"),
        "port_free": pick_icon(images, ["Network_(24)", "Hub_(24)", "Switch_(24)"], "_(24)"),
    }
    hosts_by_id = {k: n["host"] for k, n in nodes.items() if n["host"]}
    st_cache = {}

    def st_item(host, names):
        if host["hostid"] not in st_cache:
            st_cache[host["hostid"]] = status_items(api, host["hostid"])
        return next((st_cache[host["hostid"]][norm(n)] for n in names if norm(n) in st_cache[host["hostid"]]), None)

    # ports that already have a real (LLDP) link
    linked = {}
    for l in links:
        if not l.get("manual"):
            linked.setdefault(l["a"], set()).update(norm(n) for n in (l["a_names"] or [l["a_port"]]))
            linked.setdefault(l["b"], set()).add(norm(l["b_port"]))
    stubs = {}
    for k, host in hosts_by_id.items():
        for p in port_data.get(host["hostid"], []):
            if norm(p["name"]) in linked.get(k, set()):
                continue
            if p["status"] != "up" and not SHOW_DOWN_PORTS:
                continue
            stubs.setdefault(k, []).append(p)
    port_rows = {k: -(-len(v) // PORT_COLS) for k, v in stubs.items()}
    pos = layout(nodes, links, port_rows)

    sid, selements = {}, []
    for i, (k, n) in enumerate(nodes.items(), start=1):
        sid[k] = str(i)
        x, y = pos[k]
        if n["host"]:
            selements.append({"selementid": sid[k], "elementtype": 0,
                              "elements": [{"hostid": n["host"]["hostid"]}],
                              "iconid_off": icons[n["host"]["_role"]],
                              "label": "{HOST.NAME}\r\n" + ("{HOST.CONN}" if n["host"]["interfaces"]
                                                              else ", ".join(sorted(n["host"]["_ips"]))),
                              "x": x, "y": y})
        else:
            selements.append({"selementid": sid[k], "elementtype": 4,
                              "iconid_off": icons["unknown"], "label": n["label"], "x": x, "y": y})

    map_links = []
    for l in links:
        link = {"selementid1": sid[l["a"]], "selementid2": sid[l["b"]], "color": "2E7D32", "drawtype": 2}
        if l.get("manual"):
            link.update(label=l["manual"], color="7A8A99", drawtype=4)
        else:
            ha = hosts_by_id[l["a"]]
            rx, tx, trig = iface_objects(api, ha["hostid"], l["a_names"] or [l["a_port"]])
            def val(it, direction):
                if not it:
                    return ""
                if TRAFFIC_MODE == "calc":
                    return "{?last(/" + ha["host"] + "/" + ensure_mbps_item(api, ha, it, direction) + ")}"
                return "{?last(/" + ha["host"] + "/" + it["key_"] + ")} bps"
            v_in, v_out = val(rx, "in"), val(tx, "out")
            a_end = (nodes[l["a"]]["label"].splitlines()[0], short_if(l["a_port"]))
            b_end = (nodes[l["b"]]["label"].splitlines()[0], short_if(l["b_port"]))
            a_top = pos[l["a"]][1] <= pos[l["b"]][1]
            top, bot = (a_end, b_end) if a_top else (b_end, a_end)
            down, up = (v_out, v_in) if a_top else (v_in, v_out)   # measured on the a-side port
            label = f"{top[0]}  {top[1]}\r\n{bot[0]}  {bot[1]}"
            traffic = "   ".join(x for x in ((f"\u25bc {down}" if down else ""),
                                            (f"\u25b2 {up}" if up else "")) if x)
            link["label"] = label + ("\r\n" + traffic if traffic else "")
            st = st_item(ha, l["a_names"] or [l["a_port"]])
            if st:
                link.update(status_indicator(st))
            elif trig:
                link["linktriggers"] = [{"triggerid": t["triggerid"], "drawtype": 2,
                                         "color": SEVERITY_COLORS.get(int(t["priority"]), "E45959")}
                                        for t in trig]
                link["indicator_type"] = 1
        map_links.append(link)

    # one small element per remaining port, linked to its switch
    n_el = len(selements)
    for k, ports in stubs.items():
        host, (x0, y0) = hosts_by_id[k], pos[k]
        for i, p in enumerate(ports):
            n_el += 1
            s_id = str(n_el)
            conn = p.get("connected") or []
            lines = []
            for c in conn[:2]:
                if re.search(r"\d+\.\d+\.\d+\.\d+", c):          # IP known -> drop the MAC
                    c = re.sub(r"\s*([0-9a-f]{2}:){5}[0-9a-f]{2}$", "", c)
                lines.append(c if len(c) <= 45 else c[:44] + "\u2026")
            if len(conn) > 2:
                lines.append(f"+{len(conn) - 2} more")
            if p["status"] == "disabled":
                lines.append("(disabled)")
            dev = "\r\n".join(lines)
            selements.append({
                "selementid": s_id, "elementtype": 4,
                "iconid_off": icons["port_dev" if conn else "port_free"],
                "label": short_if(p["name"]) + ("  " + dev.replace("\r\n", "\r\n         ") if dev else ""),
                "label_location": 2,
                "x": x0 + PORT_X0 + (i // max(1, -(-len(ports) // PORT_COLS))) * PORT_DX,
                "y": y0 - 40 + (i % max(1, -(-len(ports) // PORT_COLS))) * PORT_DY})
            link = {"selementid1": sid[k], "selementid2": s_id, "drawtype": 0,
                    "color": UP_COLOR if p["status"] == "up" else DOWN_COLOR}
            st = st_item(host, [p["name"]])
            if st:
                link.update(status_indicator(st))
            map_links.append(link)

    for m in api.call("map.get", {"output": ["sysmapid"], "filter": {"name": MAP_NAME}}):
        api.call("map.delete", [m["sysmapid"]])
    width = max(MAP_W, MAIN_X + PORT_X0 + PORT_COLS * PORT_DX + 120) if stubs else MAP_W
    height = max(MAP_H, max(e["y"] for e in selements) + 200)
    params = {"name": MAP_NAME, "width": width, "height": height, "label_type": 0,
              "expandmacros": 1, "selements": selements, "links": map_links}
    err = None
    for level in range(3):
        for lk in map_links:
            if level >= 1 and lk.get("indicator_type") == 2:
                lk.pop("itemid", None)
                lk.pop("thresholds", None)
                lk["indicator_type"] = 0
            if level >= 2:
                lk.pop("indicator_type", None)
        try:
            mid = api.call("map.create", params)["sysmapids"][0]
            if level:
                print("  (live link colours not supported by this Zabbix version; links are static)")
            return mid
        except ZabbixError as e:
            err = e
            print(f"  ! {e} - retrying simpler")
    raise err


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
class Dash:
    """Adds widgets one by one, trying field-name variants, so one bad widget never breaks the rest."""

    def __init__(self, api, name, pages):
        self.api, self.pages = api, {p: [] for p in pages}
        for d in api.call("dashboard.get", {"output": ["dashboardid"], "filter": {"name": name}}):
            api.call("dashboard.delete", [d["dashboardid"]])
        self.id = api.call("dashboard.create", {
            "name": name, "display_period": 60, "auto_start": 0,
            "pages": [{"name": p} for p in pages]})["dashboardids"][0]

    def _push(self):
        self.api.call("dashboard.update", {"dashboardid": self.id, "pages": [
            {"name": p, "widgets": w} for p, w in self.pages.items()]})

    def add(self, page, wtype, title, x, y, w, h, variants=({},)):
        err = None
        for fields in variants:
            widget = {"type": wtype, "name": title, "x": x, "y": y, "width": w, "height": h,
                      "view_mode": 0,
                      "fields": [{"type": t, "name": n, "value": v} for n, (t, v) in fields.items()]}
            self.pages[page].append(widget)
            try:
                self._push()
                print(f"  + [{page}] {title}")
                return
            except ZabbixError as e:
                err = e
                self.pages[page].pop()
        print(f"  ! [{page}] {title} skipped: {err}")


def graph_variants(datasets):
    """datasets: list of dict(hosts=[...], items=[...], color='RRGGBB', sum=bool, label='')"""
    def build(with_type, with_agg):
        f = {}
        for i, ds in enumerate(datasets):
            if with_type:
                f[f"ds.{i}.dataset_type"] = (0, 1)
            for j, hname in enumerate(ds["hosts"]):
                f[f"ds.{i}.hosts.{j}"] = (1, hname)
            for j, iname in enumerate(ds["items"]):
                f[f"ds.{i}.items.{j}"] = (1, iname)
            f[f"ds.{i}.color"] = (1, ds.get("color", "1A7C11"))
            if with_agg and ds.get("sum"):
                f[f"ds.{i}.aggregate_function"] = (0, 5)
                f[f"ds.{i}.aggregate_grouping"] = (0, 1)
                f[f"ds.{i}.aggregate_interval"] = (1, "1m")
                if ds.get("label"):
                    f[f"ds.{i}.data_set_label"] = (1, ds["label"])
        return f
    return [build(True, True), build(False, True), build(True, False), build(False, False)]


def one_item_variants(field, ftype, value, extra=None):
    extra = extra or {}
    return [{f"{field}.0": (ftype, value), **extra}, {field: (ftype, value), **extra}]


def build_dashboard(api, hosts, sysmapid):
    switches = [h for h in hosts if h["_role"] == "switch"]
    pve = next((h for h in hosts if h["_role"] == "proxmox"), None)
    pages = ["Overview"] + (["Switches"] if switches else []) + (["Proxmox"] if pve else [])
    d = Dash(api, DASHBOARD_NAME, pages)
    pct = {"min": (1, "0"), "max": (1, "100")}

    # Overview
    d.add("Overview", "problemsbysv", "Problems by severity", 0, 0, 36, 3)
    d.add("Overview", "hostavail", "Host availability", 36, 0, 22, 3)
    d.add("Overview", "clock", "Time", 58, 0, 14, 3)
    if sysmapid:
        d.add("Overview", "map", "Network topology", 0, 3, 46, 11,
              one_item_variants("sysmapid", 8, sysmapid))
    d.add("Overview", "problems", "Current problems", 46, 3, 26, 11)
    if switches:
        names = [h["host"] for h in switches]
        d.add("Overview", "svggraph", "Total switch traffic", 0, 14, 72, 5, graph_variants([
            {"hosts": names, "items": ["*Bits received"], "color": "1E88E5", "sum": True, "label": "Total in"},
            {"hosts": names, "items": ["*Bits sent"], "color": "43A047", "sum": True, "label": "Total out"},
        ]))

    # Switches: one row per switch
    for r, sw in enumerate(switches):
        y = r * 5
        cpu = find_items(api, sw["hostid"], r"(?i)cpu utilization")
        mem = find_items(api, sw["hostid"], r"(?i)memory utilization")
        if cpu:
            d.add("Switches", "gauge", f"{sw['name']} CPU", 0, y, 12, 5,
                  one_item_variants("itemid", 4, cpu[0]["itemid"], pct))
        if mem:
            d.add("Switches", "gauge", f"{sw['name']} memory", 12, y, 12, 5,
                  one_item_variants("itemid", 4, mem[0]["itemid"], pct))
        ports = find_items(api, sw["hostid"], r"Bits (received|sent)$")
        totals = {}
        for it in ports:
            iface = it["name"].rsplit(":", 1)[0]
            totals[iface] = totals.get(iface, 0) + float(it["lastvalue"] or 0)
        busiest = sorted(totals, key=totals.get, reverse=True)[:6]
        if busiest:
            d.add("Switches", "svggraph", f"{sw['name']} busiest ports", 24, y, 48, 5, graph_variants([
                {"hosts": [sw["host"]], "items": [f"{p}: Bits received" for p in busiest], "color": "1E88E5"},
                {"hosts": [sw["host"]], "items": [f"{p}: Bits sent" for p in busiest], "color": "43A047"},
            ]))

    # Proxmox
    if pve:
        guest = r"(?i)(qemu|lxc|vm)\s*\["
        node_cpu = find_items(api, pve["hostid"], r"(?i)node.*cpu", guest, prefer_pct=True)
        node_mem = find_items(api, pve["hostid"], r"(?i)node.*memory.*(usage|util|used)", guest, prefer_pct=True)
        vm_cpu = find_items(api, pve["hostid"], guest + r".*cpu", top=12)
        if node_cpu:
            d.add("Proxmox", "gauge", "Node CPU", 0, 0, 12, 5,
                  one_item_variants("itemid", 4, node_cpu[0]["itemid"], pct if node_cpu[0]["units"] == "%" else {}))
        if node_mem:
            d.add("Proxmox", "gauge", "Node memory", 12, 0, 12, 5,
                  one_item_variants("itemid", 4, node_mem[0]["itemid"], pct if node_mem[0]["units"] == "%" else {}))
        if node_cpu or node_mem:
            d.add("Proxmox", "svggraph", "Node CPU & memory", 24, 0, 48, 5, graph_variants(
                [{"hosts": [pve["host"]], "items": [node_cpu[0]["name"]], "color": "E53935"}] * bool(node_cpu) +
                [{"hosts": [pve["host"]], "items": [node_mem[0]["name"]], "color": "8E24AA"}] * bool(node_mem)))
        if vm_cpu:
            names = {f"items.{i}": (1, it["name"]) for i, it in enumerate(vm_cpu)}
            d.add("Proxmox", "honeycomb", "VMs & containers – CPU", 0, 5, 36, 6, [
                {"hostids.0": (3, pve["hostid"]), **names}, {"hostids": (3, pve["hostid"]), **names}])
            d.add("Proxmox", "svggraph", "Top guests by CPU", 36, 5, 36, 6, graph_variants(
                [{"hosts": [pve["host"]], "items": [it["name"] for it in vm_cpu[:8]], "color": "FB8C00"}]))
        d.add("Proxmox", "problems", "Proxmox problems", 0, 11, 72, 5,
              one_item_variants("hostids", 3, pve["hostid"]))
    return d.id


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Auto-build Zabbix topology map and dashboard")
    ap.add_argument("--dry-run", action="store_true", help="only print discovered topology")
    ap.add_argument("--no-map", action="store_true")
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--community", help="SNMP v1/v2c community (overrides Zabbix macros)")
    ap.add_argument("--no-ports", action="store_true", help="do not draw individual switch ports")
    ap.add_argument("--no-sweep", action="store_true", help="skip the ping sweep used to name devices")
    args = ap.parse_args()

    if not ZABBIX_TOKEN:
        sys.exit("Set ZABBIX_TOKEN (Users -> API tokens) first.")
    if not shutil.which("snmpwalk"):
        sys.exit("snmpwalk not found: apt install snmp")

    api = Zabbix(ZABBIX_URL, ZABBIX_TOKEN, VERIFY_TLS)
    print(f"Zabbix API {api.version()} at {ZABBIX_URL}")
    hosts = load_hosts(api)
    print(f"{len(hosts)} hosts: " + ", ".join(f"{h['name']} ({h['_role']})" for h in hosts))

    print("\nReading LLDP neighbours...")
    nodes, links = build_topology(api, hosts, args.community)
    print(f"\n{len(links)} link(s):")
    for l in links:
        print(f"  {nodes[l['a']]['label']} [{l['a_port']}]  <->  "
              f"{nodes[l['b']]['label'].splitlines()[0]} [{l['b_port'] or l.get('manual', '')}]")
    if args.dry_run:
        return

    sysmapid = None
    if not args.no_map:
        port_data = {}
        if SHOW_PORTS and not args.no_ports:
            import spidermap_ports as zp
            print("\nScanning switch ports...")
            port_data = {h["hostid"]: ports for h, ports in
                         zp.collect(api, hosts, args.community, do_sweep=not args.no_sweep)}
        sysmapid = build_map(api, nodes, links, port_data)
        print(f"\nMap created: '{MAP_NAME}' (id {sysmapid})")
    if not args.no_dashboard:
        print("\nBuilding dashboard...")
        did = build_dashboard(api, hosts, sysmapid)
        print(f"Dashboard created: '{DASHBOARD_NAME}' (id {did})")


if __name__ == "__main__":
    main()
