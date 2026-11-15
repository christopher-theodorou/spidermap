#!/usr/bin/env python3
"""Weather card, storm / heat warnings, the outdoor flag, and the seasonal decorations (0.9.18).

    python3 tests/test_weather.py        (the decoration dates need node; skipped without it)
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ZABBIX_TOKEN", "test")
import spidermap as sm  # noqa: E402
import spidermap_settings as st  # noqa: E402
import spidermap_weather as wx  # noqa: E402

LIM = {"rain": 8, "gust": 60, "heat": 38}
NOW = datetime.datetime(2026, 7, 15, 14, 20)


def forecast(hours=None, cur=None, offset=10800):
    """An Open-Meteo answer: 12 hours from 14:00 local; hours = {index: {field: value}}."""
    base = datetime.datetime(2026, 7, 15, 14, 0)
    h = {"time": [], "temperature_2m": [], "precipitation": [], "precipitation_probability": [], "weather_code": [],
         "wind_gusts_10m": []}
    for i in range(12):
        x = {"temperature_2m": 30, "precipitation": 0, "precipitation_probability": 0, "weather_code": 1,
             "wind_gusts_10m": 20, **(hours or {}).get(i, {})}
        h["time"].append((base + datetime.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"))
        for k in x:
            h[k].append(x[k])
    return {"utc_offset_seconds": offset, "hourly": h,
            "current": {"temperature_2m": 31, "precipitation": 0, "weather_code": 1, "wind_speed_10m": 10,
                        "wind_gusts_10m": 20, "relative_humidity_2m": 40, **(cur or {})}}


class Location(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(wx.parse_location("37.98, 23.73"), (37.98, 23.73))
        self.assertEqual(wx.parse_location("-33.9 151.2"), (-33.9, 151.2))
        self.assertIsNone(wx.parse_location("Athens, GR"))
        for bad in ("", "95, 20", "<script>", "x" * 81):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                wx.parse_location(bad)

    def test_setting_checked(self):
        self.assertEqual(st.validate_settings({"weather_location": "Ilioupoli, GR"})[1], [])
        self.assertEqual(st.validate_settings({"weather_location": ""})[1], [])
        self.assertTrue(st.validate_settings({"weather_location": "91, 200"})[1])
        self.assertTrue(st.validate_settings({"decorations": "always"})[1])
        self.assertEqual(st.validate_settings({"decorations": "holidays_gr", "weather_heat_c": 40})[1], [])


class SettingsTabs(unittest.TestCase):
    def test_every_schema_tab_is_in_the_dialog(self):
        # 0.9.18 added the "Weather & seasons" settings but not its tab, so nobody could reach them
        html = (ROOT / "spidermap.html").read_text(encoding="utf-8")
        tabs_js = html[html.index("const SM_TABS = "):html.index("];", html.index("const SM_TABS = "))]
        shown = set(re.findall(r'\["([^"]+)", "[^"]+"', tabs_js))
        needed = {f["tab"] for f in st.SETTINGS_SCHEMA if f.get("tab")}
        self.assertEqual(needed - shown, set())


class Warnings(unittest.TestCase):
    def test_calm(self):
        self.assertEqual(wx.evaluate(forecast(), LIM, NOW), {})

    def test_rain_gust_thunder(self):
        for hours, word in (({15 - 14: {"precipitation": 12}}, "rain 12 mm/h"), ({16 - 14: {"wind_gusts_10m": 75}}, "gusts 75 km/h"),
                            ({15 - 14: {"weather_code": 95}}, "thunderstorm")):
            w = wx.evaluate(forecast(hours), LIM, NOW)
            self.assertIn("storm", w)
            self.assertIn(word, w["storm"]["why"])
        w = wx.evaluate(forecast({1: {"precipitation": 12}, 2: {"precipitation": 9}}), LIM, NOW)
        self.assertEqual((w["storm"]["from"], w["storm"]["to"]), ("15:00", "17:00"))

    def test_only_two_hours_ahead(self):
        self.assertEqual(wx.evaluate(forecast({5: {"precipitation": 30, "weather_code": 95}}), LIM, NOW), {})

    def test_heat(self):
        w = wx.evaluate(forecast({1: {"temperature_2m": 39.6}}), LIM, NOW)
        self.assertEqual(w["heat"]["max"], 40)
        self.assertNotIn("storm", w)
        self.assertIn("heat", wx.evaluate(forecast(cur={"temperature_2m": 38}), LIM, NOW))


class FakeSession:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        a = self.answers[url] if url in self.answers else self.answers["*"]
        if isinstance(a, Exception):
            raise a
        r = type("R", (), {})()
        r.raise_for_status = lambda: None
        r.json = lambda: a
        return r


class Service(unittest.TestCase):
    def make(self, answers):
        self.events, self.sess = [], FakeSession(answers)
        return wx.Weather(lambda: self.sess, lambda kind, lvl, text, nid=None: {"kind": kind, "lvl": lvl, "text": text},
                          self.events.extend)

    def t_at(self, local, offset=10800):
        return (local - datetime.datetime(1970, 1, 1)).total_seconds() - offset

    def test_geocode_fetch_publish(self):
        geo = {"results": [{"latitude": 37.93, "longitude": 23.75, "name": "Ilioupoli", "country": "Greece",
                            "country_code": "GR"}]}
        w = self.make({wx.GEOCODE: geo, wx.FORECAST: forecast()})
        s = w.refresh("Ilioupoli, GR", LIM, self.t_at(NOW))
        self.assertEqual(s["place"], "Ilioupoli, Greece")
        self.assertEqual(s["temp"], 31)
        self.assertEqual(len(s["hours"]), 8)
        self.assertEqual(s["warn"], {})
        w.refresh("Ilioupoli, GR", LIM, self.t_at(NOW) + 60)            # not due: nothing fetched again
        self.assertEqual(len(self.sess.calls), 2)

    def test_warning_announced_once_then_cleared(self):
        w = self.make({wx.FORECAST: forecast({1: {"wind_gusts_10m": 80}})})
        t = self.t_at(NOW)
        w.refresh("37.93, 23.75", LIM, t)
        w.refresh("37.93, 23.75", LIM, t + 30)
        self.assertEqual([e["lvl"] for e in self.events], ["warn"])
        self.assertIn("outdoor links may drop", self.events[0]["text"])
        self.sess.answers[wx.FORECAST] = forecast()
        w.refresh("37.93, 23.75", LIM, t + wx.EVERY + 1)
        self.assertEqual([e["lvl"] for e in self.events], ["warn", "ok"])

    def test_failure_keeps_last_and_retries(self):
        w = self.make({wx.FORECAST: forecast()})
        t = self.t_at(NOW)
        w.refresh("37.93, 23.75", LIM, t)
        self.sess.answers[wx.FORECAST] = OSError("no route")
        s = w.refresh("37.93, 23.75", LIM, t + wx.EVERY + 1)
        self.assertEqual(s["temp"], 31)
        self.assertIn("no route", s["error"])
        s = w.refresh("37.93, 23.75", LIM, t + wx.STALE + 1)      # too old: still shown, no warnings
        self.assertEqual(s["warn"], {})

    def test_no_location_no_requests(self):
        w = self.make({"*": AssertionError("no request without a location")})
        self.assertIsNone(w.refresh("", LIM))
        self.assertEqual(self.sess.calls, [])


class OutdoorFlag(unittest.TestCase):
    def test_edits(self):
        tmp = tempfile.mkdtemp()
        try:
            dev = os.path.join(tmp, "devices.json")
            Path(dev).write_text(json.dumps({"devices": {"aa:bb:cc:dd:ee:ff": {"name": "EAP Garden"}}}))
            with patch.object(sm, "DEVICES_FILE", dev), patch.object(sm, "add_events") as ae, patch.object(sm, "audit"):
                sm._merge_edits({"aa:bb:cc:dd:ee:ff": {"outdoor": True, "by": "nikos"}})
                self.assertIs(json.loads(Path(dev).read_text())["devices"]["aa:bb:cc:dd:ee:ff"]["outdoor"], True)
                sm._merge_edits({"aa:bb:cc:dd:ee:ff": {"outdoor": False, "by": "nikos"}})
                self.assertIs(json.loads(Path(dev).read_text())["devices"]["aa:bb:cc:dd:ee:ff"]["outdoor"], False)
                sm._merge_edits({"aa:bb:cc:dd:ee:ff": {"outdoor": None, "by": "nikos"}})      # back to the guess
                self.assertNotIn("outdoor", json.loads(Path(dev).read_text())["devices"]["aa:bb:cc:dd:ee:ff"])
                texts = " ".join(c.args[0][0]["text"] for c in ae.call_args_list)
                self.assertIn("marked outdoor", texts)
                self.assertIn("marked indoor", texts)
        finally:
            shutil.rmtree(tmp)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Decorations(unittest.TestCase):
    """decorFor() and orthodoxEaster() from spidermap.html, run in node with fixed dates."""

    @classmethod
    def setUpClass(cls):
        html = (ROOT / "spidermap.html").read_text(encoding="utf-8")
        start = html.index("function orthodoxEaster(")
        end = html.index("const DECOR_PREVIEW", start)
        cls.js = html[start:end]

    def run_js(self, expr):
        code = self.js + f"\nconsole.log(JSON.stringify({expr}));"
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def at(self, iso, mode):
        y, mo, d, h, mi, s = (int(x) for x in re.split(r"[-T:]", iso))
        return self.run_js(f"decorFor(new Date({y}, {mo - 1}, {d}, {h}, {mi}, {s}), {json.dumps(mode)})")

    def test_easter(self):
        got = self.run_js("[2024, 2025, 2026, 2027, 2028].map(y => orthodoxEaster(y).toDateString())")
        self.assertEqual(got, ["Sun May 05 2024", "Sun Apr 20 2025", "Sun Apr 12 2026", "Sun May 02 2027", "Sun Apr 16 2028"])

    def test_calendar(self):
        G = "holidays_gr"
        cases = [("2026-12-20T10:00:00", G, "winter", "xmas"), ("2026-12-31T19:00:00", G, "winter", "nye"),
                 ("2027-01-01T00:00:30", G, "winter", "fireworks"), ("2027-01-01T01:00:00", G, "winter", "newyear"),
                 ("2027-01-01T03:00:00", G, "winter", "xmas"), ("2027-01-07T12:00:00", G, "winter", None),
                 ("2026-10-28T09:00:00", G, "", "gr"), ("2027-03-25T09:00:00", G, "", "gr"),
                 ("2026-04-12T09:00:00", G, "", "easter"), ("2026-04-13T09:00:00", G, "", "easter"),
                 ("2026-07-15T12:00:00", G, "summer", None), ("2026-10-28T09:00:00", "holidays", None, None),
                 ("2026-12-20T10:00:00", "seasons", "winter", None)]
        for iso, mode, season, holiday in cases:
            r = self.at(iso, mode)
            with self.subTest(iso=iso, mode=mode):
                if season is None and holiday is None:
                    self.assertIsNone(r)
                else:
                    self.assertEqual((r.get("season"), r.get("holiday")), (season, holiday))

    def test_off(self):
        self.assertIsNone(self.at("2026-12-25T12:00:00", "off"))
        self.assertIsNone(self.at("2026-12-25T12:00:00", None))


if __name__ == "__main__":
    r = unittest.main(verbosity=1, exit=False).result
    n = r.testsRun - len(r.skipped)
    print(f"\n{n - len(r.failures) - len(r.errors)}/{n} passed")
    sys.exit(0 if r.wasSuccessful() else 1)
