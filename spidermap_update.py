"""`spidermap update`: find a newer SpiderMap on GitHub, ask, then download, check, back up, install - and roll
back by itself if anything fails.

    sudo spidermap update            Installed 0.9.15 / Available 0.9.16 / Upgrade to 0.9.16? [y/N]
    sudo spidermap update --check    only says whether there is a newer version (changes nothing)
    sudo spidermap update --yes      no question (for scripts and automation)
    sudo spidermap update --main     the latest code on main, before it is a release (for testing)
    sudo spidermap update --reinstall    re-run the installer on the files already here (the old behaviour)

Default: the latest GitHub *release* - only what the author published. A git checkout keeps using git.
Standard library only (urllib), so it works even when spidermap.py itself cannot load.
"""
import datetime
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("SPIDERMAP_UPDATE_REPO", "christopher-theodorou/spidermap")   # owner/name on GitHub
BACKUP_ROOT = "/var/backups/spidermap"
MAX_ZIP = 60 * 1024 * 1024                        # a SpiderMap zip is ~2 MB; anything huge is not ours
REQUIRED = ("spidermap.py", "spidermap_cli.py", "install.sh", "spidermap.html")
NEVER_IN_ZIP = ("zabbix.env", "devices.json", "secrets.json")   # settings never come from a download
TIMEOUT = 30
VERSION_RX = re.compile(r'^__version__ = "([0-9]+(?:\.[0-9]+){1,3})"', re.M)


