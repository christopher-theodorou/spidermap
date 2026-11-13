#!/usr/bin/env bash
if [ "${SPIDERMAP_DEPLOYMENT:-}" = "standalone" ]; then
  echo "Standalone deployment: install and update it with docker compose (docs/STANDALONE.md), not install.sh." >&2
  exit 2
fi
# SpiderMap installer - run on the Zabbix server (or its container) as root.
#   bash install.sh              the menu (install, update, doctor, Zabbix, Proxmox, permissions, logs, uninstall)
#   bash install.sh --install    first install without the menu (asks for the Zabbix address and API token)
#   bash install.sh --update     refresh the page, restart the service (spidermap update downloads first)
#   bash install.sh --uninstall  [--keep-data] [--keep-zabbix] [--yes]
#   bash install.sh --menu       the menu, also when not run from a terminal
# Without a terminal (scripts, pipes), plain "bash install.sh" installs as before.
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

CLI=/usr/local/bin/spidermap
CLI_MARK="SpiderMap command - installed by install.sh"
COMPLETION=/etc/bash_completion.d/spidermap

install_command() {   # the "spidermap" command, usable from anywhere (see docs/COMMANDS.md)
  if [ -e "$CLI" ] && ! grep -q "$CLI_MARK" "$CLI" 2>/dev/null; then
    printf '\033[33m%s\033[0m\n' "  $CLI exists and is not SpiderMap's - left alone (use: python3 $DIR/spidermap_cli.py)"
    return 0
  fi
  printf '#!/bin/sh\n# %s. See docs/COMMANDS.md\nexec python3 "%s/spidermap_cli.py" "$@"\n' "$CLI_MARK" "$DIR" > "$CLI"
  chmod 755 "$CLI"
  if [ -d "$(dirname "$COMPLETION")" ]; then
    echo 'complete -W "status logs scan doctor test-snmp check-ports cert fix-permissions update uninstall menu version help" spidermap' > "$COMPLETION"
  fi
  echo "  command installed: spidermap   (spidermap help)"
}

ask_zabbix_mode() {   # sets ZRO: 1 = read-only (nothing created in Zabbix), 0 = events also go to Zabbix
  echo
  echo "How should SpiderMap use Zabbix?"
  echo "  1) Read only (recommended): it reads hosts, traffic and problems - and creates nothing in Zabbix."
  echo "     New devices, moves and cable problems are in SpiderMap's Activity. A read-only API token is enough."
  echo "  2) Also send its events to Zabbix: creates 'autotopo.*' items and triggers on the Zabbix server host and"
  echo "     the SpiderMap dashboard, so Zabbix actions can notify you. The token must be allowed to create them."
  local m; read -r -p "Choose [1]: " m || m=1
  case "${m:-1}" in 2) ZRO=0 ;; *) ZRO=1 ;; esac
}

api_check() {   # $1 API URL, $2 token. Prints what it finds; the token never appears in the process list.
  local out rc=0 n
  out=$(curl -sS -m 10 -X POST -H 'Content-Type: application/json-rpc' \
        -d '{"jsonrpc":"2.0","method":"apiinfo.version","params":{},"id":1}' "$1" 2>&1) || rc=$?
  if [ $rc = 60 ] || [ $rc = 35 ] || [ $rc = 51 ] || [ $rc = 58 ]; then
    echo "  $1: this server does not trust that https certificate."
    echo "  Use http://127.0.0.1/zabbix/api_jsonrpc.php instead: SpiderMap runs on this server, so the"
    echo "  connection never leaves it and needs no certificate."
    return 1
  fi
  out=$(printf '%s' "$out" | grep -o '"result": *"[^"]*"' | cut -d'"' -f4 || true)
  [ -n "$out" ] || { echo "  No answer from $1 - check the address."; return 1; }
  echo "  Zabbix $out"
  n=$(printf 'header = "Authorization: Bearer %s"\n' "$2" | curl -s -m 10 -K - -X POST -H 'Content-Type: application/json-rpc' \
      -d '{"jsonrpc":"2.0","method":"host.get","params":{"countOutput":true},"id":2}' "$1" \
      | grep -o '"result": *"[0-9]*"' | cut -d'"' -f4 || true)
  [ -n "$n" ] || { echo "  The API token was refused - create one in Zabbix: Users > API tokens."; return 1; }
  echo "  token OK, $n hosts"
}

