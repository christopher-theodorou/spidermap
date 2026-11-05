#!/usr/bin/env bash
# SpiderMap installer - run on the Zabbix server (or its container) as root.
#   bash install.sh            first install (asks for the Zabbix address and API token)
#   bash install.sh --update   after "git pull": refresh the page, restart the service
#
# Since 0.5.0: one service ("spidermap") runs everything (no cron), as the user "spidermap";
# configuration in /etc/spidermap, data in /var/lib/spidermap. SPIDERMAP_RUN_AS=root keeps it on root.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLD_DIRS="/opt/zabbix-netmap /opt/autotopo"
CONF=/etc/spidermap
LIB=/var/lib/spidermap
SVC_USER=spidermap
cd "$DIR"

say()  { printf '\n\033[1;34m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "Please run as root."

remove_cron_lines() {   # remove only our lines; read first, then write (never both at once), keep everything else
  local cur
  cur=$(crontab -l 2>/dev/null) || return 0
  printf '%s\n' "$cur" | grep -q -- "$1" || return 0
  printf '%s\n' "$cur" | { grep -v -- "$1" || true; } | crontab -
  echo "  removed the $1 lines from root's crontab (other jobs kept)"
}

cleanup_old_jobs() {   # the earlier version was called zabbix-netmap / netmap
  if systemctl list-unit-files 2>/dev/null | grep -q '^netmap-rt.service'; then
    systemctl disable --now netmap-rt.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/netmap-rt.service && systemctl daemon-reload
    echo "  old netmap-rt service removed"
  fi
  remove_cron_lines 'zabbix_netmap.py'
}

install_ookla() {   # Ookla's official speed test: accurate on fast lines, picks a nearby server
  [ -x /usr/local/bin/ookla-speedtest ] && return 0
  case "$(uname -m)" in
    x86_64) arch=x86_64 ;; aarch64) arch=aarch64 ;; armv7l) arch=armhf ;;
    *) echo "  (no Ookla build for $(uname -m) - speed tests use speedtest-cli)"; return 0 ;;
  esac
  echo
  echo "SpiderMap can use Ookla's official speed test (the speedtest.net engine)."
  echo "It is much more accurate on fast lines, and also measures latency under load."
  echo "Free for personal, non-commercial use. Terms: https://www.speedtest.net/about/eula"
  read -r -p "Install it and accept Ookla's license and privacy terms? [Y/n] " ans
  case "${ans:-Y}" in n|N) echo "  skipped - speed tests use speedtest-cli"; return 0 ;; esac
  tmp=$(mktemp -d)
  if curl -fsSL "https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-${arch}.tgz" | tar xz -C "$tmp" speedtest \
     && install -m 755 "$tmp/speedtest" /usr/local/bin/ookla-speedtest; then
    echo "  Ookla speed test installed"
  else
    echo "  download failed - speed tests use speedtest-cli"
  fi
  rm -rf "$tmp"
}

stop_old_jobs() {   # before moving files: nothing may write to them meanwhile
  remove_cron_lines 'spidermap.py'      # the service runs the scans since 0.5.0
  systemctl stop spidermap-rt.service >/dev/null 2>&1 || true
  systemctl stop spidermap.service >/dev/null 2>&1 || true
  for l in "$DIR/.spidermap-scan.lock" "$LIB/state/.spidermap-scan.lock"; do
    [ -f "$l" ] && command -v flock >/dev/null && flock -w 600 "$l" true 2>/dev/null || true
  done
}

ensure_user() {
  id "$SVC_USER" >/dev/null 2>&1 && return 0
  useradd --system --home-dir "$LIB" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
  echo "  created system user $SVC_USER (no login)"
}

move_layout() {   # configuration -> /etc/spidermap, history -> /var/lib/spidermap/state; links stay in $DIR
  mkdir -p "$CONF" "$LIB/state"
  for f in zabbix.env devices.json; do
    if [ -f "$DIR/$f" ] && [ ! -L "$DIR/$f" ]; then
      if [ -f "$CONF/$f" ]; then mv "$DIR/$f" "$DIR/$f.old-$(date +%s)"; else mv "$DIR/$f" "$CONF/$f"; fi
      echo "  moved $f to $CONF"
    fi
    [ -f "$CONF/$f" ] && ln -sfn "$CONF/$f" "$DIR/$f"
  done
  n=0
  for f in "$DIR"/spidermap_*.json; do
    [ -f "$f" ] || continue
    [ -f "$LIB/state/$(basename "$f")" ] || { mv "$f" "$LIB/state/"; n=$((n + 1)); }
  done
  rm -f "$DIR"/.spidermap*.lock
  [ "$n" -gt 0 ] && echo "  moved $n history files to $LIB/state"
  return 0
}

choose_user() {   # the service runs as $SVC_USER only if ping works for it (needed for scans and latency)
  if [ "${SPIDERMAP_RUN_AS:-}" = "root" ]; then
    echo root > "$CONF/run-as"; echo "  runs as root (SPIDERMAP_RUN_AS=root)"; return 0
  fi
  if runuser -u "$SVC_USER" -- ping -n -c 1 -W 2 127.0.0.1 >/dev/null 2>&1; then
    echo "$SVC_USER" > "$CONF/run-as"
    echo "  runs as user $SVC_USER (ping works without root)"
  else
    echo root > "$CONF/run-as"
    printf '\033[33m%s\033[0m\n' "  stays on root: ping does not work for an ordinary user in this system."
    echo "  (to allow it: setcap cap_net_raw+ep \"\$(command -v ping)\", then bash install.sh --update)"
  fi
  chmod 644 "$CONF/run-as"
}

