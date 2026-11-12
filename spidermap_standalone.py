"""SpiderMap standalone deployment: its own web server and its own users, outside every Zabbix frontend.

Run by the container's "init" step (see standalone/ and docs/STANDALONE.md). Makes no Zabbix API call:
Zabbix is only read later, by the collector, and only with the read-only allowlist in spidermap_zabbix.py.
Proposed and prototyped by Nikolaos Karanikolas.
"""
import json
import os
import shutil
from pathlib import Path

AUTH_DIR = "/run/spidermap-auth"          # users.htpasswd + roles.json, mounted read-only
WEB_ROOT = "/var/www/html"
ROLES = {"viewer": 1, "editor": 2, "admin": 3}

# The page's Zabbix wording, and its Greek translations, for a SpiderMap with its own users.
PAGE_WORDS = [
    ('"Zabbix Super admins": "Zabbix Super admins", "Zabbix admins": "Zabbix admins", "Zabbix users": "Χρήστες Zabbix"',
     '"SpiderMap administrators": "Διαχειριστές SpiderMap", "SpiderMap editors": "Συντάκτες SpiderMap", '
     '"SpiderMap viewers": "Θεατές SpiderMap"'),
    ("Zabbix Super admins", "SpiderMap administrators"),
    ("Zabbix admins", "SpiderMap editors"),
    ("Zabbix users", "SpiderMap viewers"),
    ("Log in to Zabbix", "Log in to SpiderMap"),
    ("only to logged-in Zabbix users", "only to signed-in SpiderMap users"),
    ("Open the Zabbix login", "Reload SpiderMap"),
    ('href="${esc(z)}index.php" target="_top"', 'href="spidermap.php" target="_top"'),
]


def standalone_page(text):
    for old, new in PAGE_WORDS:
        text = text.replace(old, new)
    return text


def standalone_gateway():
    """spidermap.php for the standalone web server: Apache has already checked the password (bcrypt
    htpasswd); the role comes from roles.json on the server - never from anything the browser sends."""
    from spidermap_gateway import gateway_code
    code = gateway_code({}, "")
    start = code.index("function api_call(")
    end = code.index("$user = zabbix_user();", start) + len("$user = zabbix_user();")
    auth = r'''// Apache authenticated the user before PHP runs. Never trust an identity header from the browser.
$name = (string)($_SERVER['REMOTE_USER'] ?? '');
if ($name === '') deny(401, 'SpiderMap login required.');
$roles = json_decode((string)@file_get_contents('/run/spidermap-auth/roles.json'), true);
$role = is_array($roles) ? (string)($roles[$name] ?? '') : '';
$types = ['viewer' => 1, 'editor' => 2, 'admin' => 3];
if (!isset($types[$role])) deny(403, 'No SpiderMap role for this user (auth/roles.json).');
$user = ['id' => $name, 'name' => $name, 'type' => $types[$role]];
'''
    code = code[:start] + auth + code[end:]
    # Fixed roles: viewer sees, editor changes devices and runs speed tests, admin changes settings.
    start = code.index("function perm(")
    end = code.index("// A request for the SpiderMap service", start)
    code = (code[:start] + "function perm($what) { return ['view' => 1, 'edit' => 2, 'speedtest' => 2, "
            "'settings' => 3][$what] ?? 3; }\n\n" + code[end:])
    for old, new in PAGE_WORDS[1:4]:
        code = code.replace(old, new)
    # Same origin means scheme, host and port - the public address set in SPIDERMAP_PUBLIC_ORIGIN.
    old = ("$origin !== '' && parse_url($origin, PHP_URL_HOST) !== "
           "parse_url('//' . ($_SERVER['HTTP_HOST'] ?? ''), PHP_URL_HOST)")
    new = ("$origin !== '' && rtrim($origin, '/') !== "
           "rtrim(getenv('SPIDERMAP_PUBLIC_ORIGIN') ?: 'http://' . ($_SERVER['HTTP_HOST'] ?? ''), '/')")
    if old not in code:
        raise RuntimeError("spidermap_gateway.py changed: update standalone_gateway() too")
    return code.replace(old, new)


def check_auth_files(auth_dir=AUTH_DIR):
    """users.htpasswd and roles.json must exist, and every role must be viewer, editor or admin."""
    users, roles = Path(auth_dir, "users.htpasswd"), Path(auth_dir, "roles.json")
    if not users.is_file():
        raise SystemExit(f"Create {users} first (docs/STANDALONE.md, step 4).")
    if not roles.is_file():
        raise SystemExit(f"Create {roles} first (docs/STANDALONE.md, step 5).")
    try:
        data = json.loads(roles.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SystemExit(f"{roles} is not valid JSON: {e}")
    bad = {k: v for k, v in (data.items() if isinstance(data, dict) else []) if v not in ROLES}
    if not isinstance(data, dict) or bad:
        raise SystemExit(f"{roles}: every user needs the role viewer, editor or admin" + (f" (not {bad})" if bad else ""))
    names = {line.split(":", 1)[0] for line in users.read_text(encoding="utf-8").splitlines() if ":" in line}
    missing = sorted(set(data) - names)
    if missing:
        print(f"  note: {', '.join(missing)} in roles.json but not in users.htpasswd - they cannot log in")
    return data


def prepare():
    import spidermap as sm
    if os.environ.get("SPIDERMAP_DEPLOYMENT") != "standalone":
        raise SystemExit("Only for the standalone deployment (SPIDERMAP_DEPLOYMENT=standalone).")
    check_auth_files()
    for p in (sm.CONF_DIR, sm.STATE_DIR, sm.DATA_DIR, sm.INBOX):
        Path(p).mkdir(parents=True, exist_ok=True)
    if not Path(sm.DEVICES_FILE).exists():
        Path(sm.DEVICES_FILE).write_text(json.dumps(sm.DEFAULT_DEVICES), encoding="utf-8")
    Path(sm.CONF_DIR, "run-as").write_text("spidermap", encoding="utf-8")
    try:
        sm.install_icons(WEB_ROOT)           # optional: without internet the page uses the online copy
    except Exception as e:  # noqa: BLE001
        print(f"  icons not downloaded ({e}) - the page falls back to the internet copy")
    sm.install_page(WEB_ROOT)                # the standalone page and spidermap.php (see install_page)
    for p in Path(sm.HERE, "assets").glob("*.svg"):
        shutil.copyfile(p, Path(WEB_ROOT, sm.ASSETS, p.name))
    # The configuration, with the API token and controller secrets, is never readable by the web server.
    import pwd
    try:
        u = pwd.getpwnam("spidermap")
        uid, gid = u.pw_uid, u.pw_gid
    except KeyError:
        uid = gid = 0
    for root, dirs, files in os.walk(sm.CONF_DIR):
        os.chown(root, uid, gid)
        os.chmod(root, 0o750)
        for name in files:
            os.chown(os.path.join(root, name), uid, gid)
            os.chmod(os.path.join(root, name), 0o640)
    if not Path(sm.CONF_DIR, "zabbix.env").is_file():
        print(f"  no {sm.CONF_DIR}/zabbix.env yet - add the read-only API token before starting the collector "
              "(docs/STANDALONE.md, step 6)")
    print("SpiderMap standalone: ready (Zabbix read-only, own users).")


if __name__ == "__main__":
    prepare()