do_uninstall() {
  # Removes what SpiderMap created, after a plan, a confirmation and a backup.
  #   --keep-data    keep /etc/spidermap, /var/lib/spidermap and the spidermap user (to install again later)
  #   --keep-zabbix  leave SpiderMap's items, triggers, dashboards and map in Zabbix
  #   --yes          do not ask (for scripts)
  KEEP_DATA=0; KEEP_ZBX=0; YES=0
  for a in "$@"; do
    case "$a" in --keep-data) KEEP_DATA=1 ;; --keep-zabbix) KEEP_ZBX=1 ;; --yes|-y) YES=1 ;; *) fail "Unknown option: $a" ;; esac
  done
  [ "$(id -u)" = 0 ] || fail "Run as root."
  say "SpiderMap uninstall. This is what will be removed:"
  echo "  service: spidermap (and the older spidermap-rt, cron lines, /var/log/spidermap.log if still there)"
  python3 spidermap.py --uninstall-web --dry-run || true
  if [ $KEEP_ZBX = 1 ]; then echo "  Zabbix: kept (--keep-zabbix)"
  else python3 spidermap.py --uninstall-zabbix --with-dashboards --dry-run 2>/dev/null || echo "  Zabbix: cannot be reached now - remove the SpiderMap dashboard and the 'autotopo.' items by hand"; fi
  if [ $KEEP_DATA = 1 ]; then echo "  kept (--keep-data): $CONF, $LIB, the user $SVC_USER"
  else echo "  $CONF (settings, API token), $LIB (history, logs, backups), the user $SVC_USER"; fi
  [ -x /usr/local/bin/ookla-speedtest ] && echo "  /usr/local/bin/ookla-speedtest (installed by SpiderMap)"
  grep -qs "$CLI_MARK" "$CLI" && echo "  the spidermap command ($CLI)"
  echo "  not touched: system packages, the API token and the iframe setting in Zabbix, $DIR (the code: remove it yourself at the end)"
  BACKUP="/root/spidermap-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  echo "  first, a backup of your settings, devices and history: $BACKUP"
  if [ $YES != 1 ]; then
    read -r -p "Type 'remove' to continue: " ans
    [ "$ans" = "remove" ] || { echo "Nothing removed."; exit 0; }
  fi
  ( umask 077; tar czf "$BACKUP" $(for p in "$CONF" "$LIB/state" "$LIB/audit.log" "$LIB/speedtest.json" "$LIB/backup"; do [ -e "$p" ] && echo "$p"; done) 2>/dev/null ) \
    && echo "  backup written: $BACKUP" || echo "  (nothing to back up)"
  if [ $KEEP_ZBX != 1 ]; then                       # first, while the API token still exists
    SPIDERMAP_ZABBIX_READONLY=0 python3 spidermap.py --uninstall-zabbix --with-dashboards || printf '\033[33m%s\033[0m\n' "  some Zabbix objects could not be removed - see above"
  fi
  for u in spidermap.service spidermap-rt.service; do
    systemctl disable --now "$u" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$u"; rm -rf "/etc/systemd/system/$u.d"
  done
  systemctl daemon-reload >/dev/null 2>&1 || true
  remove_cron_lines 'spidermap.py'; remove_cron_lines 'zabbix_netmap.py'
  python3 spidermap.py --uninstall-web
  rm -f /usr/local/bin/ookla-speedtest /var/log/spidermap.log
  grep -qs "$CLI_MARK" "$CLI" && rm -f "$CLI" "$COMPLETION"
  if [ $KEEP_DATA != 1 ]; then
    rm -rf "$LIB" "$CONF"
    for f in zabbix.env devices.json; do [ -L "$DIR/$f" ] && rm -f "$DIR/$f"; done
    id "$SVC_USER" >/dev/null 2>&1 && userdel "$SVC_USER" 2>/dev/null || true
  fi
  say "SpiderMap is removed."
  echo "Remove the code too:   rm -rf $DIR"
  echo "Your backup:           $BACKUP   (to restore after a new install: tar xzf $BACKUP -C /)"
  exit 0
}

