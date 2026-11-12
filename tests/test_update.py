#!/usr/bin/env python3
"""`spidermap update` (spidermap_update.py) - offline: GitHub, the installer and systemd are replaced by fakes.

    python3 tests/test_update.py
"""
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import spidermap_update as up  # noqa: E402

FILES = ("spidermap.py", "spidermap_cli.py", "install.sh", "spidermap.html", "CHANGELOG.md")


def args(**kw):
    return types.SimpleNamespace(**{"check": False, "yes": False, "main": False, "reinstall": False, **kw})


def make_tree(d, version, extra=None):
    os.makedirs(d, exist_ok=True)
    for f in FILES:
        Path(d, f).write_text("#!/bin/sh\ntrue\n" if f == "install.sh" else "")
    Path(d, "spidermap.py").write_text(f'__version__ = "{version}"\n')
    Path(d, "CHANGELOG.md").write_text("# Changelog\n\n## [0.9.17] - x\n- seventeen\n\n## [0.9.16] - x\n- sixteen\n\n"
                                       "## [0.9.15] - x\n- fifteen\n")
    for name, text in (extra or {}).items():
        Path(d, name).write_text(text)


def make_zip(path, version, top="spidermap-0.9.16", extra=None, raw=None):
    with zipfile.ZipFile(path, "w") as z:
        if raw:
            for name, text in raw.items():
                z.writestr(name, text)
            return
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, version, extra)
            for f in os.listdir(d):
                z.write(os.path.join(d, f), f"{top}/{f}")


class Versions(unittest.TestCase):
    def test_compare(self):
        self.assertEqual(up.vtuple("v0.9.16"), (0, 9, 16))
        self.assertIsNone(up.vtuple("latest"))
        self.assertTrue(up.newer("0.9.16", "0.9.15"))
        self.assertTrue(up.newer("0.10.0", "0.9.16"))       # not compared as text
        self.assertFalse(up.newer("0.9.14", "0.9.15"))
        self.assertFalse(up.newer("0.9.15", "0.9.15"))

    def test_changelog_between(self):
        text = Path(tempfile.mkdtemp(), "x")
        make_tree(text.parent, "0.9.17")
        notes = up.changelog_between((text.parent / "CHANGELOG.md").read_text(), "0.9.15", "0.9.17")
        self.assertIn("sixteen", notes)
        self.assertIn("seventeen", notes)
        self.assertNotIn("fifteen", notes)


