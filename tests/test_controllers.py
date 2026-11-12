"""Controllers (Omada, UniFi, GWN): sign-in, sites, devices, clients - against simulated answers, no network."""
import hashlib
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import spidermap_controllers as ctl  # noqa: E402

RESULTS = []


def check(name, ok, got=None):
    RESULTS.append(ok)
    print(f"{'OK ' if ok else 'BAD'} {name}" + ("" if ok else f"  ({got!r})"[:300]))


class Resp:
    def __init__(self, j): self._j = j
    def json(self): return self._j


class FakeSession:
    """Answers by URL path; remembers what was asked (headers, query, body)."""
    def __init__(self, routes):
        self.routes, self.headers, self.verify, self.calls = routes, {}, True, []

    def _ans(self, method, url, **k):
        path = urllib.parse.urlparse(url).path
        self.calls.append((method, url, k, dict(self.headers)))
        for key, val in self.routes.items():
            if path.endswith(key):
                return Resp(val(k) if callable(val) else val)
        raise RuntimeError("no route " + path)

    def get(self, url, **k): return self._ans("GET", url, **k)
    def post(self, url, **k): return self._ans("POST", url, **k)
    def mount(self, *a): pass


def with_session(fake):
    real = ctl.session
    ctl.session = lambda conn, pinned=None: fake
    return real


def test_omada():
    fake = FakeSession({
        "/openapi/authorize/token": {"errorCode": 0, "result": {"accessToken": "AT-1"}},
        "/sites": {"errorCode": 0, "result": {"totalRows": 1, "data": [{"siteId": "s1", "name": "HomeSite"}]}},
        "/sites/s1/devices": {"errorCode": 0, "result": {"totalRows": 2, "data": [
            {"mac": "AA-BB-CC-00-00-01", "name": "EAP610-Outdoor", "type": "ap", "model": "EAP610-Outdoor", "ip": "192.0.2.7",
             "firmwareVersion": "1.2.3", "uplinkDeviceMac": "AA-BB-CC-00-00-02", "uplinkDevicePort": 3},
            {"mac": "AA-BB-CC-00-00-03", "name": "Mesh AP", "type": "ap", "uplinkDeviceMac": "AA-BB-CC-00-00-01", "wirelessLinked": True}]}},
        "/sites/s1/clients": {"errorCode": 0, "result": {"totalRows": 2, "data": [
            {"mac": "11-22-33-44-55-66", "name": "Shelly bulb", "ip": "192.0.2.174", "wireless": True, "apMac": "AA-BB-CC-00-00-01",
             "ssid": "IoT", "rssi": -67},
            {"mac": "00-11-32-98-F6-37", "name": "NAS", "ip": "192.0.2.105", "wireless": False, "switchMac": "98-BA-5F-CE-EC-A9", "port": 14}]}},
    })
    real = with_session(fake)
    conn = {"id": "c1", "kind": "omada", "url": "https://192.0.2.10:8043", "omada_id": "OID", "client_id": "CID", "sites": ["s1"]}
    try:
        sites = ctl.list_sites(conn, "SECRET")
        r = ctl.fetch(conn, "SECRET")
    finally:
        ctl.session = real
    check("Omada: sign-in with the Client ID / Secret / Omada ID, then the sites", sites == {"ok": True, "sites": [{"id": "s1", "name": "HomeSite"}], "error": ""}, sites)
    tok = next(c for c in fake.calls if c[1].endswith("/openapi/authorize/token"))
    check("Omada: Client Credentials sign-in, the access token sent as 'AccessToken=...'",
          tok[2]["json"] == {"omadacId": "OID", "client_id": "CID", "client_secret": "SECRET"} and tok[2]["params"]["grant_type"] == "client_credentials"
          and fake.calls[-1][3].get("Authorization") == "AccessToken=AT-1")
    ap = next(d for d in r["devices"] if d["name"] == "EAP610-Outdoor")
    check("Omada: an access point with its MAC, model, firmware and uplink (switch MAC + port)",
          ap["mac"] == "aa:bb:cc:00:00:01" and ap["type"] == "ap" and ap["uplink_mac"] == "aa:bb:cc:00:00:02" and ap["uplink_port"] == 3, ap)
    check("Omada: a mesh access point - its wireless uplink", next(d for d in r["devices"] if d["name"] == "Mesh AP")["wireless_uplink"])
    wl, wd = r["clients"]
    check("Omada: a Wi-Fi client on its access point, with SSID and signal",
          not wl["wired"] and wl["ap_mac"] == "aa:bb:cc:00:00:01" and wl["ssid"] == "IoT" and wl["signal"] == -67, wl)
    check("Omada: a wired client on its switch and port (the NAS on Port 14)",
          wd["wired"] and wd["switch_mac"] == "98:ba:5f:ce:ec:a9" and wd["port"] == 14, wd)