do_update() {
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
  install_command
  check_webserver_rules
  say "Done. Open pages reload themselves with the new version within a minute."
  echo "Check:  spidermap status    spidermap doctor    spidermap logs"
  exit 0
}

do_install() {
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
  echo "SpiderMap runs on this server, so the local address is best: it never leaves the server and needs no"
  echo "certificate, even when your browsers use https."
  read -r -p "Zabbix API URL [http://127.0.0.1/zabbix/api_jsonrpc.php]: " url
  url="$(normalize_api_url "${url:-http://127.0.0.1/zabbix/api_jsonrpc.php}")"
  read -r -s -p "Zabbix API token (Users > API tokens): " tok; echo
  ask_zabbix_mode
  printf 'export ZABBIX_URL="%s"\nexport ZABBIX_TOKEN="%s"\nexport SPIDERMAP_ZABBIX_READONLY="%s"\n' \
    "$url" "$tok" "$ZRO" > "$CONF/zabbix.env"
  ln -sfn "$CONF/zabbix.env" "$DIR/zabbix.env"
fi
chmod 640 "$CONF/zabbix.env"
. "$CONF/zabbix.env"

say "3/5  Checking the Zabbix API"
api_check "$ZABBIX_URL" "$ZABBIX_TOKEN" || fail "Fix it with:  bash install.sh, then 4) Zabbix connection  (or edit $CONF/zabbix.env)"

say "4/5  Service user"
choose_user

if [ "${SPIDERMAP_ZABBIX_READONLY:-0}" = "1" ]; then
  say "5/5  Setup: page, icons, service (Zabbix read-only: nothing is created in Zabbix)"
else
  say "5/5  Setup: page, icons, Zabbix items, dashboard, service (it runs the scans: no cron needed)"
fi
python3 spidermap.py --setup

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
path=$(python3 -c 'import os; from urllib.parse import urlparse; print(urlparse(os.environ["ZABBIX_URL"]).path.rsplit("/", 1)[0])')
scheme=http; python3 "$DIR/spidermap_cli.py" cert --fingerprint >/dev/null 2>&1 && scheme=https
say "SpiderMap $(python3 spidermap.py --version 2>/dev/null | cut -d' ' -f2) is running."
echo "Open the map:   ${scheme}://${ip}${path}/spidermap.php   (Zabbix Super admins; see Settings > Access)"
if [ "${SPIDERMAP_ZABBIX_READONLY:-0}" = "1" ]; then
  echo "Zabbix read-only: SpiderMap's events are in its Activity, not in Zabbix (no items, triggers or dashboard)."
  echo "Want a dashboard widget anyway? Add a URL widget yourself (docs/COMMANDS.md, read-only mode)."
else
cat <<EOF2
Or in Zabbix:   Dashboards > "SpiderMap"

One manual step in Zabbix (needed for the dashboard widget):
  Administration > General > Other > Security
  "Iframe sandboxing exceptions":  allow-scripts allow-same-origin allow-popups allow-downloads
EOF2
fi
cat <<EOF2

Your names, icons and pins live in devices.json ($CONF, linked here). Scans, intervals and access: the
Settings button on the map.
EOF2
install_command
cat <<EOF2

Useful commands:  spidermap status   spidermap doctor   spidermap logs   spidermap help
No switches on the map yet?  docs/FIRST-SWITCH.md, or:  spidermap test-snmp <switch IP> --community <community>
EOF2
check_webserver_rules
}

# --------------------------------------------------------------------------- #
# The menu
# --------------------------------------------------------------------------- #
CLIPY="$DIR/spidermap_cli.py"

normalize_api_url() {   # "10.20.0.46/zabbix" -> "http://10.20.0.46/zabbix/api_jsonrpc.php"
  local u="$1"
  case "$u" in http://*|https://*) ;; *) u="http://$u" ;; esac
  case "$u" in */api_jsonrpc.php) ;; *) u="${u%/}/api_jsonrpc.php" ;; esac
  printf '%s' "$u"
}

