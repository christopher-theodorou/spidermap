"""SpiderMap - network controllers: TP-Link Omada, UniFi (Site Manager cloud and local consoles), Grandstream GWN.

Read only. Each connection lists its sites; you choose which to import. What a controller knows - its access points,
switches and gateways, every client with its access point (Wi-Fi) or switch port (wired), and wireless uplinks (mesh)
- is merged into the map by MAC address, beside what SpiderMap finds by itself. Several connections, of different
vendors, can be used at once: one map across vendors.

Secrets (client secret, API key) are kept in secrets.json as "ctl:<connection id>" - never in devices.json, never on
the page, never exported. Field names differ between vendors and versions, so every value is read from the first of
several known names, and the shape of what came back is kept for Collector health.
"""
import hashlib
import ipaddress
import json
import re
import time
import urllib.parse

import requests

KINDS = {
    "omada": "TP-Link Omada (Open API)",
    "unifi_cloud": "UniFi Site Manager (api.ui.com)",
    "unifi_local": "UniFi console (local, API key)",
    "gwn": "Grandstream GWN Cloud / Manager (experimental)",
}
TIMEOUT = 15
MAX_PAGES = 20


def mac_norm(v):
    h = re.sub(r"[^0-9a-fA-F]", "", str(v or ""))
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)).lower() if len(h) == 12 else ""


def g(d, *keys, default=""):
    """The first of several possible field names that has a value (vendors and versions differ)."""
    for k in keys:
        cur = d
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, "", [], {}):
            return cur
    return default


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _local(url):
    """A controller on the local network: its certificate is usually self-signed."""
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return host.endswith((".local", ".lan", ".home", ".internal")) or "." not in host


def session(conn, pinned_adapter=None):
    """A requests session for one connection: certificates checked, or pinned by fingerprint, or - only for a
    controller on the local network without a fingerprint - not checked (said so in Collector health)."""
    s = requests.Session()
    s.headers["User-Agent"] = "SpiderMap"
    fp = str(conn.get("fingerprint") or "").replace(":", "").lower()
    if fp and pinned_adapter:
        s.mount("https://", pinned_adapter(fp))
        s.verify = False
    elif _local(conn.get("url", "")):
        s.verify = False
    return s


def shape(obj, depth=0):
    """The structure of an answer (keys and types, no values) - for Collector health when a field is missing."""
    if depth > 3:
        return "…"
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


# --------------------------------------------------------------------------------------------- TP-Link Omada
def _omada(conn, secret, s):
    base = conn["url"].rstrip("/")
    oid = conn.get("omada_id", "")
    r = s.post(f"{base}/openapi/authorize/token", params={"grant_type": "client_credentials"},
               json={"omadacId": oid, "client_id": conn.get("client_id", ""), "client_secret": secret}, timeout=TIMEOUT)
    j = r.json()
    if j.get("errorCode") not in (0, None) or not g(j, "result.accessToken"):
        raise RuntimeError(f"Omada sign-in refused: {j.get('msg') or j.get('errorCode')}")
    s.headers["Authorization"] = "AccessToken=" + j["result"]["accessToken"]

    def pages(path):
        out = []
        for page in range(1, MAX_PAGES + 1):
            j = s.get(f"{base}/openapi/v1/{oid}{path}", params={"page": page, "pageSize": 1000}, timeout=TIMEOUT).json()
            if j.get("errorCode") not in (0, None):
                raise RuntimeError(f"Omada {path}: {j.get('msg') or j.get('errorCode')}")
            res = j.get("result") or {}
            rows = res.get("data") if isinstance(res, dict) else res
            out += rows or []
            if not rows or len(out) >= int(g(res, "totalRows", default=len(out)) if isinstance(res, dict) else len(out)):
                return out
        return out
    return pages


def omada_sites(conn, secret, s):
    pages = _omada(conn, secret, s)
    return [{"id": str(g(x, "siteId", "id")), "name": g(x, "name", default="site")} for x in pages("/sites")]


