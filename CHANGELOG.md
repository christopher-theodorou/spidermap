# Changelog

All notable changes to SpiderMap. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/): while SpiderMap is `0.x`, a new **minor** version
(0.3.0) adds features, a **patch** version (0.2.3) only fixes things.

## [0.5.0] - 2026-11-05

**Settings on the map, a scheduler instead of cron, and SpiderMap no longer runs as root** (review phases 2 and 3;
this includes what was planned as 0.4.1).

### Added
- **Settings dialog** (the gear button; Zabbix Super admins by default, `permissions.settings`), designed after the
  "SpiderMap v0.5.0 - Settings" artifact: tabs *General, Scans, SNMP, Topology, Wireless & WAN, Device types,
  Measurements, Security, Logging*, toggles, networks as chips, a collector status card with scan progress, Cancel and
  Save with an unsaved-changes indicator. Every value is checked in the browser and again on the server; nothing is
  written when a value is wrong.
  - *General:* map name, map refresh, show offline devices, IP labels on nodes, how long devices stay marked.
  - *Scans:* automatic scan, discovery via ICMP, SNMP collection, full scan and traffic intervals, ICMP payload size,
    timeout, parallel checks, allowed networks and addresses never to scan.
  - *SNMP:* max-repetitions (GETBULK), retries, timeout. The credentials stay on each switch's SNMP interface in
    Zabbix; the tab shows what each switch uses, never a password.
  - *Topology:* link sources LLDP, MAC tables and ARP tables.
  - *Measurements:* link thickness by traffic, units (bits/s, bytes/s, % of capacity), load warning and critical
    (links turn amber / red), status refresh, history, the daily speed test.
  - *Security:* scan only in an off-peak window, a packet-per-second limit for the ping sweep, the address limit,
    and who may open SpiderMap, change devices, start speed tests and change settings.
  - *Logging:* log level (errors only up to debug, which also logs every SNMP command) and how many days of logs to
    keep (one file per day), plus recent events.
  - *Wireless & WAN* and *Device types* are shown as a preview of 0.6.0, as in the design; nothing there is active yet,
    and neither are CDP, other layouts and the link with Zabbix triggers (marked "v0.6").
- **Scan now, Cancel, Pause scans,** with the progress of the running scan. A second click does not start a second
  scan; paused scans keep the last map (traffic and status keep updating).
- **Restore, Export, Import:** the last 10 versions of the settings and devices are kept, one click brings one back;
  export to a file and import it elsewhere, with a preview before anything changes.
- **Collector health:** the service, the last scans and their errors, the Zabbix API, every switch (answering or not),
  the age of the data, free disk space, the speed test program. The header shows "Scanning… 34 s", "Discovery paused",
  or a failed last scan.
- Every settings change, scan start and cancel is in the audit log and the activity feed.

### Changed
- **One service, `spidermap`, runs everything** - no more cron jobs and no `spidermap-rt`. Its scheduler starts the
  scan and the status refresh as separate processes, with time limits (15 and 3 min), and remembers them across
  restarts (nothing runs twice after a restart).
- **SpiderMap runs as the system user `spidermap`**, with only the capability ping needs (`CAP_NET_RAW`), read-only
  access to the rest of the system, and `NoNewPrivileges`. The installer checks that ping works for that user first;
  otherwise it stays on root and says why. Root running a SpiderMap command switches to that user automatically.
- **Files have their own places:** configuration in `/etc/spidermap` (links remain in `/opt/spidermap`), history in
  `/var/lib/spidermap/state`, the scan log in `/var/lib/spidermap/spidermap.log`. `zabbix.env` is read automatically
  (`. ./zabbix.env` is no longer needed).
- **A switch that does not answer keeps its last good data for 30 minutes**, so its devices do not look offline; the
  map is marked *partial* and Collector health names the switch and its error.
- The page asks for nothing while its browser tab is hidden, and explains what is wrong instead of "Loading…".