env_get() { sed -n "s/^export $1=\"\\(.*\\)\"\$/\\1/p" "$CONF/zabbix.env" 2>/dev/null | head -1; }
mask()    { local t="$1"; [ ${#t} -gt 10 ] && printf '%s…%s' "${t:0:4}" "${t: -4}" || printf '%s' "${t:+(set)}"; }
pause()   { echo; read -r -p "Press Enter for the menu " _ || exit 0; }
installed() { [ -f "$CONF/zabbix.env" ] || [ -f "$DIR/zabbix.env" ]; }
restart_service() {
  systemctl is-enabled spidermap >/dev/null 2>&1 || return 0
  systemctl restart spidermap && echo "  service restarted"
}

menu_zabbix() {
  say "Zabbix connection"
  local cur_url cur_tok url tok a
  cur_url=$(env_get ZABBIX_URL); cur_tok=$(env_get ZABBIX_TOKEN)
  echo "  API address: ${cur_url:-not set}"
  echo "  API token:   $(mask "$cur_tok")"
  echo "  (Local http://127.0.0.1 is best: it never leaves this server, also when browsers use https.)"
  echo
  read -r -p "New API address (Enter keeps it): " url
  url="${url:-$cur_url}"; url="$(normalize_api_url "${url:-http://127.0.0.1/zabbix/api_jsonrpc.php}")"
  read -r -s -p "New API token (Enter keeps it): " tok; echo
  tok="${tok:-$cur_tok}"
  if [ "$url" = "$cur_url" ] && [ "$tok" = "$cur_tok" ]; then echo "  Nothing changed."; fi
  if ! api_check "$url" "$tok"; then
    read -r -p "Save it anyway? [y/N] " a
    case "$a" in y|Y) ;; *) echo "  Nothing saved."; return 0 ;; esac
  fi
  local cur_ro; cur_ro=$(env_get SPIDERMAP_ZABBIX_READONLY); [ "$cur_ro" = 1 ] || cur_ro=0
  echo
  if [ "$cur_ro" = 1 ]; then echo "  Mode now: read only (nothing is created in Zabbix)"
  else echo "  Mode now: events also go to Zabbix ('autotopo.*' items and triggers, the SpiderMap dashboard)"; fi
  ask_zabbix_mode
  # First the new mode is saved and the service restarted - only then is anything removed, so the service can
  # never recreate items in between.
  SMSET_ZABBIX_URL="$url" SMSET_ZABBIX_TOKEN="$tok" SMSET_SPIDERMAP_ZABBIX_READONLY="$ZRO" python3 "$CLIPY" _setenv
  if [ "$cur_ro" = 1 ] && [ "$ZRO" = 0 ]; then
    echo "  Creating the Zabbix items, triggers and dashboard..."
    SPIDERMAP_ZABBIX_READONLY=0 python3 spidermap.py --setup || echo "  Setup failed - see above (token rights?)"
  fi
  restart_service
  if [ "$cur_ro" = 0 ] && [ "$ZRO" = 1 ]; then
    systemctl is-active --quiet spidermap && echo "  The service now runs read-only: it no longer writes to Zabbix."
    echo
    echo "  SpiderMap no longer updates what it created in Zabbix earlier: last values and open problems would stay."
    echo "  Exactly these (only items with the tag source: autotopo; nothing else is touched):"
    if SPIDERMAP_ZABBIX_READONLY=0 python3 spidermap.py --uninstall-zabbix --dry-run 2>/dev/null; then
      read -r -p "Remove these items and their triggers from Zabbix now? Their Zabbix history goes too. [y/N] " a
      case "$a" in
        y|Y) SPIDERMAP_ZABBIX_READONLY=0 python3 spidermap.py --uninstall-zabbix \
               || echo "  Some could not be removed - remove them in Zabbix (tag source: autotopo)." ;;
        *)   echo "  Left in Zabbix. Later: SPIDERMAP_ZABBIX_READONLY=0 python3 $DIR/spidermap.py --uninstall-zabbix" ;;
      esac
      echo
      SPIDERMAP_ZABBIX_READONLY=0 python3 spidermap.py --uninstall-zabbix --with-dashboards --dry-run 2>/dev/null \
        | grep "dashboards:" || true
      read -r -p "Also remove the SpiderMap dashboard? Keep it if you use it to view the map. [y/N] " a
      case "$a" in
        y|Y) SPIDERMAP_ZABBIX_READONLY=0 python3 spidermap.py --uninstall-zabbix --with-dashboards \
               || echo "  Could not remove it - remove it in Zabbix (Dashboards)." ;;
        *)   echo "  Dashboard kept." ;;
      esac
    else
      echo "  (Zabbix not reachable now - later: SPIDERMAP_ZABBIX_READONLY=0 python3 $DIR/spidermap.py --uninstall-zabbix --dry-run)"
    fi
  fi
}