class Download(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_good_zip(self):
        z = os.path.join(self.d, "a.zip")
        make_zip(z, "0.9.16")
        src = up.safe_extract(z, os.path.join(self.d, "x"))
        self.assertEqual(up.verify_tree(src, "0.9.16"), "0.9.16")

    def test_unsafe_paths(self):
        for name in ("../evil.py", "/etc/evil", "a/../../evil"):
            z = os.path.join(self.d, "b.zip")
            make_zip(z, "0.9.16", raw={name: "x"})
            with self.subTest(name=name), self.assertRaises(up.UpdateError):
                up.safe_extract(z, os.path.join(self.d, "y"))

    def test_settings_or_wrong_version_refused(self):
        for extra, version in (({"zabbix.env": "export ZABBIX_TOKEN=x"}, "0.9.16"), ({}, "0.9.99")):
            z = os.path.join(self.d, "c.zip")
            make_zip(z, version, extra=extra)
            src = up.safe_extract(z, tempfile.mkdtemp(dir=self.d))
            with self.assertRaises(up.UpdateError):
                up.verify_tree(src, "0.9.16")

    def test_broken_python_refused(self):
        z = os.path.join(self.d, "d.zip")
        make_zip(z, "0.9.16", extra={"spidermap_x.py": "def (:\n"})
        src = up.safe_extract(z, os.path.join(self.d, "x"))
        with self.assertRaises(up.UpdateError):
            up.verify_tree(src, "0.9.16")


class Asking(unittest.TestCase):
    def test_non_interactive_needs_yes(self):
        with patch.object(sys, "stdin", io.StringIO("y\n")), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertFalse(up.confirm_upgrade("0.9.15", "0.9.16"))
        self.assertIn("--yes", out.getvalue())
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(up.confirm_upgrade("0.9.15", "0.9.16", assume_yes=True))

    def test_answers(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        for answer, want in (("y\n", True), ("YES\n", True), ("\n", False), ("n\n", False), ("maybe\n", False)):
            with patch.object(sys, "stdin", TTY(answer)), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(up.confirm_upgrade("0.9.15", "0.9.16"), want, answer)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.here = os.path.join(self.d, "spidermap-main")
        make_tree(self.here, "0.9.15")
        os.symlink("/etc/spidermap/zabbix.env", os.path.join(self.here, "zabbix.env"))
        self.zip = os.path.join(self.d, "release.zip")
        make_zip(self.zip, "0.9.16")
        self.calls = []
        self.fail_install = False
        rel = {"version": "0.9.16", "tag": "v0.9.16", "source": "release v0.9.16", "zip": "https://x/y.zip"}
        self.p = [patch.object(up, "latest_release", return_value=rel),
                  patch.object(up, "download", side_effect=lambda url, dest: shutil.copy(self.zip, dest)),
                  patch.object(up, "service", side_effect=lambda a: self.calls.append(a)),
                  patch.object(up, "run_installer", side_effect=self.installer),
                  patch.object(up, "BACKUP_ROOT", os.path.join(self.d, "backups"))]
        for p in self.p:
            p.start()

    def installer(self, here):
        self.calls.append("install")
        return 1 if self.fail_install else 0

    def tearDown(self):
        for p in self.p:
            p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def run_update(self, **kw):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = up.update(args(**kw), here=self.here, conf_dir=self.d + "/none", state_dir=self.d + "/none")
        return rc, out.getvalue()

    def test_check_changes_nothing(self):
        rc, out = self.run_update(check=True)
        self.assertEqual(rc, 0)
        self.assertIn("Available: 0.9.16", out)
        up.download.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertEqual(up.installed_version(self.here), "0.9.15")

    def test_cancel_changes_nothing(self):
        rc, out = self.run_update()                          # no terminal and no --yes
        self.assertIn("Update cancelled. No changes made.", out)
        up.download.assert_not_called()
        self.assertEqual(up.installed_version(self.here), "0.9.15")

    def test_upgrade(self):
        rc, out = self.run_update(yes=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(up.installed_version(self.here), "0.9.16")
        self.assertEqual(self.calls, ["stop", "install", "start"])
        self.assertTrue(os.path.islink(os.path.join(self.here, "zabbix.env")))   # settings untouched
        self.assertIn("Updated 0.9.15 -> 0.9.16", out)
        self.assertIn("sixteen", out)
        self.assertTrue(any(n.startswith("code-0.9.15-") for n in os.listdir(os.path.join(self.d, "backups"))))

    def test_rollback(self):
        self.fail_install = True
        with self.assertRaises(up.UpdateError):
            self.run_update(yes=True)
        self.assertEqual(up.installed_version(self.here), "0.9.15")
        self.assertEqual(self.calls[0], "stop")
        self.assertEqual(self.calls[-1], "start")             # the service is running again

    def test_up_to_date_and_older_release(self):
        make_tree(self.here, "0.9.16")
        rc, out = self.run_update(yes=True)
        self.assertIn("Already up to date", out)
        make_tree(self.here, "0.9.17")
        rc, out = self.run_update(yes=True)
        self.assertIn("newer than the latest release", out)
        up.download.assert_not_called()

    def test_standalone_refused(self):
        with patch.dict(os.environ, {"SPIDERMAP_DEPLOYMENT": "standalone"}), self.assertRaises(up.UpdateError):
            self.run_update(yes=True)

    def test_backups_pruned(self):
        root = os.path.join(self.d, "b")
        os.makedirs(root)
        for i in range(8):
            os.makedirs(os.path.join(root, f"code-0.9.{i}"))
            os.utime(os.path.join(root, f"code-0.9.{i}"), (i, i))
        up.prune_backups(root, keep=5)
        self.assertEqual(sorted(os.listdir(root)), [f"code-0.9.{i}" for i in range(3, 8)])


if __name__ == "__main__":
    r = unittest.main(verbosity=1, exit=False).result
    print(f"\n{r.testsRun - len(r.failures) - len(r.errors)}/{r.testsRun} passed")
    sys.exit(0 if r.wasSuccessful() else 1)
