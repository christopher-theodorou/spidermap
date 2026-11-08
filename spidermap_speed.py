"""Speed tests: Ookla (CLI), Cloudflare and Fast.com (built in).

Part of SpiderMap (see spidermap.py and docs/ARCHITECTURE.md)."""
import json
import os
import re
import shutil
import subprocess
import time

import requests

from spidermap import OOKLA, STATE_DIR, load_json, now, ping_stats, save_json  # noqa: E402 - spidermap.py loads this module at its end


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