menu_proxmox() {
  say "Proxmox (optional): names and state of your VMs and containers"
  local cur a url hp tid sec fp
  cur=$(env_get PVE_URL)
  if [ -n "$cur" ]; then
    echo "  Connected to $cur as $(env_get PVE_TOKEN_ID)"
    if [ -n "$(env_get PVE_FINGERPRINT)" ]; then echo "  Certificate: pinned ($(env_get PVE_FINGERPRINT | tr -d : | cut -c1-8)…)"
    elif [ "$(env_get PVE_VERIFY)" = 1 ] || [ -n "$(env_get PVE_CA)" ]; then echo "  Certificate: checked"
    else printf '\033[33m%s\033[0m\n' "  Certificate: NOT checked - choose 1 to pin it"; fi
  else
    echo "  Not set up."
  fi
  echo
  echo "  1) Set up / change     2) Remove     0) Back"
  read -r -p "Choose: " a
  case "$a" in
    2) python3 "$CLIPY" _setenv --unset PVE_URL --unset PVE_TOKEN_ID --unset PVE_TOKEN_SECRET \
         --unset PVE_FINGERPRINT --unset PVE_CA --unset PVE_VERIFY && restart_service; return 0 ;;
    1) ;;
    *) return 0 ;;
  esac
  cat <<'EOT'

  In Proxmox, first: add the user spidermap@pve with the role PVEAuditor on / (Datacenter > Permissions),
  then an API token "spidermap" for it with "Privilege Separation" unticked. Copy the secret.
EOT
  read -r -p "Proxmox address [${cur:-https://192.168.1.10:8006}]: " url
  url="${url:-$cur}"; [ -n "$url" ] || { echo "  Nothing saved."; return 0; }
  case "$url" in https://*) ;; http://*) url="https://${url#http://}" ;; *) url="https://$url" ;; esac
  url="${url%/}"; hp="${url#https://}"; hp="${hp%%/*}"
  case "$hp" in *:*) ;; *) hp="$hp:8006"; url="https://$hp" ;; esac
  read -r -p "Token ID [$(env_get PVE_TOKEN_ID || true)] (like spidermap@pve!spidermap): " tid
  tid="${tid:-$(env_get PVE_TOKEN_ID)}"
  read -r -s -p "Token secret (Enter keeps it): " sec; echo
  sec="${sec:-$(env_get PVE_TOKEN_SECRET)}"
  [ -n "$tid" ] && [ -n "$sec" ] || { echo "  Token ID and secret are both needed. Nothing saved."; return 0; }

  echo; echo "  The certificate Proxmox presents:"
  if ! python3 "$CLIPY" cert "$hp"; then echo "  Nothing saved."; return 0; fi
  if python3 "$CLIPY" cert "$hp" --check-trust >/dev/null 2>&1; then
    echo "  This server already trusts it (a real certificate): it is checked normally, also after renewals."
    SMSET_PVE_URL="$url" SMSET_PVE_TOKEN_ID="$tid" SMSET_PVE_TOKEN_SECRET="$sec" SMSET_PVE_VERIFY=1 \
      python3 "$CLIPY" _setenv --unset PVE_FINGERPRINT --unset PVE_CA
  else
    fp=$(python3 "$CLIPY" cert "$hp" --fingerprint)
    cat <<'EOT'

  Compare the SHA-256 above with Proxmox: <node> > System > Certificates > pve-ssl.pem > View Certificate
  (or on Proxmox: openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256).
  If they match, SpiderMap accepts exactly this certificate and refuses any other.
