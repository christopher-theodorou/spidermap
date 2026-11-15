"""Weather for the Overview card, and storm / heat warnings for outdoor devices.

Part of SpiderMap (see spidermap.py and docs/ARCHITECTURE.md). The service fetches it, never the browsers:
one request every 30 minutes to Open-Meteo (free, no account, no key), only while a location is set in
Settings > Weather & seasons. Warnings stay in SpiderMap (Activity, the card, the map) and are never sent to Zabbix.
"""
import datetime
import re
import time

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
EVERY = 30 * 60            # fetch every 30 minutes
RETRY = 10 * 60            # after a failure
STALE = 3 * 3600           # older than this: shown as out of date, no warnings
AHEAD_H = 2                # warnings look this many hours ahead
THUNDER = {95, 96, 99}     # WMO weather codes: thunderstorm (with hail)

# WMO weather code -> (text, Tabler icon)
CODES = [((0,), "Clear", "sun"), ((1,), "Mainly clear", "sun"), ((2,), "Partly cloudy", "cloud"),
         ((3,), "Overcast", "cloud"), ((45, 48), "Fog", "mist"), ((51, 53, 55, 56, 57), "Drizzle", "cloud-rain"),
         ((61, 63, 66), "Rain", "cloud-rain"), ((65, 67), "Heavy rain", "cloud-rain"),
         ((71, 73, 75, 77), "Snow", "snowflake"), ((80, 81), "Rain showers", "cloud-rain"),
         ((82,), "Violent showers", "cloud-storm"), ((85, 86), "Snow showers", "snowflake"),
         ((95,), "Thunderstorm", "cloud-storm"), ((96, 99), "Thunderstorm with hail", "cloud-storm")]


def describe(code):
    for codes, text, icon in CODES:
        if code in codes:
            return text, icon
    return "", "cloud"


def parse_location(text):
    """'37.98, 23.73' -> (37.98, 23.73); a place name -> None (geocode it); nonsense -> ValueError."""
    s = (text or "").strip()
    m = re.fullmatch(r"(-?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)", s)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
        raise ValueError("coordinates out of range")
    if not s or len(s) > 80 or re.search(r"[<>{}\\]", s):
        raise ValueError("not a place name")
    return None


def geocode(session, name):
    r = session.get(GEOCODE, params={"name": name.split(",")[0].strip(), "count": 5, "language": "en",
                                     "format": "json"}, timeout=15)
    r.raise_for_status()
    res = r.json().get("results") or []
    want = [w.strip().lower() for w in name.split(",")[1:]]   # "Athens, GR" / "Athens, Greece"
    for x in res:
        if not want or any(w in (x.get("country_code", "").lower(), x.get("country", "").lower()) for w in want):
            return {"lat": x["latitude"], "lon": x["longitude"],
                    "name": ", ".join(v for v in (x.get("name"), x.get("country")) if v)}
    raise ValueError(f"place not found: {name}")


def fetch(session, lat, lon):
    r = session.get(FORECAST, params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_hours": 12, "wind_speed_unit": "kmh",
        "current": "temperature_2m,precipitation,weather_code,wind_speed_10m,wind_gusts_10m,relative_humidity_2m",
        "hourly": "temperature_2m,precipitation,precipitation_probability,weather_code,wind_gusts_10m"}, timeout=15)
    r.raise_for_status()
    return r.json()


def _hours(raw):
    h = raw.get("hourly") or {}
    out = []
    for i, t in enumerate(h.get("time") or []):
        def g(k, i=i):
            v = (h.get(k) or [None] * (i + 1))
            return v[i] if i < len(v) else None
        out.append({"t": t, "temp": g("temperature_2m"), "rain": g("precipitation"), "prob": g("precipitation_probability"),
                    "code": g("weather_code"), "gust": g("wind_gusts_10m")})
    return out