def omada_site(conn, secret, s, site_id):
    pages = _omada(conn, secret, s)
    devs, clis = pages(f"/sites/{site_id}/devices"), pages(f"/sites/{site_id}/clients")
    devices = [{"mac": mac_norm(g(d, "mac")), "name": g(d, "name"), "model": g(d, "model", "showModel"),
                "ip": g(d, "ip", "ipAddress"), "type": str(g(d, "type", "deviceType")).lower(),
                "firmware": g(d, "firmwareVersion", "version"),
                "uplink_mac": mac_norm(g(d, "uplinkDeviceMac", "uplinkMac", "uplink.mac", "parentMac")),
                "uplink_port": g(d, "uplinkDevicePort", "uplinkPort", "uplink.port"),
                "wireless_uplink": bool(g(d, "wirelessLinked", "meshed", "isMesh", default=False))
                or str(g(d, "uplinkType", "linkType")).lower() in ("wireless", "mesh")} for d in devs]
    clients = [{"mac": mac_norm(g(c, "mac")), "name": g(c, "name", "hostName"), "ip": g(c, "ip"),
                "wired": not bool(g(c, "wireless", default=False)) if "wireless" in c else not g(c, "apMac", "ssid"),
                "ap_mac": mac_norm(g(c, "apMac")), "ssid": g(c, "ssid"), "signal": _num(g(c, "rssi", "signalLevel", "signal")),
                "switch_mac": mac_norm(g(c, "switchMac", "gatewayMac")), "port": g(c, "port", "switchPort")} for c in clis]
    return devices, clients, {"device": shape(devs[0]) if devs else None, "client": shape(clis[0]) if clis else None}


# ---------------------------------------------------------------------------------------------------- UniFi
def _unifi_base(conn, host_id=""):
    if conn["kind"] == "unifi_cloud":
        return f"https://api.ui.com/v1/connector/consoles/{host_id}/proxy/network/integration/v1"
    return conn["url"].rstrip("/") + "/proxy/network/integration/v1"


def _unifi_all(s, url):
    out, off = [], 0
    for _ in range(MAX_PAGES):
        j = s.get(url, params={"offset": off, "limit": 200}, timeout=TIMEOUT).json()
        rows = j.get("data") if isinstance(j, dict) else j
        out += rows or []
        if not rows or len(rows) < 200:
            return out
        off += 200
    return out


def unifi_sites(conn, secret, s):
    s.headers["X-API-KEY"] = secret
    s.headers["Accept"] = "application/json"
    if conn["kind"] == "unifi_local":
        return [{"id": "|" + str(g(x, "id")), "name": g(x, "name", "internalReference", default="site")}
                for x in _unifi_all(s, _unifi_base(conn) + "/sites")]
    hosts = s.get("https://api.ui.com/v1/hosts", timeout=TIMEOUT).json().get("data") or []
    out = []
    for h in hosts:
        hid, hname = str(g(h, "id")), g(h, "reportedState.name", "reportedState.hostname", "hostname", default="console")
        try:
            for x in _unifi_all(s, _unifi_base(conn, hid) + "/sites"):
                out.append({"id": f"{hid}|{g(x, 'id')}", "name": f"{hname} · {g(x, 'name', 'internalReference', default='site')}"})
        except Exception:  # noqa: BLE001 - a console that is offline or too old for the Integration API
            continue
    return out


def unifi_site(conn, secret, s, site_key):
    s.headers["X-API-KEY"] = secret
    s.headers["Accept"] = "application/json"
    hid, sid = (site_key.split("|", 1) + [""])[:2]
    base = _unifi_base(conn, hid) + f"/sites/{sid}"
    devs, clis = _unifi_all(s, base + "/devices"), _unifi_all(s, base + "/clients")
    by_id = {str(g(d, "id")): mac_norm(g(d, "macAddress", "mac")) for d in devs}
    devices = []
    for d in devs:
        feats = [str(f).lower() for f in (g(d, "features", default=[]) or [])]
        typ = "ap" if any("access" in f for f in feats) else "switch" if any("switch" in f for f in feats) \
            else "gateway" if any(x in str(g(d, "model")).lower() for x in ("udm", "uxg", "usg", "ucg")) else ""
        devices.append({"mac": by_id.get(str(g(d, "id")), ""), "name": g(d, "name"), "model": g(d, "model"),
                        "ip": g(d, "ipAddress", "ip"), "type": typ, "firmware": g(d, "firmwareVersion", "version"),
                        "uplink_mac": by_id.get(str(g(d, "uplink.deviceId", "uplinkDeviceId")), ""), "uplink_port": "",
                        "wireless_uplink": str(g(d, "uplink.type", "uplinkType")).lower() in ("wireless", "mesh")})
    clients = []
    for c in clis:
        up = by_id.get(str(g(c, "uplinkDeviceId", "uplink.deviceId")), "")
        wired = str(g(c, "type")).upper() == "WIRED"
        clients.append({"mac": mac_norm(g(c, "macAddress", "mac")), "name": g(c, "name", "hostname"), "ip": g(c, "ipAddress", "ip"),
                        "wired": wired, "ap_mac": "" if wired else up, "ssid": g(c, "ssid", "wifiName"),
                        "signal": _num(g(c, "signal", "rssi")), "switch_mac": up if wired else "", "port": g(c, "uplinkPort", "port")})
    return devices, clients, {"device": shape(devs[0]) if devs else None, "client": shape(clis[0]) if clis else None}


