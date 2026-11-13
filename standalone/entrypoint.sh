#!/bin/sh
set -eu
case "${1:-}" in
  init)
    [ -n "${SPIDERMAP_ZABBIX_FRONTEND:-}" ] || { echo "Set SPIDERMAP_ZABBIX_FRONTEND in spidermap.env (docs/STANDALONE.md)" >&2; exit 1; }
    exec python3 spidermap_standalone.py ;;
  collector) exec runuser -u spidermap -- python3 spidermap.py --service ;;
  web) exec apache2-foreground ;;
  *) exec "$@" ;;
esac
