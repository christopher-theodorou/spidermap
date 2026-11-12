#!/bin/sh
set -eu
case "${1:-}" in
  init) exec python3 spidermap_standalone.py ;;
  collector) exec runuser -u spidermap -- python3 spidermap.py --service ;;
  web) exec apache2-foreground ;;
  *) exec "$@" ;;
esac