### Fixed
- **A scan stopped with an error** when a device moved from a place behind another device (for example a Proxmox
  guest, or every device behind a switch that stopped answering). Present since 0.1.0.
- The installer could, in rare cases, empty root's crontab while removing its own lines; it now reads first, then
  writes, and keeps every other job.
- A damaged activity history no longer stops SpiderMap; it starts a new one.

### Upgrade notes
- `bash install.sh --update` does everything: creates the user, moves your files (nothing is deleted; old files that
  were already in the new place are kept as `*.old-<time>`), removes SpiderMap's cron lines and the `spidermap-rt`
  service, and starts `spidermap`. Check with `systemctl status spidermap` and the new **Collector health** panel.
- Commands change: `systemctl status spidermap`, `journalctl -u spidermap`; the scan log is
  `/var/lib/spidermap/spidermap.log`.
- To keep SpiderMap on root: `SPIDERMAP_RUN_AS=root bash install.sh --update`.

## [0.4.0] - 2026-11-05

**Hardening**, from the review by Nikolaos Karanikolas (phase 2). Nothing changes for you unless you set the new
options; defaults behave as before.

### Added
- **Separate permissions** for viewing, editing devices and starting speed tests: `"permissions": {"view": 3,
  "edit": 2, "speedtest": 2}` in `devices.json` (lowest Zabbix user type for each). Edits are never allowed below
  Zabbix admin, and nobody may do more than view. Buttons a user may not use are greyed out with the reason.
- **Audit log:** every change from the map (name, type, web address, pin, important) and every speed test is
  recorded with the Zabbix user and the time in `/var/lib/spidermap/audit.log` (root only) and in the activity feed
  under the new *Changes* filter.
