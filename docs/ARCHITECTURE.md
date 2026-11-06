# SpiderMap architecture

How SpiderMap is put together: what runs when, where every file lives, how the page gets its data, and where to
extend it. Read this before larger changes.

## The big picture

```
 switches (SNMP, LLDP)     Zabbix API          Proxmox API (optional)     internet (ping, speed test)
        │                       │                       │                           │
        └──────────────┬────────┴───────────────────────┴───────────────────────────┘
                       ▼
        spidermap.service  (systemd, user "spidermap", only CAP_NET_RAW for ping)
          ├── scheduler      starts the full scan and the status refresh as separate processes
          │     ├── full scan      every 5 min (Settings)   topology, devices, changes, presence
          │     └── status refresh every 60 s (Settings)    port status, stats, cable / traffic checks, warnings
          ├── real time      every 5 s (Settings)        port counters, latency every 15 s, speed tests
          └── requests       from the page               Scan now, Cancel, Pause, settings, import, restore
                       │
                       ▼  JSON files
        /var/lib/spidermap/www/          (service user + web server group only, NOT in the web folder)
                       │
                       ▼
        spidermap.php  (Zabbix web folder)  ── checks the Zabbix login through the public Zabbix API
                       │
                       ▼
        spidermap.html in the browser (dashboard widget or direct), polls the data through spidermap.php
```

SpiderMap only **reads** from the network and from Zabbix. It never changes switch configuration. In Zabbix it
creates only its own items, triggers and dashboard (see *Zabbix integration*).

## Components

| File | Role |
|---|---|
| `spidermap.py` | Everything that collects and decides: the service and its scheduler, full scan, status refresh, real-time loop, speed tests, events, settings, Zabbix integration, installation. Also contains the PHP gateway source (`GATEWAY_PHP`). |
| `spidermap_ports.py` | Reads each switch over SNMP: interfaces, status, speed, LLDP neighbours, MAC tables, ARP. Ping sweep, reverse DNS, default gateway. |
| `spidermap_zabbix.py` | Where files live (`CONF_DIR`, `STATE_DIR`), reading `zabbix.env`, Zabbix API client, host loading, SNMP credentials, LLDP helpers, dashboard builder. Also the *classic* Zabbix map. |
| `spidermap.html` | The whole user interface in one file: tree layout, panels, gateway card, activity feed, editing, Settings, Collector health. No build step. |
| `install.sh` | Installer and updater (`--update`): packages, Ookla CLI, service user, moving files to their places, the service, checks. |
| `tests/test_gateway.py` | Security tests for `spidermap.php` (needs `php-cli`). |
| `tests/test_core.py` | Tests for the scheduler, settings, edits, audit, SNMP credentials, permissions. |
| `tests/test_ports.py` | Tests for port names and traffic counters with real switch tables. |

## What runs when

Everything runs in one systemd service, **`spidermap`** (`spidermap.py --service`). Its scheduler starts the jobs as
separate processes, one of each kind at a time, with a time limit:

| Job | Interval (Settings) | Time limit | Does |
|---|---|---|---|
| Full scan (`--quiet`) | 5 min; not while discovery is paused | 15 min | Ping sweep, reads all switches, builds the topology, tracks devices (new / moved / IP changes / conflicts), presence, public IP, writes `spidermap.json`, sends Zabbix events |
| Status refresh (`--live`) | 60 s | 3 min | Port status / traffic / errors from Zabbix, CPU / memory / disk stats, pings important devices, cable and unusual-traffic checks, warnings to Zabbix, writes `spidermap_live.json` |
| Real time (in the service) | 5 s | - | SNMP port counters → live traffic, port up/down and flap counting, data usage per port, latency / loss / jitter (every 15 s), outages, speed tests, writes `spidermap_rt.json` |

