# Changelog

All notable changes to SpiderMap. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/): while SpiderMap is `0.x`, a new **minor** version
(0.3.0) adds features, a **patch** version (0.2.3) only fixes things.

## [0.2.2] - 2026-11-03

**Security - please upgrade.** One protected entry point for the page and the data.

### Changed
- **Single entry point `spidermap.php`.** The page, the data and all changes go through it. It replaces
  `spidermap_api.php` (0.2.0) and the separate `spider-secure.php` gateway used on some installs.
- **The page itself now needs a Zabbix login**, not only the data. The page is installed outside the web folder
  (`/var/lib/spidermap/www/spidermap.html`) and served by `spidermap.php` after the login check.
- **Zabbix Super admins only by default** (`"view_min_user_type": 3`), because the map shows the whole network.
  Set 1 (any Zabbix user) or 2 (admins) in `devices.json` to open it up.
- Session checks are cached for 10 seconds (was 60).
- The installer warns when a web server rule blocks `spidermap*.php` or `*.html`, and shows the fix.

### Added
- Browser hardening on the page: `Content-Security-Policy`, `X-Frame-Options: SAMEORIGIN`, no caching.
- Works with the session cookie of older Zabbix versions (`zbx_sessionid`) as well as `zbx_session` (5.4+),
  and with the API's request-body authentication used before Zabbix 6.4.
- The Zabbix API address the installer verified is written into `spidermap.php`, so the login check also works
  when the web server does not listen on 127.0.0.1.
- Version number: `python3 spidermap.py --version`.
- `tests/test_gateway.py`: automated security tests for the gateway (20 checks).
- Documentation: `CHANGELOG.md`, `SECURITY.md`, `docs/ARCHITECTURE.md`, `docs/RELEASING.md`.

### Fixed
- Upgrading to 0.2.0 stopped with `No such file or directory: /var/lib/spidermap/www/...` on the first scan.
  The data folder is now created before the first write. (Prepared as 0.2.1, released as part of 0.2.2.)

### Upgrade notes
- Old addresses keep working: `spidermap.html` and `spider-secure.php` in the web folder become redirects to
  `spidermap.php`. A replaced `spider-secure.php` is backed up to `/var/lib/spidermap/backup/`.
- If a web server rule blocks `spidermap*.(json|php|html)`, change it to block only `spidermap*.json`.

## [0.2.0] - 2026-11-02

**Security - please upgrade.** Until 0.2.0 the map data could be read without a Zabbix login.

### Changed
- Map data moved out of the web folder to `/var/lib/spidermap/www`, served by `spidermap_api.php` only to users
  with a valid Zabbix session. The Zabbix guest account is refused.
- Edits and speed tests: Zabbix admins only, with protection against cross-site request forgery.
- Least-privilege files: the web server can write only to `/var/lib/spidermap/inbox`.
- Speed tests use Ookla's official CLI (accurate on fast lines, nearby server), falling back to `speedtest-cli`.
- The speed test history moved to the Internet panel, as labelled bars; results from the old tool are hidden.

### Added
- Latency under load and a bufferbloat rating (low / medium / high) for every speed test.
- `view_min_user_type` setting.
- Login screen when you are not signed in to Zabbix.

### Fixed
- Ookla's CLI crashed (`basic_string::_M_construct null not valid`) when run by the service without `HOME`.
- The gateway card and side panel no longer hide behind a header that wraps onto two rows.

## [0.1.0] - 2026-11-02

First release as **SpiderMap** (earlier development versions were called *zabbix-autotopo* and *zabbix-netmap*).

### Added
- Automatic topology from LLDP and switch MAC tables: Internet → gateway → switches → devices → Proxmox guests.
- Live port traffic every 5 seconds (`spidermap-rt` service), port speed colours, CPU / memory / disk stats.
- Gateway card: throughput, latency, packet loss, jitter, uptime and outages, monthly usage, speed test.
- Activity feed and Zabbix alerts: new and unknown devices, moved devices, IP changes and conflicts, possible cable
  problems, unusual traffic, internet outages, public IP changes, ports up and down.
- Who's online (presence timeline), top talkers, CSV export, editing from the map, light and dark theme.
- Installer with migration from the earlier *zabbix-netmap* install.

[0.2.2]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.2.2
[0.2.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.2.0
[0.1.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.1.0
