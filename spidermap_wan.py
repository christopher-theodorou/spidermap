"""The WAN and the internet: the router over SNMP / UPnP, UniFi, outages and uptime.

Part of SpiderMap (see spidermap.py and docs/ARCHITECTURE.md)."""
import subprocess
import datetime
import re
import time

import requests

import spidermap_zabbix as at
from spidermap import _PinnedAdapter, counter_rate, now, snmp_get  # noqa: E402 - spidermap.py loads this module at its end


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


def unifi_fetch(url, site, user, password, fingerprint=""):
    """APs and clients from a UniFi controller (read-only user). UniFi OS first (Cloud Key Gen2+, UDM), then the older
    controller API. Returns {"kind", "devices", "clients"}."""
    s = requests.Session()
    fp = re.sub(r"[^0-9a-fA-F]", "", fingerprint or "").lower()
    requests.packages.urllib3.disable_warnings()
    s.verify = False
    if fp:
        s.mount("https://", _PinnedAdapter(fp))
    url = url.rstrip("/")
    login = {"username": user, "password": password}
    r = s.post(url + "/api/auth/login", json={**login, "remember": False}, timeout=10, verify=False)
    if r.status_code in (401, 403):                          # UniFi OS answered: the user or password is wrong
        raise RuntimeError("the controller refused the login: check the user and password (a local user, role View only)")
    if r.status_code == 200:
        kind, base, hdr = "UniFi OS", url + "/proxy/network", {"X-CSRF-Token": r.headers.get("X-CSRF-Token", "")}
    else:
        r = s.post(url + "/api/login", json=login, timeout=10, verify=False)
        if r.status_code != 200:
            raise RuntimeError(f"the controller refused the login (HTTP {r.status_code}): check the address, user and password")
        kind, base, hdr = "UniFi Network", url, {}
    out = {"kind": kind}
    for what in ("device", "sta"):
        rr = s.get(f"{base}/api/s/{site}/stat/{what}", headers=hdr, timeout=15, verify=False)
        if rr.status_code != 200:
            raise RuntimeError(f"stat/{what}: HTTP {rr.status_code} (is the site '{site}' right?)")
        out["devices" if what == "device" else "clients"] = rr.json().get("data", [])
    return out


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


# ---------------------------------------------------------------------------------------------------------------
# WAN from the gateway's own items in Zabbix - any router or firewall, however Zabbix measures it (agent, SNMP, HTTP)
# ---------------------------------------------------------------------------------------------------------------
WAN_HINT = re.compile(r"(?i)(?<![a-z0-9])(wan\d*|pppoe\d*|ppp\d+|wwan\d*|lte|4g|5g|v?dsl|adsl|internet|isp|uplink|fib(re|er)|ftth|"
                      r"starlink|outside|external)(?![a-z0-9])")
_DIR = re.compile(r"(?i)(received|incoming|inbound|download|\bin\b|rx|sent|outgoing|outbound|upload|\bout\b|tx)")


def _seconds(delay):
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(delay or ""))
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else 60


def item_bps(it, t=None):
    """An item's current value in bit/s, and whether it is stale: (bps or None, age in s).
    bps / b/s - as is; Bps - x8; a raw byte counter (B or no unit) - from its last two values over its interval."""
    t = t or time.time()
    try:
        last, units = float(it.get("lastvalue")), str(it.get("units") or "")
    except (TypeError, ValueError):
        return None, None
    age = t - int(it.get("lastclock") or 0) if it.get("lastclock") else None
    if units in ("bps", "b/s", "bit/s", "bits/s") or units.lower() in ("bps", "bit/s") and units != "Bps":
        return last, age
    if units in ("Bps", "B/s"):
        return last * 8, age
    try:
        prev = float(it.get("prevvalue"))
    except (TypeError, ValueError):
        return None, age
    return (max(0.0, (last - prev)) * 8 / max(1, _seconds(it.get("delay"))) if last >= prev else None), age


