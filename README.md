<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/spidermap-dark.png">
    <img alt="SpiderMap - Nothing touches the web unnoticed. Network topology map for Zabbix" src="assets/spidermap-light.png" width="720">
  </picture>
</p>

<p align="center"><a href="https://github.com/christopher-theodorou/spidermap/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/christopher-theodorou/spidermap/actions/workflows/tests.yml/badge.svg"></a></p>

<p align="center">
  <b>A live, beautiful map of your network, built automatically from your switches and Zabbix.</b><br>
  Every device, every port, every change.
</p>

<p align="center">
  <img alt="SpiderMap overview" src="assets/screenshot-overview.png" width="900">
</p>

## What it does

### 🕸️ Maps your network by itself
Reads LLDP, CDP, spanning tree, LACP and the switches' MAC and ARP tables, and draws the real topology: Internet →
gateway → switches → devices → virtual machines. Every link says where it comes from (LLDP, MAC table, your
confirmation, or a dotted *guess*), cables that spanning tree blocks are drawn as redundant links, link aggregations
as one thick link, fibre in its own style. Switches nobody manages are found from the devices behind them, and
links between switches without LLDP are **suggested** from ports that change state - or carry exactly the same
traffic - together. You confirm or reject them.

### ⚡ Live
Port traffic every 5 seconds with animated links, link thickness and load colours, ports by speed and duplex, PoE,
VLANs, CPU, memory and disk for switches, servers and VMs. Nodes that are Zabbix hosts show their Zabbix problems.

### 🌐 Internet health
The router's real WAN throughput (SNMP or UPnP), latency, loss and jitter, uptime and outages this month (with time
nobody was watching shown honestly), public IP changes, and speed tests with Ookla, Cloudflare or Fast.com.

### 🔔 Nothing touches the web unnoticed
An activity feed and Zabbix alerts for new and **unknown devices** (especially on a free switch port), devices that
**move**, IP changes and conflicts, **cable problems** (errors, flapping, a drop to a lower speed, duplex
mismatches), **unusual traffic**, internet outages and important devices going offline.

### 🗂️ Views
The map (tree or free layout, vertical or horizontal, physical or by subnet, with a VLAN filter), an **overview**
of cards, and an **inventory** of models, serial numbers and firmware - with devices of one model running different
firmware marked.

### 📡 Controllers and wireless
TP-Link Omada, UniFi (Site Manager or a local console) and Grandstream GWN, read only and several at once - one
map across vendors: Wi-Fi clients under their access point with SSID and signal, wired clients on their switch port,
mesh links. Links between buildings (Ubiquiti, MikroTik, Cambium, TP-Link, Mimosa radios) drawn as wireless, with
their signal. WAN throughput from the gateway's own Zabbix items - any router or firewall - with several
connections, the active one in a failover, and a 4G / 5G router's or a Starlink's own figures.

### 🎨 Yours
Device types with their own icons, names and web addresses edited on the map, map edits (an unmanaged switch here,
a device connected through another), UniFi access points with their wireless clients, themes (SpiderMap and the
green, blue and red spiders, each light and dark), English and Greek.

<p align="center">
  <img alt="Overview" src="assets/screenshot-dashboard.png" width="440">
  <img alt="Inventory" src="assets/screenshot-inventory.png" width="440">
</p>
<p align="center">
  <img alt="Switch panel" src="assets/screenshot-switch.png" width="300">
  <img alt="Activity in dark mode" src="assets/screenshot-dark.png" width="300">
</p>
<p align="center">
  <img alt="The green, blue and red spider themes" src="assets/screenshot-spiders.png" width="900">
</p>

SpiderMap only **reads** from your network. It never changes your switch configuration.

## Requirements

- **Zabbix 7.0 or newer.** SpiderMap runs on the Zabbix server (or its LXC container), Debian or Ubuntu.
- **Switches in Zabbix with an SNMP interface** and a template that discovers interfaces.
  Port names and traffic are tested with interface tables of Huawei VRP, MikroTik RouterOS, TP-Link Omada and
  Cisco-style switches; other brands using the standard IF-MIB, BRIDGE-MIB and LLDP-MIB should work.
- **LLDP enabled** on the switches (and `lldpd` on Linux and Proxmox hosts so they are recognised).
- A Zabbix **API token** for an admin user: *Users → API tokens → Create API token*.

## Install