def evaluate(raw, limits, now_local):
    """The warnings for the next AHEAD_H hours. limits: rain mm/h, gust km/h, heat °C.
    now_local: naive datetime in the location's time (Open-Meteo's times are local, 'YYYY-MM-DDTHH:MM')."""
    cur = raw.get("current") or {}
    soon = []
    for h in _hours(raw):
        try:
            ht = datetime.datetime.strptime(h["t"], "%Y-%m-%dT%H:%M")
        except (TypeError, ValueError):
            continue
        if now_local - datetime.timedelta(hours=1) < ht <= now_local + datetime.timedelta(hours=AHEAD_H):
            soon.append((ht, h))
    warn = {}
    storm = [(ht, h) for ht, h in soon
             if (h["rain"] or 0) >= limits["rain"] or (h["gust"] or 0) >= limits["gust"] or h["code"] in THUNDER]
    if (cur.get("wind_gusts_10m") or 0) >= limits["gust"] or cur.get("weather_code") in THUNDER:
        storm.insert(0, (now_local, {"rain": cur.get("precipitation"), "gust": cur.get("wind_gusts_10m"),
                                     "code": cur.get("weather_code")}))
    if storm:
        why = []
        if any(h["code"] in THUNDER for _, h in storm):
            why.append("thunderstorm")
        r = max((h["rain"] or 0) for _, h in storm)
        if r >= limits["rain"]:
            why.append(f"rain {r:.0f} mm/h")
        g = max((h["gust"] or 0) for _, h in storm)
        if g >= limits["gust"]:
            why.append(f"gusts {g:.0f} km/h")
        warn["storm"] = {"from": min(ht for ht, _ in storm).strftime("%H:%M"),
                         "to": (max(ht for ht, _ in storm) + datetime.timedelta(hours=1)).strftime("%H:%M"),
                         "why": ", ".join(why) or "storm"}
    temps = [v for v in [cur.get("temperature_2m")] + [h["temp"] for _, h in soon] if v is not None]
    if temps and max(temps) >= limits["heat"]:
        warn["heat"] = {"max": round(max(temps)), "now": cur.get("temperature_2m")}
    return warn


def publish(raw, place, warn, fetched):
    cur = raw.get("current") or {}
    text, icon = describe(cur.get("weather_code"))
    hours = []
    for h in _hours(raw)[:8]:
        t, i = describe(h["code"])
        hours.append({"t": (h["t"] or "")[11:16], "temp": h["temp"], "rain": h["rain"], "prob": h["prob"],
                      "gust": h["gust"], "icon": i, "text": t})
    return {"place": place, "fetched": fetched, "temp": cur.get("temperature_2m"), "humidity": cur.get("relative_humidity_2m"),
            "rain": cur.get("precipitation"), "wind": cur.get("wind_speed_10m"), "gust": cur.get("wind_gusts_10m"),
            "code": cur.get("weather_code"), "text": text, "icon": icon, "hours": hours, "warn": warn}


class Weather:
    """Kept by the service: refresh() every loop; it fetches only when due. state: what the page gets."""

    def __init__(self, session_factory, ev, add_events):
        self.session_factory, self.ev, self.add_events = session_factory, ev, add_events
        self.next_t, self.raw, self.fetched, self.loc_key, self.place = 0, None, 0, None, None
        self.active = {}            # warnings already announced in Activity
        self.place_name = ""
        self.state = None
        self.error = ""

    def refresh(self, location, limits, t=None):
        t = t or time.time()
        location = (location or "").strip()
        if not location:
            self.state, self.raw, self.loc_key, self.active = None, None, None, {}
            return None
        if location != self.loc_key:
            self.next_t, self.raw, self.place, self.loc_key = 0, None, None, location
        if t >= self.next_t:
            try:
                s = self.session_factory()
                pos = parse_location(location)
                if pos is None:
                    if not self.place or self.place.get("q") != location:
                        g = geocode(s, location)
                        self.place = {"q": location, **g}
                    lat, lon, name = self.place["lat"], self.place["lon"], self.place["name"]
                else:
                    lat, lon, name = pos[0], pos[1], f"{pos[0]:.2f}, {pos[1]:.2f}"
                self.raw, self.fetched, self.error = fetch(s, lat, lon), t, ""
                self.place_name = name
                self.next_t = t + EVERY
            except Exception as e:  # noqa: BLE001 - weather never stops the service
                self.error, self.next_t = str(e)[:160], t + RETRY
        if not self.raw:
            self.state = {"error": self.error or "not fetched yet", "place": location}
            return self.state
        stale = t - self.fetched > STALE
        off = (self.raw.get("utc_offset_seconds") or 0)
        now_local = datetime.datetime.fromtimestamp(t + off, datetime.timezone.utc).replace(tzinfo=None)
        warn = {} if stale else evaluate(self.raw, limits, now_local)
        self._announce(warn)
        self.state = publish(self.raw, self.place_name or location, warn, self.fetched)
        if stale or self.error:
            self.state["error"] = self.error or "out of date"
        return self.state

    def _announce(self, warn):
        evs = []
        for k in ("storm", "heat"):
            w, was = warn.get(k), self.active.get(k)
            if w and not was:
                evs.append(self.ev("weather", "warn",
                                   f"Storm expected {w['from']}–{w['to']} ({w['why']}): outdoor links may drop" if k == "storm"
                                   else f"Heat: up to {w['max']} °C - outdoor devices and links may overheat or drop", "inet"))
            elif was and not w:
                evs.append(self.ev("weather", "ok", "Storm warning over" if k == "storm" else "Heat warning over", "inet"))
            if w:
                self.active[k] = w
            else:
                self.active.pop(k, None)
        if evs:
            self.add_events(evs)