The scheduler remembers its jobs in `spidermap_jobs.json`: after a restart nothing runs again before its interval,
and a job that was running is marked *interrupted*. It writes `spidermap_status.json` every cycle (jobs, pause,
health) for the page. Job output goes to `/var/lib/spidermap/spidermap.log` (2 MB + one older file); the service
itself logs to the journal (`journalctl -u spidermap`).

**VLANs and link aggregation** come from Q-BRIDGE-MIB (`dot1qVlanStaticName`, `dot1qVlanStaticEgressPorts` /
`UntaggedPorts` read as hex bitmaps, `dot1qPvid`, `dot1qVlanFdbId`; the VLAN of a device is the fdb id in
`dot1qTpFdbPort`) and IEEE8023-LAG-MIB (`dot3adAggPortAttachedAggID`), in `scan_switch()` and `vlan_tables()`.
Both are optional: a switch without them is scanned as before.

**Device types:** `DEVICE_CATEGORIES` in `spidermap.py` (key, type, title, examples, how it is detected, regex on
name / DNS name / manufacturer) is checked before the older `CLASSIFY` rules; categories in `device_types_off` are
skipped. The Settings > Device types tab is built from it.

**Uninstall:** `install.sh --uninstall` drives it; `spidermap.py --uninstall-zabbix` / `--uninstall-web`
(with `--dry-run`) remove only `autotopo.*` items, SpiderMap's dashboards and map, and web files that carry
`REDIRECT_MARK` or are SpiderMap's gateway.

**Internet and throughput.** Every 15 s the service pings Cloudflare, Google and Quad9; when all fail it checks
again at once before recording an outage (`inet_update()`). Time with no check for over 2 minutes (service stopped,
server off) is recorded as a gap (`inet_start()`), and `month_uptime()` counts uptime over the monitored time only.
Throughput comes from the router's WAN counters over UPnP when it answers (`read_upnp()`: SSDP, then
WANCommonInterfaceConfig, only from the default gateway), otherwise from the switch port to the gateway.

**Map reset** (`"reset"` in a settings request) moves the history files to `state/reset-<time>/` (the last 3 are
kept), removes the page data, and restarts the service with a clean memory; the first scan starts right away.

**A switch that does not answer** keeps its last good data for 30 minutes (from `switch_cache` in the state), so its
devices do not look offline; the map is marked *partial* and Collector health names the switch and its error.

## Running without root

The installer creates the system user `spidermap` and runs everything as that user if `ping` works for it (`ping`
has the file capability `cap_net_raw` on Debian; the service also gets `CAP_NET_RAW` as its only capability).
Otherwise it stays on root and says why. The choice is in `/etc/spidermap/run-as`.

When root runs a SpiderMap command (a scan, `--live`, `--check-ports`), `drop_privileges()` switches to the service
user first, so root never leaves files the service cannot write. Only `--setup`, `--install-web` and
`--install-service` keep root: they write the Zabbix web folder and the systemd unit. `fix_ownership()` sets every
owner and mode after them.

The unit also uses `ProtectSystem=strict` (only `/var/lib/spidermap` and `/etc/spidermap` writable),
`ProtectHome`, `PrivateTmp`, `NoNewPrivileges` and `UMask=0027`. If a container does not support these, the
installer restarts the service without the file-system protection and says so.

## Where files live

**Code** (`/opt/spidermap`, a git checkout, root): the program. `zabbix.env` and `devices.json` there are links to
`/etc/spidermap`, so `cd /opt/spidermap && nano devices.json` keeps working. (Before the installer has moved them,
everything is read from the code folder; `SPIDERMAP_CONF` / `SPIDERMAP_STATE` override both.)

**Configuration** (`/etc/spidermap`, service user, 750):

| File | Owner / mode | Contents |
|---|---|---|
| `zabbix.env` | root:spidermap 640 | Zabbix API address and token, optional Proxmox token and certificate fingerprint. Read automatically |
| `devices.json` | spidermap 640 | Your names, types, pins, web addresses, important devices, settings (also changed from the Settings panel) |
| `run-as` | root:spidermap 640 | `spidermap` or `root` |

