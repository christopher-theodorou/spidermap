# SpiderMap commands

The installer adds one command, `spidermap`, usable from any folder. Run it as root (`sudo spidermap ...` if you
are not). Tab completion works after the next login.

| Command | What it does | Changes anything? |
|---|---|---|
| [`spidermap status`](#status) | Service, last and next scan, every switch, where to open the map | no |
| [`spidermap logs`](#logs) | The scan log, or the service's journal | no |
| [`spidermap scan`](#scan) | Scan now and show the progress | starts a scan |
| [`spidermap doctor`](#doctor) | Checks everything; every problem comes with its fix | no |
| [`spidermap test-snmp <switch>`](#test-snmp) | Does a switch answer SNMP, and does it give LLDP and MAC tables? | no |
| [`spidermap check-ports`](#check-ports) | How SpiderMap sees every switch port | no |
| [`spidermap cert [host[:port]]`](#cert) | An https certificate: names, expiry, SHA-256 fingerprint | no |
| [`spidermap fix-permissions`](#fix-permissions) | Puts file owners and modes right, restarts the service | yes |
| [`spidermap update`](#update) | `git pull`, then `bash install.sh --update` | yes |
| [`spidermap uninstall`](#uninstall) | `bash install.sh --uninstall` (with a plan, a confirmation and a backup) | yes |
| [`spidermap menu`](#the-installer-menu) | The installer menu | - |
| `spidermap version` / `help` | | no |

Commands that talk to switches run as the `spidermap` user, like the service, so root never leaves files behind
that the service cannot write. Nothing here prints your API token, SNMP community or passwords.

If the command is missing (an install before 0.9.8, or `/usr/local/bin/spidermap` belongs to something else):
`python3 /opt/spidermap/spidermap_cli.py <command>` does the same, and `bash install.sh --update` adds it.

---

## status

```
$ spidermap status
SpiderMap 0.9.8
  Service:     active, as spidermap, since 2026-11-10 09:12
  Last scan:   2026-11-10 10:20, 41 s, ok
  Next scan:   2026-11-10 10:35
  Map:         57 devices, 49 online
  Switches:
    ✓ SW-Core  (28 ports)
    ✗ SW-Lab  snmpwalk timeout on udp:10.20.0.3:161
  Zabbix API:  ok
  Disk:        12.3 GB free
  Open:        https://10.20.0.46/zabbix/spidermap.php
```

The quick look. For an explanation of a red line, run `spidermap doctor`.

## logs

```bash
spidermap logs                 # the last 40 lines of the scan log
spidermap logs -n 200          # more
spidermap logs -f              # follow it live (Ctrl-C to stop)
spidermap logs --errors        # only problems (and the line that starts each job)
spidermap logs --service       # the service's own journal (start, stop, crashes); also with -f
```

The scan log is `/var/lib/spidermap/spidermap.log`, one file per day (kept as set in *Settings > General*). More
detail: *Settings > General > Log level > debug*, then `spidermap scan`.

## scan

```bash
spidermap scan                 # asks the service for a scan and shows its progress
spidermap scan --no-wait       # just ask, and return
```

The scan runs in the service, exactly like *Scan now* on the map: it appears in the job history and the activity
feed. Ctrl-C only stops watching. If the service is not running, the scan runs in the terminal instead.

## doctor

```bash
spidermap doctor
```

Checks, in order: the **service** (running, not stuck, same version as the code, code up to date), the
**configuration** (`zabbix.env` private, `devices.json` valid), **permissions**, the **web** side (`spidermap.php`
installed, asks for a login, no web server rule blocking it), **Zabbix** (API address, token, the iframe sandboxing
exceptions the dashboard widget needs, the dashboard), **HTTPS** (see below), **Proxmox** (if set up), the **data**
(last scan, disk space, speed test program) and every **switch** over SNMP.

Every problem is followed by its fix:

```
Switches (SNMP)
  ✓ SW-Core (10.20.0.2, v2c, community from Zabbix)
  ✗ SW-Lab (10.20.0.3) does not answer SNMP
      → no answer: check that SNMP is enabled on the switch, that it allows 10.20.0.46 (this server),
        the community, and that UDP 161 is not blocked
```

`✗` is a problem, `!` a warning or a suggestion. Exit code: 0 = no problems, 2 = problems (useful in scripts).

**HTTPS checks.** SpiderMap does not create or install certificates; that is the web server's job. The doctor
only tells you when something needs attention:

- the certificate expires within 30 days, or has expired;
- it does not cover the server's IP address, so browsers opening `https://<ip>/` warn;
- `http://` does not redirect to `https://` (a suggestion), or `http://127.0.0.1` *does* redirect (SpiderMap and
  other local tools expect plain http there - exclude 127.0.0.1 from the redirect);
- the **dashboard widget** opens an absolute `http://...` address while Zabbix runs on https: browsers block such
  an http page inside an https dashboard. The fix is the relative URL **`spidermap.php`** (*Dashboards > SpiderMap >
  Edit > widget > URL*), which follows Zabbix itself on http and https, and survives an address change too. This is
  what the installer creates.

SpiderMap's own connection to the Zabbix API is best left on `http://127.0.0.1/...`: it never leaves the server,
so it needs no certificate even when every browser uses https.

## test-snmp

```bash
spidermap test-snmp SW-Core                         # a Zabbix host, by name
spidermap test-snmp 10.20.0.2                       # or by the IP of its SNMP interface
spidermap test-snmp 10.20.0.9 --community public    # a switch not in Zabbix yet (SNMPv2c)
```

```
SW-Core  (10.20.0.2, v2c, community from Zabbix)
  ✓ answers: SW-Core
      TP-Link JetStream 18-Port Gigabit L2+ Managed Switch
      interfaces: 34, up for: 41 days
  ! LLDP neighbours: none
  ✓ MAC table (Q-BRIDGE): 212
  ! MAC table (BRIDGE): none
  ✓ Model, serial, firmware (ENTITY-MIB, optional): 3

  No LLDP neighbours: enable LLDP on the switch (and its neighbours). SpiderMap still finds links
  from the other side, or from the MAC tables - see docs/COMMANDS.md > test-snmp
```

The fastest answer to "why is this switch not linked?":

- **No answer**: SNMP is off on the switch, the community (or SNMPv3 user) is wrong, the switch does not allow this
  server's IP, or a firewall blocks UDP 161. Fix it on the switch, then test again.
- **LLDP neighbours: none**: enable LLDP on the switch. SpiderMap still finds the link if the switch at the other
  end reports it (since 0.9.7), or from the MAC tables.
- **No MAC table** (neither of the two): devices behind this switch cannot be placed on its ports. One of the two is
  enough. Cisco switches give the BRIDGE table per VLAN, with the VLAN in the community (`community@1`).
- **ENTITY-MIB** is optional: it fills the model, serial and firmware in *Inventory*.

Adding a switch to SpiderMap = adding it to Zabbix as a host with an **SNMP interface** (any SNMP template). The
next scan picks it up.

## check-ports

```bash
spidermap check-ports
spidermap check-ports --community public
```

Every interface of every switch: its SpiderMap label, speed and state, or why it is not counted as a port. The
same as `python3 spidermap.py --check-ports`.

## cert

```bash
spidermap cert                          # this server's https certificate (port 443)
spidermap cert 10.20.0.10:8006          # any other server, e.g. Proxmox
spidermap cert 10.20.0.10:8006 --fingerprint     # only the SHA-256, for scripts
```

```
  Server:        127.0.0.1:443
  Issued to:     zabbix
  Issued by:     zabbix  (self-signed)
  Valid for:     10.20.0.46
  Valid until:   2036-11-08 12:00  (3650 days left)
  SHA-256:       3A:7C:21:D9:...:E0:4B
  Short:         3A7C21D9…E04B   (compare this when you install it on a device)
```

**Installing a self-signed certificate on your devices**: copy the certificate file from the web server (for
Apache, the `SSLCertificateFile` in its https site), install it as a *trusted root* on each PC or phone, then open
it there and compare the SHA-256 with the one above. Same fingerprint = the right certificate. With a domain and
Let's Encrypt (or your own small certificate authority) this step disappears.

## fix-permissions

```bash
spidermap fix-permissions
```

Sets the owners and modes SpiderMap expects (`zabbix.env` root:spidermap 640, the data folders owned by the service
user, the web server's inbox owned by the web server) and restarts the service. Useful after copying files by hand
or restoring a backup. The menu's *Fix permissions* also checks again which user the service should run as.

## update

```bash
spidermap update
```

`git pull` in `/opt/spidermap`, then `bash install.sh --update`. Stops if `git pull` cannot fast-forward (files
changed by hand: `git -C /opt/spidermap status` shows them).

## uninstall

```bash
spidermap uninstall
spidermap uninstall --keep-data          # keep settings, devices and history, to install again later
spidermap uninstall --keep-zabbix        # leave SpiderMap's items, dashboards and map in Zabbix
```

Shows everything it will remove, asks you to type `remove`, writes a backup to `/root/spidermap-backup-<time>.tar.gz`,
then removes it. See the README's *Uninstall* section.

---

## The installer menu

`bash install.sh` in a terminal (or `spidermap menu`) opens:

```
  SpiderMap 0.9.8  -  installed, service active

   1) Install             first install, or run the setup again (repair)
   2) Update              git pull, then refresh the page and restart the service
   3) Check everything    every problem, with its fix (spidermap doctor)
   4) Zabbix connection   API address and token
   5) Proxmox             optional: names of your VMs and containers
   6) Fix permissions
   7) Status and logs
   8) Certificate         this server's https certificate, for installing on your devices
   9) Uninstall
   0) Exit
```

- **4) Zabbix connection** shows the address and a masked token, tests a new one before saving it, keeps the rest
  of `zabbix.env` and restarts the service. `10.20.0.46/zabbix` is completed to the full API address.
- **5) Proxmox** asks for the address and token, shows Proxmox's certificate and asks you to compare its SHA-256
  with the one Proxmox shows (*node > System > Certificates*). If they match, SpiderMap accepts exactly that
  certificate and refuses any other. A certificate this server already trusts (Let's Encrypt) is checked normally
  instead, so renewals keep working. When Proxmox renews a self-signed certificate, `doctor` says so: choose 5
  again to pin the new one.

The command-line options stay for scripts and automation:

| Option | Does |
|---|---|
| `bash install.sh --install` | First install without the menu |
| `bash install.sh --update` | After `git pull`: refresh the page, restart the service |
| `bash install.sh --uninstall [--keep-data] [--keep-zabbix] [--yes]` | Uninstall |
| `bash install.sh --menu` | The menu, also without a terminal |

Run without a terminal (from a script or a pipe), plain `bash install.sh` installs exactly as before.