EOT
    read -r -p "Do they match? Pin it [y/N] " a
    case "$a" in y|Y) ;; *) echo "  Nothing saved."; return 0 ;; esac
    SMSET_PVE_URL="$url" SMSET_PVE_TOKEN_ID="$tid" SMSET_PVE_TOKEN_SECRET="$sec" SMSET_PVE_FINGERPRINT="$fp" \
      python3 "$CLIPY" _setenv --unset PVE_CA --unset PVE_VERIFY
    echo "  (When Proxmox renews this certificate, come back here to pin the new one: spidermap doctor tells you.)"
  fi
  local code
  code=$(printf 'header = "Authorization: PVEAPIToken=%s=%s"\n' "$tid" "$sec" \
         | curl -sk -m 8 -K - -o /dev/null -w '%{http_code}' "$url/api2/json/version" || true)
  case "$code" in
    200) echo "  Proxmox answers, token accepted." ;;
    401) printf '\033[33m%s\033[0m\n' "  Proxmox refused the token - check the ID and secret (option 1 again)." ;;
    *)   printf '\033[33m%s\033[0m\n' "  Proxmox did not answer (HTTP ${code:-none}) - check the address." ;;
  esac
  restart_service
}

menu_permissions() {
  say "Fixing permissions"
  ensure_user
  move_layout
  choose_user
  python3 "$CLIPY" fix-permissions
}

menu_update() {   # a newer version on GitHub? It asks first, backs up, and rolls back by itself if it fails
  python3 "$CLIPY" update || true
}

menu() {
  local a v st
  while true; do
    v=$(python3 spidermap.py --version 2>/dev/null | cut -d' ' -f2 || true)
    if installed; then
      st=$(systemctl is-active spidermap 2>/dev/null || true); st="installed${st:+, service $st}"
    else st="not installed yet"; fi
    printf '\n\033[1;34m  SpiderMap %s\033[0m  -  %s\n' "${v:-?}" "$st"
    cat <<'EOM'
  Nothing touches the web unnoticed.

   1) Install             first install, or run the setup again (repair)
   2) Update              a newer version on GitHub? asks first, then downloads and installs it
   3) Check everything    every problem, with its fix (spidermap doctor)
   4) Zabbix connection   API address and token
   5) Proxmox             optional: names of your VMs and containers
   6) Fix permissions
   7) Status and logs
   8) Certificate         this server's https certificate, for installing on your devices
   9) Uninstall
   0) Exit
EOM
    if installed; then read -r -p "Choose: " a || exit 0; else read -r -p "Choose [1]: " a || exit 0; a="${a:-1}"; fi
    case "$a" in
      1) do_install; pause ;;
      2) installed && menu_update || echo "  Install first (1)." ;;
      9) do_uninstall ;;
      0|q|Q|"") exit 0 ;;
      3|4|5|6|7|8)
        if ! installed; then echo "  Install first (1)."; continue; fi
        case "$a" in
          3) python3 "$CLIPY" doctor || true ;;
          4) menu_zabbix ;;
          5) menu_proxmox ;;
          6) menu_permissions ;;
          7) python3 "$CLIPY" status || true; echo; python3 "$CLIPY" logs -n 20 || true ;;
          8) python3 "$CLIPY" cert || true ;;
        esac
        pause ;;
      *) echo "  Choose a number from the list." ;;
    esac
  done
}

usage() { sed -n '2,9p' "$DIR/install.sh" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
  --uninstall) shift; do_uninstall "$@" ;;
  --update)    do_update ;;
  --install)   do_install ;;
  --menu)      menu ;;
  -h|--help)   usage ;;
  "")          if [ -t 0 ] && [ -t 1 ]; then menu; else do_install; fi ;;
  *)           fail "Unknown option: $1   (bash install.sh --help)" ;;
esac
