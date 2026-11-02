#!/usr/bin/env bash
# SpiderMap installer - run on the Zabbix server (or its container) as root.
#   bash install.sh            first install (asks for the Zabbix address and API token)
#   bash install.sh --update   after "git pull": refresh the page, restart the service
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLD_DIRS="/opt/zabbix-netmap /opt/autotopo"
cd "$DIR"

say()  { printf '\n\033[1;34m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "Please run as root."

cleanup_old_jobs() {   # the earlier version was called zabbix-netmap / netmap
  if systemctl list-unit-files 2>/dev/null | grep -q '^netmap-rt.service'; then
    systemctl disable --now netmap-rt.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/netmap-rt.service && systemctl daemon-reload
    echo "  old netmap-rt service removed"
  fi
  ( crontab -l 2>/dev/null | grep -v 'zabbix_netmap.py' || true ) | crontab -
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

if [ "${1:-}" = "--update" ]; then
  say "Updating SpiderMap..."
  [ -f zabbix.env ] || fail "No zabbix.env here - run: bash install.sh"
  . ./zabbix.env
  cleanup_old_jobs
  install_ookla
  python3 spidermap.py --quiet
  python3 spidermap.py --install-service
  say "Done. Open pages reload themselves with the new version within a minute."
  exit 0
fi

cat <<'EOF2'

   SpiderMap
   Nothing touches the web unnoticed.

EOF2

say "1/5  Installing packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq snmp python3-requests ieee-data speedtest-cli cron iputils-ping curl >/dev/null
systemctl enable --now cron >/dev/null 2>&1 || true
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
if [ ! -f zabbix.env ]; then
  read -r -p "Zabbix API URL [http://127.0.0.1/zabbix/api_jsonrpc.php]: " url
  url="${url:-http://127.0.0.1/zabbix/api_jsonrpc.php}"
  read -r -s -p "Zabbix API token (Users > API tokens): " tok; echo
  printf 'export ZABBIX_URL="%s"\nexport ZABBIX_TOKEN="%s"\n' "$url" "$tok" > zabbix.env
fi
chmod 600 zabbix.env
. ./zabbix.env

say "3/5  Checking the Zabbix API"
ver=$(curl -s -X POST -H 'Content-Type: application/json-rpc' \
      -d '{"jsonrpc":"2.0","method":"apiinfo.version","params":{},"id":1}' "$ZABBIX_URL" | grep -o '"result":"[^"]*"' || true)
[ -n "$ver" ] || fail "No answer from $ZABBIX_URL - check the address in zabbix.env."
echo "  Zabbix $ver"
ok=$(curl -s -X POST -H 'Content-Type: application/json-rpc' -H "Authorization: Bearer $ZABBIX_TOKEN" \
      -d '{"jsonrpc":"2.0","method":"host.get","params":{"countOutput":true},"id":2}' "$ZABBIX_URL" | grep -o '"result":"[0-9]*"' || true)
[ -n "$ok" ] || fail "The API token was refused - check ZABBIX_TOKEN in zabbix.env."
echo "  token OK, $ok hosts"

say "4/5  Setup: page, icons, Zabbix items, dashboard, real-time service, first scan"
python3 spidermap.py --setup

say "5/5  Scheduled jobs"
( crontab -l 2>/dev/null | grep -v 'spidermap.py' || true
  echo "*/5 * * * * cd $DIR && . ./zabbix.env && python3 spidermap.py --quiet >> /var/log/spidermap.log 2>&1"
  echo "* * * * * cd $DIR && . ./zabbix.env && python3 spidermap.py --live >> /var/log/spidermap.log 2>&1"
) | crontab -
echo "  full scan every 5 minutes, live refresh every minute"

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
path=$(python3 -c 'import os; from urllib.parse import urlparse; print(urlparse(os.environ["ZABBIX_URL"]).path.rsplit("/", 1)[0])')
say "SpiderMap is running."
cat <<EOF2
Open the map:   http://${ip}${path}/spidermap.html
Or in Zabbix:   Dashboards > "SpiderMap"

One manual step in Zabbix (needed for the dashboard widget):
  Administration > General > Other > Security
  "Iframe sandboxing exceptions":  allow-scripts allow-same-origin allow-popups allow-downloads

Your names, icons and pins live in devices.json in this folder.
EOF2