**History** (`/var/lib/spidermap/state`, service user, 700):

| File | Contents |
|---|---|
| `spidermap_state.json` | Device memory: first/last seen, location, IP history, flags, port speeds, public IP, last good data per switch |
| `spidermap_hist.json` | Minute history for graphs, cable / traffic baselines and previous warnings |
| `spidermap_events_all.json` | Activity feed (60 days, Settings) |
| `spidermap_presence_all.json` | Online intervals per device (30 days, Settings) |
| `spidermap_inet.json` | Outages, packet-loss state |
| `spidermap_usage.json` | Bytes per port, today and this month, and since when it counts (`since`) |
| `spidermap_schedule.json` | Date of the last scheduled speed test |
| `spidermap_jobs.json` | The scheduler's jobs and the last 30 runs |
| `spidermap_scan_report.json` | The last full scan: each switch answered or not, warnings |
| `config-history/` | The last 10 versions of `devices.json`, saved before each change from the page (Restore) |
| `.spidermap-*.lock` | Only one scan and one refresh at a time, also when run by hand |

**`/var/lib/spidermap/`** (service user, 755):

| Path | Owner / mode | Contents |
|---|---|---|
| `www/` | spidermap:www-data 2750, files 640 | What the browser gets: the page, `spidermap.json`, `_live`, `_rt`, `_events`, `_presence`, `_status`, `_settings`, and `access.json` (permissions read by `spidermap.php`) |
| `inbox/` | www-data:spidermap 2770, files 660 | The only place the web server can write: `edits.json`, `speedtest.request`, `scan.request`, `scan.cancel`, `settings.request`, `sess/` (login check cache, 700) |
| `speedtest.json` | spidermap | Speed test history |
| `spidermap.log` | spidermap, 600 | Output of the scans and refreshes |
| `backup/` | root | Files replaced during upgrades (e.g. an old `spider-secure.php`) |
| `audit.log` | spidermap, 600 | Who changed what from the map, settings, scans and speed tests (JSON lines, ~1 MB + one older file) |
| `snmp/<id>/snmp.conf` | spidermap, 700 / 600 | SNMP credentials per switch, given to net-snmp via `SNMPCONFPATH` instead of the command line |

**Zabbix web folder** (e.g. `/usr/share/zabbix/ui`): `spidermap.php`, `spidermap-assets/` (icons only), and
redirects from old addresses (`spidermap.html`, `netmap.html`, `spider-secure.php`).

**Elsewhere:** `/etc/systemd/system/spidermap.service`, `/usr/local/bin/ookla-speedtest`. No cron jobs since 0.5.0.

## How the page gets its data

`spidermap.php` handles three kinds of request:

| Request | Who | Returns |
|---|---|---|
| `GET spidermap.php` | `permissions.view` or higher | The page |
| `GET spidermap.php?data=map\|live\|rt\|events\|presence` | same | One JSON file from `/var/lib/spidermap/www` |
| `GET spidermap.php?data=me` | same | The user's name and what they may do (`can.edit`, `can.speedtest`, `can.settings`), to grey out buttons |
| `GET spidermap.php?data=status` | same | Jobs, pause, Collector health |
| `GET spidermap.php?data=settings` | `permissions.settings` | The settings form (schema and values), devices, saved versions |
| `POST spidermap.php` | `permissions.edit` / `.speedtest` / `.settings`, same-origin, header `X-SpiderMap: 1`, at most 4 MB | Device edits (`{mac, name, type, url, pin, important}`), or `{action}`: `speedtest`, `scan`, `cancel`, `pause`, `resume`, `settings` (`{settings}`), `import` (`{import: {settings, devices}}`), `restore` (`{restore: id}`), with an optional `rid` |