# ------------------------------------------------------------------------------------- Grandstream GWN (experimental)
def _gwn(conn, secret, s):
    base = (conn.get("url") or "https://www.gwn.cloud").rstrip("/")
    tok = s.get(base + "/oauth/token", params={"grant_type": "client_credentials", "client_id": conn.get("client_id", ""),
                                                "client_secret": secret}, timeout=TIMEOUT).json().get("access_token")
    if not tok:
        raise RuntimeError("GWN sign-in refused: check the APP ID and Secret Key (API Developer Mode)")

    def call(path, body):
        ts = round(time.time() * 1000)
        params = f"access_token={tok}&appID={conn.get('client_id', '')}&secretKey={secret}&timestamp={ts}"
        raw = json.dumps(body, separators=(",", ":"))
        sig = hashlib.sha256(f"&{params}&{hashlib.sha256(raw.encode()).hexdigest()}&".encode()).hexdigest()
        q = f"access_token={tok}&appID={conn.get('client_id', '')}&timestamp={ts}&signature={sig}"
        j = s.post(f"{base}/oapi/v1.0.0/{path}?{q}", data=raw, headers={"Content-Type": "application/json"}, timeout=TIMEOUT).json()
        if j.get("retCode") not in (0, None):
            raise RuntimeError(f"GWN {path}: {j.get('msg') or j.get('retCode')}")
        d = j.get("data") or {}
        return d.get("result") or d.get("list") or d.get("data") or (d if isinstance(d, list) else [])
    return call


def gwn_sites(conn, secret, s):
    call = _gwn(conn, secret, s)
    return [{"id": str(g(x, "id", "networkId")), "name": g(x, "networkName", "name", default="network")}
            for x in call("network/list", {"type": "asc", "order": "id", "search": "", "pageNum": 1, "pageSize": 100})]


def gwn_site(conn, secret, s, net_id):
    call = _gwn(conn, secret, s)
    nid = int(net_id) if str(net_id).isdigit() else net_id
    aps = call("ap/list", {"search": "", "order": "name", "pageNum": 1, "pageSize": 500, "networkId": nid})
    try:
        clis = call("client/list", {"pageNum": 1, "pageSize": 1000, "networkId": nid})
    except Exception:  # noqa: BLE001 - the client list is optional
        clis = []
    devices = [{"mac": mac_norm(g(d, "mac")), "name": g(d, "name"), "model": g(d, "deviceType", "model"), "ip": g(d, "ip", "ipv4"),
                "type": "ap", "firmware": g(d, "versionFirmware", "firmware"), "uplink_mac": mac_norm(g(d, "uplinkMac", "parentMac")),
                "uplink_port": "", "wireless_uplink": bool(g(d, "mesh", "isMesh", default=False))} for d in aps]
    clients = [{"mac": mac_norm(g(c, "clientMac", "mac")), "name": g(c, "name", "hostname"), "ip": g(c, "ipv4", "ip"),
                "wired": str(g(c, "connectType", "linkType")).lower() == "wired", "ap_mac": mac_norm(g(c, "apMac", "apId")),
                "ssid": g(c, "ssid", "ssidName"), "signal": _num(g(c, "rssi", "signal")), "switch_mac": "", "port": ""} for c in clis]
    return devices, clients, {"device": shape(aps[0]) if aps else None, "client": shape(clis[0]) if clis else None}


SITES = {"omada": omada_sites, "unifi_cloud": unifi_sites, "unifi_local": unifi_sites, "gwn": gwn_sites}
SITE = {"omada": omada_site, "unifi_cloud": unifi_site, "unifi_local": unifi_site, "gwn": gwn_site}


def list_sites(conn, secret, pinned_adapter=None):
    """{"ok", "sites": [{"id", "name"}], "error"} - what this connection offers to import."""
    try:
        return {"ok": True, "sites": SITES[conn["kind"]](conn, secret, session(conn, pinned_adapter)), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "sites": [], "error": str(e)[:200]}


def fetch(conn, secret, pinned_adapter=None):
    """What the chosen sites of one connection know: {"ok", "devices", "clients", "error", "shape", "unchecked_tls"}."""
    out = {"ok": False, "devices": [], "clients": [], "error": "", "shape": {}, "unchecked_tls": False}
    if not secret:
        out["error"] = "no secret saved for this connection"
        return out
    s = session(conn, pinned_adapter)
    out["unchecked_tls"] = s.verify is False and not conn.get("fingerprint")
    try:
        for site in conn.get("sites") or []:
            d, c, sh = SITE[conn["kind"]](conn, secret, s, site)
            out["devices"] += [x for x in d if x["mac"]]
            out["clients"] += [x for x in c if x["mac"]]
            out["shape"] = sh
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
    return out