def wan_candidates(items):
    """The gateway's traffic items, grouped by interface: [{"iface", "down", "up", "hint"}] - "down" is what comes in,
    "up" what goes out. Found by what they measure (net.if.in/out keys, interface counters, or bps units with an
    in / out name), never by vendor or template."""
    groups = {}
    for it in items:
        key, name = str(it.get("key_", "")), str(it.get("name", ""))
        m = re.match(r"net\.if\.(in|out)\[\s*\"?([^\",\]]+)", key)
        if m:
            d, gid = ("down" if m.group(1) == "in" else "up"), m.group(2)
            gid = re.sub(r"(?i)^if(hc)?(in|out)octets\.?", "idx", gid)          # SNMP templates: by interface index
        elif re.search(r"(?i)if(hc)?(in|out)octets", key):
            d = "down" if re.search(r"(?i)if(hc)?inoctets", key) else "up"
            gid = "idx" + (re.findall(r"\d+", key) or ["?"])[-1]
        elif str(it.get("units", "")) in ("bps", "Bps", "b/s", "B/s") and _DIR.search(name):
            d = "down" if re.search(r"(?i)received|incoming|inbound|download|\bin\b|rx", name) else "up"
            gid = _DIR.sub("", name).strip(" :-")
        else:
            continue
        label = (re.search(r"(?i)interface\s+(.+?)\s*:", name) or re.search(r"(.+)", gid)).group(1)
        g = groups.setdefault(gid, {"iface": label, "tags": ""})
        g[d] = it
        g["tags"] += " " + " ".join(f"{x.get('tag', '')} {x.get('value', '')}" for x in it.get("tags") or [])
    out = []
    for g in groups.values():
        if g.get("down") and g.get("up"):
            out.append({"iface": g["iface"], "down": g["down"], "up": g["up"],
                        "hint": bool(WAN_HINT.search(f"{g['iface']} {g['down'].get('name', '')} {g['tags']}"))})
    return sorted(out, key=lambda x: (not x["hint"], x["iface"]))


def read_zabbix_wan(api, zs, S, gh, t0, usage):
    """The WAN from the gateway's items in Zabbix: {"rx", "tx", "links"} in bit/s, or None (zs["error"] says why).
    The links chosen in Settings, else every interface whose name or tags say WAN. Items are asked for every 15 s."""
    if t0 >= zs.get("cands_next", 0) or zs.get("host") != gh["hostid"]:
        try:
            try:
                items = api.call("item.get", {"hostids": gh["hostid"], "output": ["itemid", "name", "key_", "units", "lastvalue",
                                 "prevvalue", "lastclock", "delay", "status"], "selectTags": "extend", "filter": {"status": "0"}})
            except Exception:  # noqa: BLE001 - older Zabbix without item tags
                items = api.call("item.get", {"hostids": gh["hostid"], "output": ["itemid", "name", "key_", "units", "lastvalue",
                                 "prevvalue", "lastclock", "delay", "status"], "filter": {"status": "0"}})
            zs.update(cands=wan_candidates(items), cands_next=t0 + 600, host=gh["hostid"], hostname=gh["name"])
        except Exception as e:  # noqa: BLE001
            zs.update(error=f"Zabbix items of {gh['name']}: {str(e)[:150]}", cands_next=t0 + 120)
            return None
    chosen = [x for x in (S.get("wan_links") or []) if x.get("down") and x.get("up")]
    links = chosen or [{"name": c["iface"], "down": c["down"]["itemid"], "up": c["up"]["itemid"]} for c in zs.get("cands", []) if c["hint"]]
    if not links:
        zs["error"] = (f"choose the WAN interface of {gh['name']} in Settings > Measurements (WAN from Zabbix)"
                       if zs.get("cands") else "")
        return None
    if t0 < zs.get("next", 0) and zs.get("last"):
        return zs["last"]
    try:
        ids = sorted({str(x[k]) for x in links for k in ("down", "up")})
        got = {i["itemid"]: i for i in api.call("item.get", {"itemids": ids, "output": ["itemid", "name", "units", "lastvalue",
                                                                                         "prevvalue", "lastclock", "delay"]})}
    except Exception as e:  # noqa: BLE001
        zs.update(error=f"Zabbix items of {gh['name']}: {str(e)[:150]}", next=t0 + 30)
        return None
    out, rx, tx, now_ = [], 0.0, 0.0, time.time()
    for x in links:
        d_, u_ = got.get(str(x["down"])), got.get(str(x["up"]))
        if not d_ or not u_:
            zs.update(error=f"the WAN item {x['down'] if not d_ else x['up']} is no longer in Zabbix - choose it again", next=t0 + 60)
            return None
        (r, age_r), (s_, age_s) = item_bps(d_, now_), item_bps(u_, now_)
        stale = max(age_r or 0, age_s or 0) > max(3 * _seconds(d_.get("delay")), 180)
        out.append({"name": x.get("name") or "WAN", "rx": r or 0, "tx": s_ or 0, "stale": stale, "age": int(max(age_r or 0, age_s or 0))})
        if r is not None and s_ is not None and not stale:
            rx, tx = rx + r, tx + s_
    if all(o["stale"] for o in out):
        zs.update(error=f"the WAN items of {gh['name']} have not been updated for {out[0]['age'] // 60} min", next=t0 + 15)
        return None
    dt = min(300, t0 - zs["t"]) if zs.get("t") else 0          # day / month totals from the rate over the time since
    if dt > 0:
        w = usage.setdefault("wan", {})
        for per in ("d", "m"):
            u = w.setdefault(per, [0, 0])
            u[0] += rx * dt / 8
            u[1] += tx * dt / 8
        usage.setdefault("wan_since", now())
    zs.update(t=t0, next=t0 + 15, error="", last={"rx": rx, "tx": tx, "links": out})
    return zs["last"]