The minimum Zabbix user types come from `"permissions"` in `devices.json` (`permissions()`): written into
`spidermap.php` as defaults by `gateway_code()`, and into `www/access.json` by the service whenever they change, which
`spidermap.php` reads at every request (so a change applies at once, without root). The lower limits are enforced in
both places. The tests build the gateway with the same function.

**Changes go through the service.** `spidermap.php` only writes a request file (with the Zabbix user and the page's
`rid`) into the inbox; the service validates it again (`validate_settings()`, `validate_devices()`), keeps the previous
`devices.json` in `config-history/`, writes the audit log and puts the result in `spidermap_status.json`
(`config.result`, matched by `rid`), which the page shows.

## Settings

`SETTINGS_SCHEMA` in `spidermap.py` is the one list of settings that can be changed from the page. Each entry
(`_S(...)`) has a key, type (`bool`, `int`, `choice`, `str`, `time`, `digits`, `cidrs`, `ips`, `level`), default,
tab, group, label, and optionally a range, unit, drop-down choices (a third element `"planned"` shows an option that
cannot be chosen yet), help text, `preview` (shown, not active: "v0.6") or `locked` (always the default). It drives
the validation (`validate_settings()`), the defaults (`effective_settings()`) and the Settings dialog, which builds its
tabs and fields from it. To add a setting: add an entry, use it with `setting("key")` or `current_settings()`, and
add a test. Settings that change how the map looks (`DISPLAY_KEYS`) are passed to every viewer in
`spidermap_status.json`; the scan's progress comes from `spidermap_scan_progress.json` in the state folder, and the
scan log is one file per day (`spidermap.log.YYYY-MM-DD`, kept for `log_keep_days`).
Other keys in `devices.json` (`port_media`, `aliases`, `manual_links`, `core_switch`) are kept as they are; an import
may set them, with a type check.

The login check reads the Zabbix session cookie and asks the Zabbix API (`user.checkAuthentication`, with
`extend: false`). The answer is cached for 10 seconds per session in `inbox/sess/`. Edits are written to
`inbox/edits.json` and merged into `devices.json` by the next live refresh (within a minute). Both sides lock
the file (`flock`) while they use it, so no edit is lost.

The page polls: map every 60 s, live data every 30 s, real-time data every 5 s, events every 20 s, presence every
5 min. When the data reports a newer `page_version`, the page reloads itself.

## Ports and traffic

**Identity.** A port is identified by its switch (Zabbix host ID) and its **ifIndex**, everywhere: `port_ref` in the
map data, live and real-time data, history, usage totals, cable and traffic checks. In the page, a port's `id` is the
ifIndex; `n` and `label` are only for display.

**Which interfaces are ports** (`iface_kind()` in `spidermap_ports.py`): Ethernet types (ifType 6, 117) and link
aggregations (161, or names like `Port-channel`, `Eth-Trunk`, `bond`), minus virtual interfaces by name (VLAN
interfaces, bridges, loopback, management ports, tunnels) - some switches report those as Ethernet.

**Labels** (`label_ports()` in `spidermap.py`), the same for every vendor:

| Switch | Labels |
|---|---|
| One kind of port with unique numbers (`GigabitEthernet0/0/1-24`, `ether1-8`, `Gi1/0/1-48`) | the device's own numbers: Port 1..N |
| Several kinds (`ether1-8` + `sfp-sfpplus1-2`, `Fa0/1-24` + `Gi0/1-2`) | in device (ifIndex) order: Port 1..N |
| Stack (`Gi1/0/x`, `Gi2/0/x`) | Port 1/x, Port 2/x |
| Link aggregation | LAG 1..N |

If two ports would still get the same label, the interface name is used instead. Pins in `devices.json`
(`"SW-Office:5"`) refer to these labels.