- **Proxmox certificate check:** `PVE_FINGERPRINT` (works with Proxmox's self-signed certificate), `PVE_CA` or
  `PVE_VERIFY=1` in `zabbix.env`. Without them SpiderMap connects as before and logs a warning.
- README: how to give SpiderMap its own limited Zabbix account (role with an API allow list, read-only host groups).

### Changed
- **SNMP passwords and communities are no longer on the command line**, where any local user could read them
  (`ps`). They are passed to net-snmp in a private file per switch, in `/var/lib/spidermap/snmp` (700 / 600).
- The certificate setting for Proxmox can no longer be overridden by `REQUESTS_CA_BUNDLE` in the environment.
- `"view_min_user_type"` still works; `"permissions"` is the new way to set it.

### Upgrade notes
- Nothing to do. Optional: add `PVE_FINGERPRINT` to `zabbix.env` if you use the Proxmox integration, and set
  `"permissions"` if you want to let other Zabbix users look or edit.

## [0.3.1] - 2026-11-04

### Changed
- **Gateway card in the style of a UniFi gateway card:** gateway IP and ping, then the internet provider with this
  month's uptime (green, amber or red), public IP, monthly data usage, throughput with graph and latency tiles.
- **"ISP Speed Test" button at the bottom of the card, always visible:** when the card is taller than the window,
  the content scrolls and the button stays in place. It opens the new **speed test panel** (also from the toolbar):
  latest result, latency under load and bufferbloat, run again, and the history.

### Fixed
- **Switches appeared a second time as devices** under "Not placed yet" (their own management address was
  treated as an unknown device). A switch's own address is no longer a device.
- **A horizontal scrollbar** appeared at the bottom of the page (the closed side panel made the page wider than the
  window).

## [0.3.0] - 2026-11-03

**Correct traffic numbers on every switch.** From the review by Nikolaos Karanikolas, confirmed with the interface
tables of his MikroTik CRS310 / CRS112 and TP-Link Omada SG3218XP-M2 / SG2206MP.

### Fixed
- **Impossible speeds and data volumes** on switches where two interfaces end in the same number
  (`ether1` + `sfp-sfpplus1` on MikroTik, `Vlan-interface1` + port 1 on TP-Link, stacked or mixed-speed switches).
  SpiderMap identified a port by the digits at the end of its name, so two interfaces shared one counter baseline:
  every 5 seconds a port's total traffic since boot was counted as new traffic. Ports are now identified by their
  **ifIndex** everywhere (traffic, history, totals, cable and traffic checks, the page).
- **No Zabbix data for TP-Link ports**: interface names with spaces (`two-gigabitEthernet 1/0/1`) were not
  recognised. Zabbix items are now matched by the ifIndex in the item key, so port names and languages do not matter.
- `Vlan-interface1` and other virtual interfaces that some switches report as Ethernet are no longer shown as ports.

### Changed
- **One port naming for every vendor:** `Port 1 … Port N` in the order of the device, also for SFP ports
  (e.g. MikroTik `ether1-8` + `sfp-sfpplus1-2` → Port 1-10). Switches with one kind of port keep their own numbers
  (Huawei `GigabitEthernet0/0/5` is still Port 5). Stacks: `Port 2/5`. Link aggregations: `LAG 1`.
  The device's own interface name is shown in the switch panel.
- **Pluggable ports are marked**: `Port 9 (SFP+)`, also in the activity feed, and shown as a separate group on the
  switch's front panel. The type comes from the interface name when it says so (`sfp`, `sfpplus`, `qsfp`), the
  device's hardware inventory (ENTITY-MIB), or the new `port_media` setting. SpiderMap does not guess: names like
  `ten-gigabitEthernet` or `Te1/1/1` can be SFP+ or 10GBASE-T copper, so they stay `Port N` unless one of these says
  otherwise.
- **Counters follow RFC 2863:** exact integers, a monotonic clock, and a sample is ignored when the device restarted
  (`sysUpTime`), says its counters are not comparable (`ifCounterDiscontinuityTime`), a counter went back, or the
  traffic would be more than the link can carry. Ignored samples are counted and shown with the port.
- Devices without 64-bit counters (SNMPv1, some older switches) use 32-bit counters, with wrap-around handled.

### Added
- `python3 spidermap.py --check-ports`: shows, read-only, every interface of every switch, its SpiderMap name or why
  it is not a port, and whether it was affected by the old numbering.
- `tests/test_ports.py`: 33 checks with real switch tables (MikroTik, TP-Link, Huawei, Cisco).

### Upgrade notes
- **Data usage totals restart** ("since …" is shown), because totals measured before 0.3.0 can be wrong. The old
  totals are kept in `spidermap_usage.before-0.3.0.json`.
- Per-port graphs restart, and the normal-traffic baselines for *unusual traffic* are learned again (24 hours).
- On switches without the problem (e.g. Huawei with only GigabitEthernet ports) the map looks exactly the same.

## [0.2.3] - 2026-11-03

Bug fixes from the review by Nikolaos Karanikolas (findings F08-F11). Nothing else changes.

### Fixed
- **Gateway recovery was never reported** (F10). "Gateway is not responding" appeared in the activity feed, but
  "Gateway is responding again" never did.
- **The daily speed test could run twice** (F11). The date of the last scheduled test is now kept on disk
  (`spidermap_schedule.json`), so restarting the service no longer repeats it. Times like `3:30` now work like `03:30`.
- **An edit could be lost** (F09) when it was saved from the map at the exact moment SpiderMap read the edits.
  SpiderMap now reads and clears them under the same lock the page uses when writing.
- **Two scans could run at the same time** (F08) when one took longer than 5 minutes. Now only one scan and one
  live refresh run at a time; cron skips a turn instead of starting a second copy, and `install.sh --update` waits
  for a running scan before installing the new version.
- The "Open web UI" button only opens `http://` and `https://` addresses (the server already refused others).

### Added
- `tests/test_core.py`: tests for these fixes (19 checks, no network needed).

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

[0.5.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.5.0
[0.4.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.4.0
[0.3.1]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.3.1
[0.3.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.3.0
[0.2.3]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.2.3
[0.2.2]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.2.2
[0.2.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.2.0
[0.1.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.1.0