check_webserver_rules() {   # a rule written for spider-secure.php would block spidermap.php
  local hits
  hits=$(grep -rlsE 'spidermap[^"]*\\?\.\(?[a-z|]*(php|html)' /etc/apache2 /etc/httpd /etc/nginx 2>/dev/null || true)
  [ -z "$hits" ] && return 0
  printf '\n\033[33m%s\033[0m\n' "!! A web server rule blocks SpiderMap files:"
  echo "$hits" | sed 's/^/     /'
  cat <<'EOW'
   SpiderMap is now opened through spidermap.php, which checks the Zabbix login itself,
   so the rule must no longer block .php and .html files. Change it to block only .json:

     <FilesMatch "^spidermap.*\.json$">
       Require all denied
     </FilesMatch>

   then check and reload:  apachectl configtest && systemctl reload apache2
EOW
}

if [ "${1:-}" = "--update" ]; then
  say "Updating SpiderMap to $(python3 spidermap.py --version 2>/dev/null | cut -d' ' -f2)..."
  [ -f zabbix.env ] || [ -f "$CONF/zabbix.env" ] || fail "No zabbix.env - run: bash install.sh"
  cleanup_old_jobs
  install_ookla
  stop_old_jobs
  ensure_user
  move_layout
  choose_user
  python3 spidermap.py --install-web
  python3 spidermap.py --install-service
  check_webserver_rules
  say "Done. Open pages reload themselves with the new version within a minute."
  echo "Service: systemctl status spidermap    Log: journalctl -u spidermap  and  $LIB/spidermap.log"
  exit 0
fi

cat <<'EOF2'

   SpiderMap
   Nothing touches the web unnoticed.

EOF2

say "1/5  Installing packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq snmp python3-requests ieee-data speedtest-cli iputils-ping curl util-linux >/dev/null
install_ookla

say "2/5  Configuration"
if [ ! -f zabbix.env ]; then
  for OLD in $OLD_DIRS; do
    [ "$OLD" != "$DIR" ] && [ -f "$OLD/zabbix.env" ] || continue
    echo "Found an earlier install in $OLD."
    read -r -p "Move its settings, devices and history to SpiderMap? [Y/n] " a
    case "${a:-Y}" in n|N) continue ;; esac
    for f in zabbix.env devices.json; do [ -f "$OLD/$f" ] && cp -p "$OLD/$f" "$DIR/$f" && echo "  copied $f"; done
    for f in "$OLD"/netmap_*.json; do
      [ -f "$f" ] || continue
      new="$DIR/spidermap_$(basename "$f" | sed 's/^netmap_//')"
      cp -p "$f" "$new" && echo "  copied $(basename "$f") -> $(basename "$new")"
    done
    echo "  (the files in $OLD are left untouched - delete the folder once SpiderMap runs fine)"
    break
  done
fi
cleanup_old_jobs
stop_old_jobs
ensure_user
move_layout
if [ ! -f "$CONF/zabbix.env" ]; then
  read -r -p "Zabbix API URL [http://127.0.0.1/zabbix/api_jsonrpc.php]: " url
  url="${url:-http://127.0.0.1/zabbix/api_jsonrpc.php}"
  read -r -s -p "Zabbix API token (Users > API tokens): " tok; echo
  printf 'export ZABBIX_URL="%s"\nexport ZABBIX_TOKEN="%s"\n' "$url" "$tok" > "$CONF/zabbix.env"
  ln -sfn "$CONF/zabbix.env" "$DIR/zabbix.env"
fi
chmod 640 "$CONF/zabbix.env"
. "$CONF/zabbix.env"

say "3/5  Checking the Zabbix API"
ver=$(curl -s -X POST -H 'Content-Type: application/json-rpc' \
      -d '{"jsonrpc":"2.0","method":"apiinfo.version","params":{},"id":1}' "$ZABBIX_URL" | grep -o '"result":"[^"]*"' || true)
[ -n "$ver" ] || fail "No answer from $ZABBIX_URL - check the address in zabbix.env."
echo "  Zabbix $ver"
ok=$(curl -s -X POST -H 'Content-Type: application/json-rpc' -H "Authorization: Bearer $ZABBIX_TOKEN" \
      -d '{"jsonrpc":"2.0","method":"host.get","params":{"countOutput":true},"id":2}' "$ZABBIX_URL" | grep -o '"result":"[0-9]*"' || true)
[ -n "$ok" ] || fail "The API token was refused - check ZABBIX_TOKEN in zabbix.env."
echo "  token OK, $ok hosts"

say "4/5  Service user"
choose_user

say "5/5  Setup: page, icons, Zabbix items, dashboard, service (it runs the scans: no cron needed)"
python3 spidermap.py --setup

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
path=$(python3 -c 'import os; from urllib.parse import urlparse; print(urlparse(os.environ["ZABBIX_URL"]).path.rsplit("/", 1)[0])')
say "SpiderMap $(python3 spidermap.py --version 2>/dev/null | cut -d' ' -f2) is running."
cat <<EOF2
Open the map:   http://${ip}${path}/spidermap.php   (Zabbix Super admins; see Settings > Access)
Or in Zabbix:   Dashboards > "SpiderMap"

One manual step in Zabbix (needed for the dashboard widget):
  Administration > General > Other > Security
  "Iframe sandboxing exceptions":  allow-scripts allow-same-origin allow-popups allow-downloads

Your names, icons and pins live in devices.json ($CONF, linked here). Scans, intervals and access: the
Settings button on the map.
EOF2
check_webserver_rules