**Pluggable ports** get their type in the label (`Port 9 (SFP+)`) and their own group on the front panel. Sources,
in order: `port_media` in `devices.json`, the interface name when it says so (`media_from_text()`), the device's
hardware inventory (`entity_media()`: ENTITY-MIB port descriptions mapped to ifIndex). Nothing is guessed from
speed or from names like `ten-gigabitEthernet`, which are used for SFP+ and for 10GBASE-T copper alike.

**Counters** (`counter_rate()`): ifHCInOctets / ifHCOutOctets as exact integers, every 5 s, with a monotonic clock.
A sample is not used when the device restarted (sysUpTime went back), ifCounterDiscontinuityTime changed, a 64-bit
counter went back, or the rate is above the link speed (+5 %). Such samples are counted in `quality` in
`spidermap_rt.json` and shown with the port. Devices without 64-bit counters fall back to ifInOctets / ifOutOctets
(32-bit, wrap-around handled).

**Zabbix items** are matched by the ifIndex in the item key (`net.if.in[ifHCInOctets.<ifIndex>]`), with the
interface name in the item name as a fallback.

**Check a switch:** `python3 spidermap.py --check-ports` lists every interface, its label or why it is not a port.

## Zabbix integration

SpiderMap uses the API token in `zabbix.env` and creates, on the Zabbix server host:

| Item key | Trigger |
|---|---|
| `autotopo.new` | New device(s), Warning |
| `autotopo.changed` | Device moved or changed IP, Information |
| `autotopo.down` | Important device(s) down, High |
| `autotopo.warnings` | Active warnings (cable, traffic, IP conflict, packet loss, internet down), Warning |
| `autotopo.speed.down`, `.up`, `.ping`, `.load` | (values only, for graphs) |

The item keys keep their historical `autotopo.` prefix so existing history is not lost. Values are sent with
`history.push`. SpiderMap also creates the dashboard **SpiderMap** with a URL widget pointing to `spidermap.php`.

## Extending SpiderMap

| To add | Change |
|---|---|
| A device type / icon | `TYPE_ICON` in `spidermap.py`, `TYPES` in `spidermap.html` |
| Automatic recognition | `CLASSIFY` (regex on name and manufacturer) and `EXTRA_OUI` in `spidermap.py` |
| Web UI port of an app | `APP_URLS` in `spidermap.py` |
| A setting | `DEFAULT_DEVICES["settings"]`, `devices.example.json`, the README settings table |
| A new data file for the page | Write it to `DATA_DIR`, add it to `FILES` in `GATEWAY_PHP`, load it in the page with `getData()` |
| A new kind of change from the page | Handle it in the POST part of `GATEWAY_PHP` (admins only, validate every field), process it in `spidermap.py`, and add test cases to `tests/test_gateway.py` |

Rules that keep SpiderMap safe:
- Nothing with network data in the web folder. Data goes to `DATA_DIR` via `save_json()`.
- Everything the page shows is escaped (`esc()` in `spidermap.html`). Never build HTML from data without it.
- Every new POST action: admins only, validated input, covered by a test.
- Read-only towards the network: no SNMP writes, no configuration changes on devices.
- Never put a secret on a command line: SNMP credentials go through `snmp_auth()` (private snmp.conf).

## Compatibility

| | Status |
|---|---|
| Zabbix | 7.0+ required (uses `history.push`, map link indicators). Tested on 7.4. The gateway itself also understands older session cookies. |
| PHP | 7.4+ (tested with 8.2 and 8.3) |
| OS | Debian / Ubuntu with systemd (incl. Proxmox LXC) |
| Web server | Apache tested; nginx should work (no special rules needed) |
| Switches | Huawei VRP in use; port tables of MikroTik RouterOS, TP-Link Omada and Cisco tested; standard IF-MIB, BRIDGE-MIB / Q-BRIDGE-MIB and LLDP-MIB expected |

Known limitations: switches that keep MAC tables per VLAN community (e.g. some Cisco models) need a change in
`spidermap_ports.py`; Wi-Fi clients behind a router are grouped under the gateway (no per-client Wi-Fi data).