def test_unifi():
    fake = FakeSession({
        "/v1/hosts": {"data": [{"id": "H1", "reportedState": {"name": "Cloud Key"}}]},
        "/integration/v1/sites": {"data": [{"id": "U1", "name": "Default"}]},
        "/sites/U1/devices": {"data": [{"id": "d1", "macAddress": "74:83:c2:00:00:01", "name": "U6-Lite", "model": "U6 Lite",
                                         "features": ["accessPoint"], "ipAddress": "192.0.2.20"},
                                        {"id": "d2", "macAddress": "74:83:c2:00:00:02", "name": "USW-Lite-8", "model": "USW Lite 8 PoE",
                                         "features": ["switching"]}]},
        "/sites/U1/clients": {"data": [{"type": "WIRELESS", "macAddress": "aa:00:00:00:00:01", "name": "Phone", "uplinkDeviceId": "d1"},
                                        {"type": "WIRED", "macAddress": "aa:00:00:00:00:02", "name": "PC", "uplinkDeviceId": "d2"}]},
    })
    real = with_session(fake)
    conn = {"id": "c2", "kind": "unifi_cloud", "url": "", "sites": ["H1|U1"]}
    try:
        sites = ctl.list_sites(conn, "KEY")
        r = ctl.fetch(conn, "KEY")
    finally:
        ctl.session = real
    check("UniFi Site Manager: consoles from api.ui.com, their sites through the console connector",
          sites["sites"] == [{"id": "H1|U1", "name": "Cloud Key · Default"}] and fake.calls[0][3].get("X-API-KEY") == "KEY", sites)
    check("UniFi: the connector path of the console's own Integration API",
          any("/v1/connector/consoles/H1/proxy/network/integration/v1/sites/U1/devices" in c[1] for c in fake.calls))
    types = {d["name"]: d["type"] for d in r["devices"]}
    check("UniFi: what each device is, from its features (access point, switch)", types == {"U6-Lite": "ap", "USW-Lite-8": "switch"}, types)
    ph, pc = r["clients"]
    check("UniFi: a Wi-Fi client on its access point, a wired one on its switch (by the uplink device)",
          ph["ap_mac"] == "74:83:c2:00:00:01" and not ph["wired"] and pc["wired"] and pc["switch_mac"] == "74:83:c2:00:00:02", r["clients"])


def test_gwn():
    seen = {}

    def ap_list(k):
        seen["body"], seen["q"] = k.get("data"), k.get("params")
        return {"retCode": 0, "data": {"result": [{"mac": "00:0b:82:00:00:01", "name": "GWN7660", "deviceType": "GWN7660", "ip": "192.0.2.30"}]}}
    fake = FakeSession({"/oauth/token": {"access_token": "TOK"},
                        "/network/list": {"retCode": 0, "data": {"result": [{"id": 7, "networkName": "Office"}]}},
                        "/ap/list": ap_list, "/client/list": {"retCode": 0, "data": {"result": []}}})
    real = with_session(fake)
    conn = {"id": "c3", "kind": "gwn", "url": "https://www.gwn.cloud", "client_id": "APP", "sites": ["7"]}
    try:
        sites = ctl.list_sites(conn, "SEC")
        r = ctl.fetch(conn, "SEC")
    finally:
        ctl.session = real
    check("GWN: the networks (sites) after the token", sites["sites"] == [{"id": "7", "name": "Office"}], sites)
    call = next(c for c in fake.calls if "/ap/list" in c[1])
    q = urllib.parse.parse_qs(urllib.parse.urlparse(call[1]).query)
    body = call[2]["data"]
    want = hashlib.sha256(("&access_token=TOK&appID=APP&secretKey=SEC&timestamp=" + q["timestamp"][0] + "&"
                           + hashlib.sha256(body.encode()).hexdigest() + "&").encode()).hexdigest()
    check("GWN: the request is signed as documented - sha256(&params&sha256(body)&) - and the secret is not sent",
          q["signature"][0] == want and "secretKey" not in q and "SEC" not in call[1], q)
    check("GWN: its access points", [d["name"] for d in r["devices"]] == ["GWN7660"] and json.loads(body)["networkId"] == 7, r["devices"])


def test_safety():
    check("a controller on the local network: its self-signed certificate is not checked (and that is said)",
          ctl._local("https://192.0.2.10:8043") and ctl._local("https://omada.local") and not ctl._local("https://api.ui.com"))
    r = ctl.fetch({"id": "c9", "kind": "omada", "url": "https://192.0.2.1", "sites": ["x"]}, "")
    check("no secret: nothing is asked, a reason is given", not r["ok"] and "no secret" in r["error"], r)
    check("the shape of an answer, never its values", ctl.shape({"mac": "aa", "n": [{"x": 1}]}) == {"mac": "str", "n": [{"x": "int"}]})


def main():
    test_omada()
    test_unifi()
    test_gwn()
    test_safety()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
