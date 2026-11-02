<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/spidermap-dark.png">
    <img alt="SpiderMap - Nothing touches the web unnoticed. Network topology map for Zabbix" src="assets/spidermap-light.png" width="720">
  </picture>
</p>

<p align="center">
  <b>A live, beautiful map of your network, built automatically from your switches and Zabbix.</b><br>
  Every device, every port, every change.
</p>

<p align="center">
  <img alt="SpiderMap overview" src="assets/screenshot-overview.png" width="900">
</p>

## What it does

**🕸️ Maps your network by itself**
Reads LLDP and the switches' MAC tables, and draws the real topology: Internet → gateway → switches → devices →
Proxmox guests. Every link is labelled with its port, and every device has an icon picked from its type.

**⚡ Live**
Port traffic every 5 seconds with animated links, ports coloured by speed (100 Mbps, 1 / 2.5 / 10 Gbps),
CPU, memory and disk for switches, servers and VMs, with graphs.

**🌐 Internet health**
Throughput, latency, packet loss and jitter to the gateway and the internet, uptime and outages this month,
public IP changes, and a speed test on demand or every night.

**🔔 Nothing touches the web unnoticed**
An activity feed and Zabbix alerts for:
- new and **unknown devices**, especially ones plugged into a free switch port
- devices that **move** to another port, **IP changes** and **IP conflicts**
- **possible cable problems**: port errors, flapping links, a port dropping from 1 Gbps to 100 Mbps
- **unusual traffic**, like a camera suddenly uploading at 3 am
- **internet outages**, packet loss, ports going up and down, important devices going offline

**👥 And more**
Who's online (presence timeline), top talkers, device list export (CSV), edit names, icons and web UI addresses
straight from the map, light and dark theme.

<p align="center">
  <img alt="Switch panel" src="assets/screenshot-switch.png" width="440">
  <img alt="Activity feed in dark mode" src="assets/screenshot-dark.png" width="440">
</p>

SpiderMap only **reads** from your network. It never changes your switch configuration.

## Requirements

- **Zabbix 7.0 or newer.** SpiderMap runs on the Zabbix server (or its LXC container), Debian or Ubuntu.
- **Switches in Zabbix with an SNMP interface** and a template that discovers interfaces.
  Tested with Huawei VRP; other brands using the standard BRIDGE-MIB and LLDP-MIB should work.
- **LLDP enabled** on the switches (and `lldpd` on Linux and Proxmox hosts so they are recognised).
- A Zabbix **API token** for an admin user: *Users → API tokens → Create API token*.

## Install

```bash
apt install -y git
git clone https://github.com/<you>/spidermap.git /opt/spidermap
cd /opt/spidermap
bash install.sh
```

The installer sets up the packages it needs, asks for your Zabbix API address and token, runs the first scan,
starts the real-time service (`spidermap-rt`) and schedules the scans. Upgrading from the earlier
*zabbix-netmap* version? The installer finds it and moves your devices, settings and history over.

**One manual step in Zabbix**, for the dashboard widget:
*Administration → General → Other → Security → Iframe sandboxing exceptions*

```
allow-scripts allow-same-origin allow-popups allow-downloads
```

Then open **Dashboards → SpiderMap**, or the page directly at `http://<zabbix>/zabbix/spidermap.html`.

## Your settings: `devices.json`

Created on first install and **never committed to git**. `devices.example.json` shows every option.
Most of it can also be changed from the map with the **Edit** button (Zabbix admins only).

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
| `core_switch`, `manual_links`, `aliases` | Only for the classic Zabbix map (`spidermap_zabbix.py`) |

### Optional: Proxmox guest names

Gives every VM and container its real name and running/stopped state. In Proxmox, add the user `spidermap@pve`
with the role **PVEAuditor** on `/`, create an API token `spidermap` for it with *Privilege Separation* unticked,
and add `PVE_URL`, `PVE_TOKEN_ID` and `PVE_TOKEN_SECRET` to `zabbix.env` (see `zabbix.env.example`).

## Update

```bash
cd /opt/spidermap && git pull && bash install.sh --update
```

Open pages reload themselves with the new version.

## Network load

Very light: one small SNMP request per switch every 5 s (about 1–2 KB), a ping sweep of the subnet every
5 minutes, a few short pings to the gateway and 3 public DNS servers every 15 s, and one ping per important device
per minute. A speed test uses about 10 seconds of full-speed traffic each way, so it only runs when you ask for it
or once a day if you set `speedtest_daily`.

## Security

- **The map needs a Zabbix login.** The topology data is kept outside the web folder (`/var/lib/spidermap/www`) and is
  only handed out by `spidermap_api.php` to users with a valid Zabbix session. The Zabbix *guest* account is refused.
  Checking the map never keeps an expired Zabbix session alive.
- **Changes need a Zabbix admin**: editing devices and starting speed tests. These requests are protected against
  cross-site request forgery (a special request header plus an Origin check).
- **Least privilege on disk**: the data files are readable by root and the web server only; the web server can write
  to just one folder (`/var/lib/spidermap/inbox`) for edits and speed test requests. Your Zabbix API token stays in
  `zabbix.env` (root only) and never reaches the browser.
- **Only Zabbix admins should see the map?** Set `"view_min_user_type": 2` in `devices.json` settings
  (1 = any Zabbix user, 2 = admins, 3 = super admins) and run `bash install.sh --update`.

**Behind a reverse proxy:** keep forwarding the original host name (for nginx: `proxy_set_header Host $host;`),
otherwise SpiderMap refuses edits as cross-site requests. Better still, don't publish Zabbix to the internet at all
and reach it through a VPN. If you must publish it: HTTPS only, an IP allowlist or an extra login at the proxy, and
multi-factor authentication for Zabbix users (*Users → Authentication → MFA settings*, Zabbix 7.0+).

## Troubleshooting

```bash
systemctl status spidermap-rt                      # real-time service
journalctl -u spidermap-rt -n 30                   # its log
tail -n 30 /var/log/spidermap.log                  # scans
cd /opt/spidermap && . ./zabbix.env && python3 spidermap.py   # run a scan by hand
```

## Files

| File | Purpose |
|---|---|
| `spidermap.py` | Scans, live refresh, real-time service, Zabbix integration |
| `spidermap.html` | The map page (copied into the Zabbix web folder) |
| `spidermap_ports.py` | Switch port scanner (also usable alone: port report and HTML table) |
| `spidermap_zabbix.py` | Zabbix API and SNMP helpers, plus the classic Zabbix map and dashboard |
| `install.sh` | Installer and updater |
| `assets/` | Logo, icons and screenshots |

## License

SpiderMap is licensed under the [GNU Affero General Public License v3.0](LICENSE).