def measure_wan(S, data, hosts, sw_wan, upnp, rsnmp, usage, t0, api=None):
    """The WAN throughput: (rate or None, "zabbix" | "snmp" | "upnp" | "switch" | None, reason when there is no rate).
    The router's WAN by default: its own traffic items in Zabbix (any router or firewall, also without SNMP), else its
    SNMP when it is an SNMP host in Zabbix, else UPnP. No other numbers are put in its place: without a router reading
    the result is None, with the reason."""
    if S["wan_source"] == "switch":
        return sw_wan, ("switch" if sw_wan else None), ("" if sw_wan else "no traffic data for the switch port to the gateway")
    gw_ip = next((n.get("ip") for n in data["nodes"] if n.get("kind") == "router" and n.get("ip")), None)
    why = []
    gz = next((h for h in hosts if gw_ip and gw_ip in (h.get("_ips") or ()) and str(h.get("status", "0")) == "0"), None)
    if gz and api is not None:
        r = read_zabbix_wan(api, rsnmp.setdefault("zbx", {}), S, gz, t0, usage)
        if r:
            return r, "zabbix", ""
        if rsnmp["zbx"].get("error"):
            why.append(rsnmp["zbx"]["error"])
    gh = next((h for h in hosts if h.get("_snmp") and gw_ip and gw_ip in (h.get("_ips") or [h["_snmp"].get("ip")])), None)
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
    if not gh and not gz:
        why.append("for exact WAN figures add the gateway to Zabbix (with an agent or SNMP)")
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


def upstream_router(gw_ip):
    """The second hop to the internet, when it is a private address other than the gateway: a router before the
    gateway (double NAT). A provider's carrier-grade NAT (100.64.0.0/10) is not a private address and is not shown."""
    import ipaddress
    try:
        r = subprocess.run(["ping", "-n", "-c", "1", "-W", "2", "-t", "2", "1.1.1.1"], capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"From (\d+\.\d+\.\d+\.\d+)", r.stdout)
    if not m:
        return None
    ip = m.group(1)
    try:
        return ip if ip != gw_ip and ipaddress.ip_address(ip).is_private else None
    except ValueError:
        return None
