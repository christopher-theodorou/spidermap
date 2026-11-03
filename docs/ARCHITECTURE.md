# SpiderMap architecture

How SpiderMap is put together: what runs when, where every file lives, how the page gets its data, and where to
extend it. Read this before larger changes.

## The big picture

```
 switches (SNMP, LLDP)     Zabbix API          Proxmox API (optional)     internet (ping, speed test)
        │                       │                       │                           │
        └──────────────┬────────┴───────────────────────┴───────────────────────────┘
                       ▼
        spidermap.py  (runs as root on the Zabbix server / container)
          ├── full scan      cron, every 5 min    topology, devices, changes, presence
          ├── live refresh   cron, every minute   port status, stats, cable / traffic checks, warnings
          └── real time      spidermap-rt service port counters every 5 s, latency every 15 s, speed tests
                       │
                       ▼  JSON files
        /var/lib/spidermap/www/          (root + web server group only, NOT in the web folder)
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
| `spidermap.py` | Everything that collects and decides: full scan, live refresh, real-time loop, speed tests, events, Zabbix integration, installation of the page and the gateway. Also contains the PHP gateway source (`GATEWAY_PHP`). |
| `spidermap_ports.py` | Reads each switch over SNMP: interfaces, status, speed, LLDP neighbours, MAC tables, ARP. Ping sweep, reverse DNS, default gateway. |
| `spidermap_zabbix.py` | Zabbix API client, host loading, SNMP credentials from Zabbix, LLDP helpers, dashboard builder. Also the *classic* Zabbix map (`python3 spidermap_zabbix.py`). |
| `spidermap.html` | The whole user interface in one file: tree layout, panels, gateway card, activity feed, editing. No build step. |
| `install.sh` | Installer and updater (`--update`). Packages, Ookla CLI, migration from older installs, cron jobs, checks. |
| `tests/test_gateway.py` | Security tests for `spidermap.php` (needs `php-cli`). |
| `tests/test_core.py` | Tests for scheduling, edits and job locking. |

## What runs when

| Job | Started by | Interval | Does |
|---|---|---|---|
| Full scan | cron | 5 min | Ping sweep, reads all switches, builds the topology, tracks devices (new / moved / IP changes / conflicts), presence, public IP, writes `spidermap.json`, installs the page and the gateway, sends Zabbix events |
| Live refresh | cron (`--live`) | 1 min | Port status / traffic / errors from Zabbix, CPU / memory / disk stats, pings important devices, cable and unusual-traffic checks, warnings to Zabbix, writes `spidermap_live.json` |
| Real time | systemd `spidermap-rt` (`--rt`) | 5 s | SNMP port counters → live traffic, port up/down and flap counting, data usage per port, latency / loss / jitter (every 15 s), outages, speed test requests, writes `spidermap_rt.json` |

Cron output goes to `/var/log/spidermap.log`; the service logs to the journal (`journalctl -u spidermap-rt`).
Only one full scan and one live refresh run at a time: cron skips a turn if the previous run is still busy.

## Where files live

**Repository folder** (`/opt/spidermap`, root only): the code, plus private files that are never committed
(see `.gitignore`):

| File | Contents |
|---|---|
| `zabbix.env` | Zabbix API address and token, optional Proxmox token (mode 600) |
| `devices.json` | Your names, types, pins, web addresses, important devices, settings |
| `spidermap_state.json` | Device memory: first/last seen, location, IP history, flags, port speeds, public IP |
| `spidermap_hist.json` | Minute history for graphs, cable / traffic baselines and previous warnings |
| `spidermap_events_all.json` | Activity feed, 60 days |
| `spidermap_presence_all.json` | Online intervals per device, 30 days |
| `spidermap_inet.json` | Outages, packet-loss state |
| `spidermap_usage.json` | Bytes per port, today and this month |
| `spidermap_schedule.json` | Date of the last scheduled speed test |
| `.spidermap-scan.lock`, `.spidermap-live.lock` | Make sure only one scan and one live refresh run at a time |

**`/var/lib/spidermap/`** (root, mode 755):

| Path | Owner / mode | Contents |
|---|---|---|
| `www/` | root:www-data 750, files 640 | What the browser gets: `spidermap.html`, `spidermap.json`, `spidermap_live.json`, `spidermap_rt.json`, `spidermap_events.json`, `spidermap_presence.json` |
| `inbox/` | www-data 700 | The only place the web server can write: `edits.json`, `speedtest.request`, `sess/` (login check cache) |
| `speedtest.json` | root | Speed test history |
| `backup/` | root | Files replaced during upgrades (e.g. an old `spider-secure.php`) |

**Zabbix web folder** (e.g. `/usr/share/zabbix/ui`): `spidermap.php`, `spidermap-assets/` (icons only), and
redirects from old addresses (`spidermap.html`, `netmap.html`, `spider-secure.php`).

**Elsewhere:** `/etc/systemd/system/spidermap-rt.service`, `/usr/local/bin/ookla-speedtest`, root's crontab.

## How the page gets its data

`spidermap.php` handles three kinds of request:

| Request | Who | Returns |
|---|---|---|
| `GET spidermap.php` | `view_min_user_type` or higher | The page |
| `GET spidermap.php?data=map\|live\|rt\|events\|presence` | same | One JSON file from `/var/lib/spidermap/www` |
| `POST spidermap.php` | Zabbix admins, same-origin, header `X-SpiderMap: 1` | Device edits (`{mac, name, type, url, pin, important}`) or `{action: "speedtest"}` |

The login check reads the Zabbix session cookie and asks the Zabbix API (`user.checkAuthentication`, with
`extend: false`). The answer is cached for 10 seconds per session in `inbox/sess/`. Edits are written to
`inbox/edits.json` and merged into `devices.json` by the next live refresh (within a minute). Both sides lock
the file (`flock`) while they use it, so no edit is lost.

The page polls: map every 60 s, live data every 30 s, real-time data every 5 s, events every 20 s, presence every
5 min. When the data reports a newer `page_version`, the page reloads itself.

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

## Compatibility

| | Status |
|---|---|
| Zabbix | 7.0+ required (uses `history.push`, map link indicators). Tested on 7.4. The gateway itself also understands older session cookies. |
| PHP | 7.4+ (tested with 8.2 and 8.3) |
| OS | Debian / Ubuntu with systemd and cron (incl. Proxmox LXC) |
| Web server | Apache tested; nginx should work (no special rules needed) |
| Switches | Huawei VRP tested; standard IF-MIB, BRIDGE-MIB / Q-BRIDGE-MIB and LLDP-MIB expected |

Known limitations: switches that keep MAC tables per VLAN community (e.g. some Cisco models) need a change in
`spidermap_ports.py`; Wi-Fi clients behind a router are grouped under the gateway (no per-client Wi-Fi data).