class UpdateError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #
def vtuple(v):
    """'v0.9.16' / '0.9.16' -> (0, 9, 16). Anything else -> None."""
    m = re.fullmatch(r"v?([0-9]+(?:\.[0-9]+){1,3})", (v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def newer(available, current):
    a, c = vtuple(available), vtuple(current)
    return bool(a and c and a > c)


def version_in(text):
    m = VERSION_RX.search(text or "")
    return m.group(1) if m else None


def installed_version(here=HERE):
    with open(os.path.join(here, "spidermap.py"), encoding="utf-8") as f:
        v = version_in(f.read())
    if not v:
        raise UpdateError(f"no version found in {here}/spidermap.py")
    return v


# --------------------------------------------------------------------------- #
# GitHub (https only; no API, so no rate limit and no token)
# --------------------------------------------------------------------------- #
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _open(url, redirect=True):
    if not url.startswith("https://"):
        raise UpdateError(f"refusing a non-https address: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "SpiderMap-updater"})
    opener = urllib.request.build_opener() if redirect else urllib.request.build_opener(_NoRedirect)
    return opener.open(req, timeout=TIMEOUT)


def latest_release(repo=REPO):
    """The tag of the latest published release, from the /releases/latest redirect."""
    url = f"https://github.com/{repo}/releases/latest"
    try:
        r = _open(url, redirect=False)
        loc = r.geturl()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "") if 300 <= e.code < 400 else ""
        if not loc:
            raise UpdateError(f"GitHub answered {e.code} for {url}")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"cannot reach GitHub ({getattr(e, 'reason', e)}) - is there internet on this server?")
    m = re.search(r"/releases/tag/([^/?#]+)$", loc or "")
    if not m or not vtuple(m.group(1)):
        raise UpdateError(f"no published release found on github.com/{repo}")
    tag = m.group(1)
    return {"version": tag.lstrip("v"), "tag": tag, "source": f"release {tag}",
            "zip": f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"}


def latest_main(repo=REPO):
    url = f"https://raw.githubusercontent.com/{repo}/main/spidermap.py"
    try:
        v = version_in(_open(url).read(2_000_000).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"cannot reach GitHub ({getattr(e, 'reason', e)})")
    if not v:
        raise UpdateError("no version found in spidermap.py on main")
    return {"version": v, "tag": "main", "source": "main (not a release yet)",
            "zip": f"https://github.com/{repo}/archive/refs/heads/main.zip"}


def download(url, dest):
    try:
        r = _open(url)
        size = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ZIP:
                    raise UpdateError("the download is far larger than SpiderMap - stopped")
                f.write(chunk)
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"download failed ({getattr(e, 'reason', e)})")


# --------------------------------------------------------------------------- #
# Checking what was downloaded
# --------------------------------------------------------------------------- #
def safe_extract(zip_path, dest):
    """Unzip, refusing absolute paths, '..', and links. Returns the one top folder."""
    with zipfile.ZipFile(zip_path) as z:
        tops = set()
        root = os.path.realpath(dest)
        for info in z.infolist():
            name = info.filename
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                raise UpdateError(f"unsafe path in the zip: {name}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError(f"link in the zip: {name}")
            target = os.path.realpath(os.path.join(dest, name))
            if not (target == root or target.startswith(root + os.sep)):
                raise UpdateError(f"unsafe path in the zip: {name}")
            tops.add(name.split("/", 1)[0])
        if len(tops) != 1:
            raise UpdateError("the zip does not have one SpiderMap folder")
        z.extractall(dest)
    return os.path.join(dest, tops.pop())


def verify_tree(src, expected):
    """The files are all there, compile, carry the expected version and no settings."""
    missing = [f for f in REQUIRED if not os.path.isfile(os.path.join(src, f))]
    if missing:
        raise UpdateError(f"files missing from the download: {', '.join(missing)}")
    found = [f for f in NEVER_IN_ZIP if os.path.lexists(os.path.join(src, f))]
    if found:
        raise UpdateError(f"the download contains settings ({', '.join(found)}) - refused")
    got = installed_version(src)
    if got != expected:
        raise UpdateError(f"the download says {got}, expected {expected}")
    for f in sorted(os.listdir(src)):
        if f.endswith(".py"):
            r = subprocess.run([sys.executable, "-c", "import sys; compile(open(sys.argv[1], encoding='utf-8').read(),"
                                " sys.argv[1], 'exec')", os.path.join(src, f)], capture_output=True, text=True)
            if r.returncode != 0:
                raise UpdateError(f"{f} does not compile: {r.stderr.strip().splitlines()[-1:]}")
    r = subprocess.run(["bash", "-n", os.path.join(src, "install.sh")], capture_output=True, text=True)
    if r.returncode != 0:
        raise UpdateError(f"install.sh has a syntax error: {r.stderr.strip()}")
    return got


def changelog_between(text, current, new):
    """The CHANGELOG sections after `current` up to and including `new` (newest first)."""
    out, keep = [], False
    for line in (text or "").splitlines():
        m = re.match(r"^## \[([0-9.]+)\]", line)
        if m:
            v = vtuple(m.group(1))
            keep = bool(v and vtuple(current) < v <= vtuple(new))
        if keep:
            out.append(line)
    return "\n".join(out).strip()


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #
def confirm_upgrade(current, available, assume_yes=False, source=""):
    print(f"Installed: {current}\nAvailable: {available}" + (f"   ({source})" if source else ""))
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Non-interactive session: use --yes to upgrade.")
        return False
    try:
        answer = input(f"Upgrade to {available}? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


# --------------------------------------------------------------------------- #
# Backup, install, roll back
# --------------------------------------------------------------------------- #
def make_backup(current, here, conf_dir, state_dir, root=None):
    root = root or BACKUP_ROOT
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    code = os.path.join(root, f"code-{current}-{stamp}")
    shutil.copytree(here, code, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    data = os.path.join(root, f"settings-{current}-{stamp}.tar.gz")
    old = os.umask(0o077)
    try:
        with tarfile.open(data, "w:gz") as t:
            for p in (conf_dir, state_dir):
                if p and os.path.exists(p):
                    t.add(p)
    finally:
        os.umask(old)
    prune_backups(root)
    return code, data


def prune_backups(root, keep=5):
    """Only the last `keep` backups of each kind stay."""
    for prefix in ("code-", "settings-"):
        found = sorted((os.path.getmtime(os.path.join(root, n)), n) for n in os.listdir(root) if n.startswith(prefix))
        for _, n in found[:-keep]:
            p = os.path.join(root, n)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)


def copy_tree_over(src, dst):
    """New files over the old ones. zabbix.env / devices.json links in dst are never in src, so they stay."""
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)


def service(action):
    subprocess.run(["systemctl", action, "spidermap"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_installer(here):
    return subprocess.run(["bash", os.path.join(here, "install.sh"), "--update"], cwd=here).returncode


def rollback(code_backup, here):
    print("Rolling back to the version that was installed...")
    for name in os.listdir(code_backup):
        s, d = os.path.join(code_backup, name), os.path.join(here, name)
        if os.path.islink(s):
            continue                                  # zabbix.env / devices.json links were never touched
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(s, d)
    rc = run_installer(here)
    service("start")
    return rc == 0


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def git_update(args, here):
    """A git checkout: compare with the upstream branch, ask, then git pull --ff-only."""
    current = installed_version(here)
    if subprocess.run(["git", "-C", here, "fetch", "--quiet"]).returncode != 0:
        raise UpdateError("git fetch failed - see above")
    r = subprocess.run(["git", "-C", here, "show", "@{u}:spidermap.py"], capture_output=True, text=True)
    available = version_in(r.stdout) if r.returncode == 0 else None
    if not available:
        raise UpdateError("cannot read the version on the upstream branch (git -C %s status)" % here)
    behind = subprocess.run(["git", "-C", here, "rev-list", "--count", "HEAD..@{u}"],
                            capture_output=True, text=True).stdout.strip()
    if behind in ("", "0"):
        print(f"Installed: {current}\nAlready up to date (git).")
        return 0
    if args.check:
        print(f"Installed: {current}\nAvailable: {available}   (git, {behind} new commit(s))")
        return 0
    if not confirm_upgrade(current, available, args.yes, f"git, {behind} new commit(s)"):
        print("Update cancelled. No changes made.")
        return 0
    if subprocess.run(["git", "-C", here, "pull", "--ff-only"]).returncode != 0:
        raise UpdateError(f"git pull failed - local changes? (git -C {here} status)")
    return run_installer(here)


def update(args, here=HERE, conf_dir="/etc/spidermap", state_dir="/var/lib/spidermap/state"):
    if os.environ.get("SPIDERMAP_DEPLOYMENT") == "standalone":
        raise UpdateError("standalone deployment: update the container image (docs/STANDALONE.md), not this")
    if args.reinstall:
        print(f"Reinstalling SpiderMap {installed_version(here)} from {here} (nothing is downloaded)")
        return run_installer(here)
    if os.path.isdir(os.path.join(here, ".git")) and not args.main:
        return git_update(args, here)
    current = installed_version(here)
    latest = latest_main() if args.main else latest_release()
    if not newer(latest["version"], current):
        print(f"Installed: {current}\nAvailable: {latest['version']}   ({latest['source']})")
        if vtuple(latest["version"]) and vtuple(latest["version"]) < vtuple(current) and not args.main:
            print("Already newer than the latest release. (The development version: spidermap update --main)")
        else:
            print("Already up to date.")
        return 0
    if args.check:                                    # --check stops before any question or download
        print(f"Installed: {current}\nAvailable: {latest['version']}   ({latest['source']})")
        print(f"To install it:  sudo spidermap update{' --main' if args.main else ''}")
        return 0
    if not confirm_upgrade(current, latest["version"], args.yes, latest["source"]):
        print("Update cancelled. No changes made.")
        return 0

    work = tempfile.mkdtemp(prefix="spidermap-update-")
    try:
        print(f"Downloading {latest['source']}...")
        zpath = os.path.join(work, "spidermap.zip")
        download(latest["zip"], zpath)
        src = safe_extract(zpath, os.path.join(work, "x"))
        verify_tree(src, latest["version"])
        print(f"  checked: all files present, they compile, version {latest['version']}, no settings inside")
        code_bak, data_bak = make_backup(current, here, conf_dir, state_dir)
        print(f"  backup:  {code_bak}\n           {data_bak}")
        service("stop")
        try:
            copy_tree_over(src, here)
            rc = run_installer(here)
        except Exception as e:  # noqa: BLE001
            print(f"  failed: {e}")
            rc = 1
        if rc != 0:
            ok = rollback(code_bak, here)
            raise UpdateError(f"the update failed; {'rolled back to ' + current if ok else 'ROLLBACK ALSO FAILED'}"
                              f" - backup: {code_bak}")
        service("start")
        print(f"\nUpdated {current} -> {latest['version']}.")
        try:
            with open(os.path.join(here, "CHANGELOG.md"), encoding="utf-8") as f:
                notes = changelog_between(f.read(), current, latest["version"])
            if notes:
                lines = notes.splitlines()
                print("\nWhat's new:\n" + "\n".join(lines[:40]) + ("\n  ... (CHANGELOG.md)" if len(lines) > 40 else ""))
        except OSError:
            pass
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def add_arguments(p):
    p.add_argument("--check", action="store_true", help="only say whether a newer version exists")
    p.add_argument("-y", "--yes", action="store_true", help="upgrade without asking for confirmation")
    p.add_argument("--main", action="store_true", help="the latest code on main instead of the latest release")
    p.add_argument("--reinstall", action="store_true", help="re-run the installer on the files already here")