```bash
apt install -y git
git clone https://github.com/<you>/spidermap.git /opt/spidermap
cd /opt/spidermap
bash install.sh
```

`bash install.sh` opens a menu: choose **1) Install**. The same menu later updates SpiderMap, checks everything,
changes the Zabbix or Proxmox connection, fixes permissions, shows the logs and the certificate, and uninstalls
(see [docs/COMMANDS.md](docs/COMMANDS.md#the-installer-menu)). For scripts: `bash install.sh --install`.

The installer sets up the packages it needs, asks for your Zabbix API address and token, creates the system user
`spidermap` and starts the **`spidermap` service**, which runs the scans (no cron jobs) and the first scan right away.
Configuration goes to `/etc/spidermap`, data to `/var/lib/spidermap`; `zabbix.env` and `devices.json` in
`/opt/spidermap` are links there. Upgrading from the earlier
*zabbix-netmap* version? The installer finds it and moves your devices, settings and history over.

**One manual step in Zabbix**, for the dashboard widget:
*Administration → General → Other → Security → Iframe sandboxing exceptions*

```
allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-downloads
```

`allow-popups-to-escape-sandbox` makes the device pages you open from the map (*Open web UI*) normal tabs. Without
it they inherit the dashboard's sandbox, and logins on them (UniFi OS, Proxmox, ...) do nothing because the sandbox
blocks their forms. The map itself stays sandboxed.

Then open **Dashboards → SpiderMap**, or the page directly at `http://<zabbix>/zabbix/spidermap.php`
(Zabbix Super admins by default, see [Security](#security)).

## Commands

The installer adds the `spidermap` command (run as root):

```bash
spidermap status              # service, last scan, switches, where to open the map
spidermap doctor              # checks everything; every problem comes with its fix
spidermap logs -f             # follow the scan log   (--errors, --service)
spidermap scan                # scan now, with progress
spidermap test-snmp SW-Core   # does a switch answer SNMP, with LLDP and MAC tables?
spidermap cert                # the https certificate: names, expiry, fingerprint
spidermap help                # all of them
```

Every command, with examples: [docs/COMMANDS.md](docs/COMMANDS.md).

### HTTPS

SpiderMap works the same on http and https; it does not create certificates, the web server does. When Zabbix runs
on https:

- keep `ZABBIX_URL` on `http://127.0.0.1/...` (it never leaves the server, so it needs no certificate) and exclude
  127.0.0.1 from any http-to-https redirect;
- leave the dashboard widget's URL relative, `spidermap.php`: an absolute `http://` address is blocked by browsers
  inside an https dashboard;
- `spidermap cert` shows the certificate's SHA-256 to compare when you install a self-signed certificate on your
  devices, and `spidermap doctor` warns 30 days before it expires.

## Settings

Zabbix Super admins (by default) find a **Settings** button (the gear) on the map. The dialog has these tabs:

| Tab | What you set |
|---|---|
| General | Map name, theme, language, default view, mouse wheel, map refresh, offline devices, IP labels, Zabbix problems; backup (export, import, restore an earlier version) and map reset |
| Scans | Automatic scan, ICMP discovery, SNMP collection, intervals, ICMP payload, timeout, parallel checks, allowed networks, addresses never to scan; **Scan now / Cancel / Pause** with the scan's progress |
| SNMP | GETBULK max-repetitions, retries, timeout (the credentials come from each switch's interface in Zabbix) |
| Topology | Link sources: LLDP, CDP, MAC and ARP tables, VLANs, traffic correlation, hidden switches; tree or free layout |
| Measurements | Throughput source (the router's WAN or a switch port), link thickness, units, load warning / critical, status refresh, history, speed test provider and daily test |
| Security | Off-peak scan window, packet limit, address limit, who may open, edit, test and change settings |
| Logging | Log level and retention, recent events |
| Device types | 11 categories (firewalls, ONT/modems, UPS/PDU, cameras/NVR, IP phones/PBX, printers, NAS, hypervisors, industrial/IoT, powerline/media converters, out-of-band management), how each is recognised, on/off |
| Controllers & wireless | Omada, UniFi and GWN connections (read only, several at once), signal thresholds, the older UniFi connection |

Every change is checked in the browser and on the server, recorded in the audit log, and the previous version is
kept, so a mistake is one click away from being undone.

**Throughput is the router's WAN.** Devices on the router's own Wi-Fi never pass a switch, so SpiderMap reads the
router itself: over **SNMP** when the gateway is a Zabbix host with an SNMP interface (exact 64-bit counters; the WAN
interface is found from the default route, or set in Settings), otherwise over **UPnP** (many home routers; some stop
counting after a few GB, which SpiderMap recognises). When neither works, the card says "WAN unavailable" and why,
instead of showing other numbers.

**Overview and themes.** *Map | Overview* in the header switches to a page of cards: devices, internet, throughput,
speed test, problems, switches, top talkers, activity and suggested links. The palette button chooses a theme
(SpiderMap, Green Spider, Blue Spider, Red Spider) and light, dark or automatic; Settings > General sets the default for everyone.

**Moving around the map.** The controls at the top right zoom, fit, switch between the physical and the **logical
view by subnet**, between the tree and a **free layout** (drag nodes; they keep their place), turn the map
**vertical or horizontal**, **expand or collapse everything** and go **full screen**. The mouse wheel zooms (or scrolls, if you prefer), Shift +
wheel and two-finger scrolling move the map, Ctrl + wheel or a pinch zooms; the arrow keys move, + / − / 0 zoom
and fit.

**Collector health** (the heart icon, for everyone who may open the map) shows the service, the last scans, the
Zabbix API, every switch (answering or not, and its error), the age of the data and the free disk space.

## Your devices: `devices.json`

Created on first install and **never committed to git**. `devices.example.json` shows every option.
Devices can also be changed from the map with the **Edit** button, and most settings from the **Settings** panel.

| Device option | What it does |
|---|---|
| `name` | Display name |
| `type` | Icon: router, switch, unifi, camera, tv, laptop, desktop, phone, printer, appliance, iot, nas, server, vm, lxc |
| `important` | Red on the map and a Zabbix alert when it goes down |
| `url` | Web UI address; `{ip}` is replaced by the current IP, e.g. `http://{ip}:8080` |
| `pin` | Always draw it on this port, e.g. `"SW-Office:3"` (handy for devices that are often off) |

| Setting | What it does |
|---|---|
| `speedtest_daily` | Run a speed test every day at this time, e.g. `"03:30"` (empty = only on demand) |
| `scan_subnets` | Subnets to scan (default: the /24 of each switch) |
| `flag_hours` | How long "new" and "moved" stay highlighted |
| `forget_after_days` | Forget unknown devices not seen for this long |
| `permissions` | Who may do what, as the lowest Zabbix user type (1 = user, 2 = admin, 3 = Super admin): `{"view": 3, "edit": 2, "speedtest": 2}`. Nobody may do more than view; edits never below admin. Run `bash install.sh --update` after a change |
| `port_media` | Mark ports as SFP / SFP+ when SpiderMap cannot tell, per switch: `{"SW-Core": {"17": "SFP+", "18": "SFP+"}}` (`"copper"` removes a mark) |
| `core_switch`, `manual_links`, `aliases` | Only for the classic Zabbix map (`spidermap_zabbix.py`) |

### Optional: Proxmox guest names

Gives every VM and container its real name and running/stopped state. In Proxmox, add the user `spidermap@pve`
with the role **PVEAuditor** on `/`, create an API token `spidermap` for it with *Privilege Separation* unticked,
and add `PVE_URL`, `PVE_TOKEN_ID` and `PVE_TOKEN_SECRET` to `zabbix.env` (see `zabbix.env.example`).

**Check Proxmox's certificate** by adding its fingerprint to `zabbix.env` too; this works with Proxmox's own
self-signed certificate. You find it in Proxmox under *your node → System → Certificates → pve-ssl.pem → View
Certificate*, or on the Proxmox shell:

```bash
openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256     # pveproxy-ssl.pem if you uploaded your own
```

```bash
export PVE_FINGERPRINT="69:E7:68:40:...:E2:0F"
```

Alternatives: `PVE_CA=/path/to/pve-root-ca.pem`, or `PVE_VERIFY=1` for a certificate your system already trusts
(e.g. Let's Encrypt). Without any of these SpiderMap still connects, but writes a warning to its log.

## Update

```bash
spidermap update
```

The same as `cd /opt/spidermap && git pull && bash install.sh --update` (use that the first time you update from a
version before 0.9.8). Open pages reload themselves with the new version.

## Network load

Very light: one small SNMP request per switch every 5 s (about 1–2 KB), a ping sweep of the subnet every
5 minutes, a few short pings to the gateway and 3 public DNS servers every 15 s, and one ping per important device
per minute. A speed test uses about 10 seconds of full-speed traffic each way, so it only runs when you ask for it
or once a day if you set `speedtest_daily`.

## Security

SpiderMap has a single entry point in the Zabbix web folder: **`spidermap.php`**. It checks your Zabbix login
through the public Zabbix API before it shows anything, so it keeps working across Zabbix upgrades.

- **Page and data need a Zabbix login.** By default only **Zabbix Super admins** can open SpiderMap, because it shows
  the whole network. Who may look, edit devices and start speed tests is set separately with `"permissions"` in
  `devices.json` (see *Your settings*). Buttons a user may not use are greyed out with the reason. The Zabbix *guest*
  account is always refused.
- **Nothing about your network is in the web folder.** The page and the map data live in `/var/lib/spidermap/www`,
  readable by root and the web server only. The web folder contains just `spidermap.php`, the icons, and redirects
  from older addresses (`spidermap.html`, `spider-secure.php`).
- **Changes need a Zabbix admin** (or what you set in `permissions`): editing devices and starting speed tests,
  protected against cross-site request forgery (a special request header and an Origin check). The web server can
  write to one folder only, `/var/lib/spidermap/inbox`.
- **Audit log:** every change from the map and every speed test is recorded with the Zabbix user and the time, in
  `/var/lib/spidermap/audit.log` (root only, one JSON line each) and in the activity feed (filter *Changes*).
- **SNMP passwords are never on a command line**, where any local user could see them with `ps`: SpiderMap gives them
  to net-snmp in a private file per switch, in `/var/lib/spidermap/snmp` (folders 700, files 600).
- **Session checks** are reused for at most 10 seconds and never extend an expired Zabbix session.
- **Browser hardening**: no caching, no framing by other sites (`X-Frame-Options`, `frame-ancestors`), and a
  Content-Security-Policy that only allows SpiderMap's own resources.
- Your Zabbix API token stays in `/etc/spidermap/zabbix.env` (root and the service, 640) and never reaches the browser.
- **SpiderMap runs as its own user** `spidermap`, not root, with only the permission `ping` needs and read-only
  access to the rest of the system (the installer checks that this works, otherwise it stays on root and tells you).

### A dedicated Zabbix account for SpiderMap

The installer can use any API token, but a separate account limits what SpiderMap could ever do in Zabbix:

1. **User role** *SpiderMap* (*Users → User roles*): user type **Admin**, *API access* on, and *API methods* set to
   **Allow list**: `host.get`, `item.get`, `item.create`, `trigger.get`, `trigger.create`, `history.push`,
   `usermacro.get`, `image.get`, `dashboard.get`, `dashboard.create`, `dashboard.update`, `dashboard.delete`
   (plus `map.get`, `map.create`, `map.delete` only if you use the classic Zabbix map, `spidermap_zabbix.py`).
2. **User group** *SpiderMap*: *Frontend access* **Disabled**; **Read** on the host groups of your switches and
   servers; **Read-write** on the host group of the *Zabbix server* host, where SpiderMap creates its items and
   triggers.
3. **User** *spidermap* in that group with that role; then *Users → API tokens → Create API token* for it, and put
   the token in `zabbix.env`.

SNMP credentials stored as *Secret text* macros are never returned by the Zabbix API, to any account, so SpiderMap
cannot use them: keep the SNMP credentials as text macros or in the host's SNMP interface.

**Optional extra layer:** a web server rule that blocks any stray `spidermap*.json` in the web folder. Do **not**
block `.php` or `.html` files: `spidermap.php` must stay reachable. The installer warns you if a rule blocks it.

```apache
<FilesMatch "^spidermap.*\.json$">
  Require all denied
</FilesMatch>
```

**HTTPS:** SpiderMap protects *who* can see the map, not the connection itself. Without HTTPS the Zabbix login cookie
travels unencrypted on your network. Enable HTTPS on the Zabbix frontend, even with a self-signed certificate.

**Behind a reverse proxy:** keep forwarding the original host name (nginx: `proxy_set_header Host $host;`),
otherwise edits are refused as cross-site requests. Better still, don't publish Zabbix to the internet and reach it
through a VPN. If you must publish it: HTTPS only, an IP allowlist or an extra login at the proxy, and multi-factor
authentication for Zabbix users (*Users → Authentication → MFA settings*, Zabbix 7.0+).

## Troubleshooting

Start with `spidermap doctor`: it names the problem and the fix. Then:

```bash
spidermap status                                   # the quick look
spidermap logs --errors                            # what went wrong in the scans
spidermap logs --service                           # the service's journal (journalctl -u spidermap)
spidermap test-snmp <switch>                       # one switch over SNMP: answer, LLDP, MAC tables
spidermap check-ports                              # how every switch port is seen
tail -n 5 /var/lib/spidermap/audit.log             # recent changes from the map
```

## Files

| File | Purpose |
|---|---|
| `spidermap.py` | The service and its scheduler, scans, live refresh, requests from the page, installation |
| `spidermap_settings.py` | The settings, their validation, secrets and permissions |
| `spidermap_infer.py` | Device types, link-state and traffic correlation, suggested links |
| `spidermap_wan.py` | The router's WAN (SNMP, UPnP), UniFi, outages and uptime |
| `spidermap_speed.py` | Speed tests: Ookla, Cloudflare, Fast.com |
| `spidermap_gateway.py` | The template of `spidermap.php` |
| `spidermap.html` | The map page (installed in the protected folder, served by `spidermap.php`) |
| `spidermap_ports.py` | Switch port scanner (also usable alone: port report and HTML table) |
| `spidermap_zabbix.py` | Zabbix API and SNMP helpers, plus the classic Zabbix map and dashboard |
| `spidermap_cli.py` | The `spidermap` command (status, logs, scan, doctor, test-snmp, cert, ...) |
| `install.sh` | Installer, updater and the menu |
| `tests/` | Automated tests (run on GitHub for every change: `.github/workflows/tests.yml`) |
| `docs/` | Architecture and release documentation |
| `assets/` | Logo, icons and screenshots |

## Uninstall

```bash
spidermap uninstall          # or: cd /opt/spidermap && bash install.sh --uninstall
```

It first shows everything it will remove and asks; then it writes a backup of your settings, devices and history
to `/root/spidermap-backup-<time>.tar.gz` and removes the service, the `spidermap` user, `/etc/spidermap`,
`/var/lib/spidermap`, SpiderMap's own files in the Zabbix web folder, and in Zabbix only SpiderMap's `autotopo.*`
items (with their triggers), its dashboards and map, and the `spidermap` command. Finally remove the code: `rm -rf /opt/spidermap`.

| Option | Effect |
|---|---|
| `--keep-data` | Keep `/etc/spidermap`, `/var/lib/spidermap` and the `spidermap` user, to install again later |
| `--keep-zabbix` | Leave SpiderMap's items, triggers, dashboards and map in Zabbix |
| `--yes` | Do not ask (for scripts) |

Not touched: the system packages, the API token you created in Zabbix and the iframe setting you changed there.
By hand, without the script: `systemctl disable --now spidermap`, remove `/etc/systemd/system/spidermap.service`,
`/etc/spidermap`, `/var/lib/spidermap`, `spidermap.php` and `spidermap-assets/` in the Zabbix web folder, the
user `spidermap`, and in Zabbix the *SpiderMap* dashboard and the items whose key starts with `autotopo.`.

## Documentation

| Document | For |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | What changed in every version, and upgrade notes |
| [SECURITY.md](SECURITY.md) | Supported versions and how to report a vulnerability |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How SpiderMap works: jobs, files, data flow, Zabbix integration, extending it |
| [docs/COMMANDS.md](docs/COMMANDS.md) | The `spidermap` command and the installer menu, with examples |
| [docs/RELEASING.md](docs/RELEASING.md) | Checklist for publishing a new version |
| [tests/test_gateway.py](tests/test_gateway.py) | Security tests for `spidermap.php` (`python3 tests/test_gateway.py`, needs `php-cli`) |
| [tests/test_core.py](tests/test_core.py) | Tests for scheduling, edits and job locking (`python3 tests/test_core.py`) |
| [tests/test_ports.py](tests/test_ports.py) | Tests for port names and traffic counters with real switch tables (`python3 tests/test_ports.py`) |
| [tests/test_cli.py](tests/test_cli.py) | Tests for the `spidermap` command, certificates and the menu helpers (`python3 tests/test_cli.py`) |

## License

SpiderMap is licensed under the [GNU Affero General Public License v3.0](LICENSE).
