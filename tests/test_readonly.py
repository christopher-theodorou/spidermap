#!/usr/bin/env python3
"""Zabbix read-only mode and the standalone deployment (0.9.16).

    python3 tests/test_readonly.py        (the standalone web tests need php-cli; skipped without it)

Based on the behavioural tests in Nikolaos Karanikolas's prototype.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ZABBIX_TOKEN", "test")
import spidermap as sm  # noqa: E402
import spidermap_zabbix as at  # noqa: E402
import spidermap_standalone as st  # noqa: E402

WRITES = ("item.create", "item.update", "item.delete", "trigger.create", "history.push", "dashboard.create",
          "dashboard.update", "dashboard.delete", "map.create", "map.delete", "user.login", "host.massupdate",
          "host.getx", None)


class ReadOnlyMode(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"SPIDERMAP_ZABBIX_READONLY": "1"})
        self.env.start()
        os.environ.pop("SPIDERMAP_DEPLOYMENT", None)

    def tearDown(self):
        self.env.stop()

    def test_switch(self):
        self.assertTrue(at.readonly_mode())
        with patch.dict(os.environ, {"SPIDERMAP_ZABBIX_READONLY": "0"}):
            self.assertFalse(at.readonly_mode())
            with patch.dict(os.environ, {"SPIDERMAP_DEPLOYMENT": "standalone"}):
                self.assertTrue(at.readonly_mode())          # standalone is always read-only

    def test_writes_refused_before_anything_is_sent(self):
        api = at.Zabbix("https://invalid.test/api_jsonrpc.php", "fake")
        api.s = Mock()
        for method in WRITES:
            with self.subTest(method=method), self.assertRaises(at.ZabbixError):
                api.call(method, {})
        api.s.post.assert_not_called()

    def test_reads_go_through(self):
        api = at.Zabbix("https://invalid.test/api_jsonrpc.php", "fake")
        api.s = Mock()
        api.s.post.return_value.json.return_value = {"result": []}
        for method in at.READ_METHODS:
            self.assertEqual(api.call(method, {}), [])
        self.assertEqual(api.s.post.call_count, len(at.READ_METHODS))

    def test_full_mode_unchanged(self):
        with patch.dict(os.environ, {"SPIDERMAP_ZABBIX_READONLY": "0"}):
            api = at.Zabbix("https://invalid.test/api_jsonrpc.php", "fake")
            api.s = Mock()
            api.s.post.return_value.json.return_value = {"result": {"itemids": ["1"]}}
            self.assertEqual(api.call("item.create", {}), {"itemids": ["1"]})

    def test_no_event_items_no_push_no_sender(self):
        api = Mock()
        api.call.side_effect = AssertionError("unexpected Zabbix API call")
        with patch.object(sm.subprocess, "run", side_effect=AssertionError("unexpected zabbix_sender")):
            self.assertEqual(sm.ensure_event_items(api, {"hostid": "1"}), {})
            sm.push_events(api, [{"_role": "zabbix", "hostid": "1", "host": "z"}], {"new": ["x"], "changed": []}, [])
        api.call.assert_not_called()

    def test_speedtest_kept_locally(self):
        api = Mock()
        api.call.side_effect = AssertionError("unexpected Zabbix API call")
        state, res = {}, {"down": 500e6, "up": 200e6, "ping": 7, "server": "test"}
        with tempfile.TemporaryDirectory() as d, \
                patch.object(sm, "SPEED_FILE", str(Path(d) / "speed.json")), \
                patch.object(sm, "setting", return_value="cloudflare"), \
                patch.object(sm, "_speedtest_cloudflare", return_value=res), \
                patch.object(sm, "add_events"):
            sm.run_speedtest(api, {"hostid": "1"}, state)
            self.assertEqual(sm.load_json(sm.SPEED_FILE, [])[0]["down"], res["down"])
        self.assertEqual(state["error"], "")
        api.call.assert_not_called()

    def test_old_dashboard_not_deleted(self):
        api = Mock()
        api.call.side_effect = AssertionError("unexpected Zabbix API call")
        with tempfile.TemporaryDirectory() as d:
            sm.migrate_old_install(api, d)
        api.call.assert_not_called()

    def test_setup_creates_nothing_in_zabbix(self):
        api = Mock()
        api.call.side_effect = AssertionError("unexpected Zabbix API call")
        api.version.return_value = "7.4.0"
        with tempfile.TemporaryDirectory() as d, \
                patch.object(sm, "webdir", return_value=d), patch.object(sm, "DEVICES_FILE", os.path.join(d, "dev.json")), \
                patch.object(sm, "install_icons"), patch.object(sm, "install_page"), \
                patch.object(sm, "fix_ownership"), patch.object(sm, "install_service") as svc, \
                patch("sys.stdout"):
            sm.setup(api)
        api.call.assert_not_called()
        api.version.assert_called_once()
        svc.assert_called_once()

    def test_uninstall_zabbix_only_lists(self):
        api = Mock()
        api.call.side_effect = lambda m, p=None: [] if m.endswith(".get") else self.fail(m)
        self.assertEqual(sm.uninstall_zabbix(api, dry=True), [])


class Standalone(unittest.TestCase):
    def test_page_wording_and_greek(self):
        page = st.standalone_page((ROOT / "spidermap.html").read_text(encoding="utf-8"))
        for gone in ("Zabbix Super admins", "Zabbix admins", "Zabbix users", "Log in to Zabbix", "Χρήστες Zabbix"):
            self.assertNotIn(gone, page)
        self.assertIn('"SpiderMap viewers": "Θεατές SpiderMap"', page)
        self.assertIn('href="spidermap.php" target="_top"', page)

    def test_gateway_has_no_zabbix_login(self):
        code = st.standalone_gateway()
        for text in ("function api_call(", "user.checkAuthentication", "HTTP_X_AUTH_USER"):
            self.assertNotIn(text, code)
        for text in ("$_SERVER['REMOTE_USER']", "/run/spidermap-auth/roles.json", "SPIDERMAP_PUBLIC_ORIGIN"):
            self.assertIn(text, code)

    def test_auth_files(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                st.check_auth_files(d)
            Path(d, "users.htpasswd").write_text("nikos:$2y$05$x\n")
            with self.assertRaises(SystemExit):
                st.check_auth_files(d)
            Path(d, "roles.json").write_text('{"nikos": "root"}')
            with self.assertRaises(SystemExit):
                st.check_auth_files(d)
            Path(d, "roles.json").write_text('{"nikos": "admin", "ghost": "viewer"}')
            self.assertEqual(st.check_auth_files(d)["nikos"], "admin")

    def test_web_folder(self):
        with patch.dict(os.environ, {"SPIDERMAP_DEPLOYMENT": "standalone"}):
            self.assertEqual(sm.webdir(), "/var/www/html")


class Packaging(unittest.TestCase):
    def test_image_gets_program_files_only(self):
        df = (ROOT / "standalone" / "Dockerfile").read_text()
        copies = [l for l in df.splitlines() if l.startswith("COPY")]
        self.assertFalse(any(l.split()[1] == "." for l in copies), "COPY . would take settings into the image")
        for line in copies:
            for name in ("zabbix.env", "devices.json", "secrets.json", "auth", ".env"):
                self.assertNotIn(name, line.split()[1:-1])

    def test_settings_outside_the_folder(self):
        compose = (ROOT / "standalone" / "compose.yaml").read_text()
        self.assertIn("/etc/spidermap-standalone", compose)
        self.assertNotIn("./auth", compose)

    def test_no_dot_files_needed(self):
        # uploaded with drag and drop, hidden files get lost: nothing outside .github may need one
        dots = [p for p in ROOT.rglob(".*") if ".git" not in p.parts and ".github" not in p.parts
                and p.name not in (".gitignore", ".spidermap.lock")
                and "__pycache__" not in p.parts]
        self.assertEqual(dots, [])

    def test_menu_saves_and_restarts_before_cleanup(self):
        sh = (ROOT / "install.sh").read_text()
        body = sh[sh.index("menu_zabbix() {"):sh.index("\nmenu_proxmox()")]
        first_delete = body.index("--uninstall-zabbix \\")
        self.assertLess(body.index("_setenv"), first_delete)
        self.assertLess(body.index("restart_service"), first_delete)


@unittest.skipUnless(shutil.which("php"), "php-cli not installed")
class StandaloneWeb(unittest.TestCase):
    """spidermap.php as the standalone web server runs it. Apache's password check is simulated:
    a prepend file sets REMOTE_USER the way Apache does after a correct password."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="spidermap-sa-")
        web, data, inbox, auth = (os.path.join(cls.tmp, x) for x in ("web", "data", "inbox", "auth"))
        for d in (web, data, os.path.join(inbox, "sess"), auth):
            os.makedirs(d)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        code = (st.standalone_gateway().replace("'/var/lib/spidermap/www'", repr(data))
                .replace("'/var/lib/spidermap/inbox'", repr(inbox))
                .replace("/run/spidermap-auth/roles.json", os.path.join(auth, "roles.json")))
        Path(web, "spidermap.php").write_text(code)
        Path(auth, "roles.json").write_text('{"nikos": "admin", "eva": "editor", "obs": "viewer", "odd": "boss"}')
        # Apache sets REMOTE_USER after the password check; here a test-only header plays Apache's part
        Path(cls.tmp, "prepend.php").write_text(
            "<?php if (isset($_SERVER['HTTP_X_TEST_APACHE_USER'])) "
            "$_SERVER['REMOTE_USER'] = $_SERVER['HTTP_X_TEST_APACHE_USER'];")
        shutil.copy(ROOT / "spidermap.html", os.path.join(data, "spidermap.html"))
        Path(data, "spidermap.json").write_text(json.dumps({"nodes": [{"id": "inet"}]}))
        Path(data, "spidermap_settings.json").write_text(json.dumps({"schema": [], "values": {}}))
        Path(inbox, "edits.json").write_text("{}")
        env = dict(os.environ, SPIDERMAP_PUBLIC_ORIGIN=cls.origin)
        cls.srv = subprocess.Popen(["php", "-d", "auto_prepend_file=" + os.path.join(cls.tmp, "prepend.php"),
                                    "-S", f"127.0.0.1:{cls.port}", "-t", web], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.terminate()
        cls.srv.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, path="", user=None, body=None, headers=None):
        h = dict(headers or {})
        if user:
            h["X-Test-Apache-User"] = user
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(f"{self.origin}/spidermap.php{path}", data=data, headers=h,
                                   method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def test_no_user_no_access(self):
        self.assertEqual(self.req("?data=me")[0], 401)
        self.assertEqual(self.req("?data=me", headers={"X-Auth-User": "nikos"})[0], 401)   # a header is no login

    def test_unknown_role(self):
        self.assertEqual(self.req("?data=me", user="odd")[0], 403)
        self.assertEqual(self.req("?data=me", user="nobody")[0], 403)

    def test_roles(self):
        for user, can in (("obs", set()), ("eva", {"edit", "speedtest"}), ("nikos", {"edit", "speedtest", "settings"})):
            code, text = self.req("?data=me", user=user)
            self.assertEqual(code, 200, text)
            me = json.loads(text)
            self.assertEqual({k for k, v in me["can"].items() if v}, can, user)

    def test_changes_need_role_and_origin(self):
        good = {"X-SpiderMap": "1", "Origin": self.origin}
        self.assertEqual(self.req(user="obs", body={"action": "scan"}, headers=good)[0], 403)
        self.assertEqual(self.req(user="nikos", body={"action": "scan"})[0], 403)                 # no X-SpiderMap
        self.assertEqual(self.req(user="nikos", body={"action": "scan"},
                                  headers={**good, "Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(self.req(user="nikos", body={"action": "scan"},                          # other port
                                  headers={**good, "Origin": "http://127.0.0.1:1"})[0], 403)
        code, text = self.req(user="nikos", body={"action": "scan"}, headers=good)
        self.assertEqual(code, 200, text)

    def test_page_for_a_viewer(self):
        code, text = self.req("", user="obs")
        self.assertEqual(code, 200)
        self.assertIn("SpiderMap", text)
        self.assertEqual(self.req("?data=map", user="obs")[0], 200)

    def test_path_traversal(self):
        self.assertEqual(self.req("?data=../../etc/passwd", user="nikos")[0], 400)


if __name__ == "__main__":
    r = unittest.main(verbosity=1, exit=False).result
    n = r.testsRun - len(r.skipped)
    print(f"\n{n - len(r.failures) - len(r.errors)}/{n} passed")
    sys.exit(0 if r.wasSuccessful() else 1)
