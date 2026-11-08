# Changelog

All notable changes to SpiderMap. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/): while SpiderMap is `0.x`, a new **minor** version
(0.3.0) adds features, a **patch** version (0.2.3) only fixes things.

## [0.9.4] - 2026-11-09

### Fixed
- In *Overview* and *Inventory* the page covered the header, so its buttons (Map, Inventory, Settings, ...) could
  not be clicked until the page was reloaded. The two views now start below the header, like the map.

### Added
- **Device drawings for the spiders** (our own designs): one drawing per device type - router, firewall, switch,
  access point, camera, IP phone, server, hypervisor, NAS, printer, PC, laptop, mobile, TV, IoT, UPS, modem / ONT,
  VM, internet and Wi-Fi - shown as drawn devices in Green Spider, as outlines in Blue Spider and as solid shapes in
  Red Spider. Other types, and the default theme, keep their icons.

### Removed
- The colour themes Clean, Midnight and Ops (0.9.0): the spiders replace them. Whoever had chosen one gets the
  default SpiderMap theme.

## [0.9.3] - 2026-11-09

### Added
- **The spiders:** three style packs beside the default SpiderMap theme, each with more than colours.
  *Green Spider* - green, the map from left to right by default, nodes as device cards. *Blue Spider* - blue, nodes
  as tiles. *Red Spider* - dark by default, red, nodes as hexagons, devices that are down pulse. The direction is
  remembered for each theme (Green Spider starts left to right); a light / dark mode you chose yourself always wins.
- A small easter egg in some of them.

## [0.9.2] - 2026-11-09

### Fixed
- The map controls sat at the bottom right; with all their buttons they could reach below a short window (such as
  a Zabbix dashboard widget). They now sit at the top right, become one row in windows lower than 560 px, and
  scroll instead of being cut off.

## [0.9.1] - 2026-11-08

### Added
- **Inventory** (*Map | Overview | Inventory* in the header). *Network devices*: model, serial number, firmware,
  hardware version, manufacturer, uptime and location of every switch and router (SNMP ENTITY-MIB and the system
  group) and of UniFi devices (from the controller). Devices of the same model running different firmware are marked
  - often one was forgotten in an update. *All devices*: name, type, manufacturer, IP, MAC, status, VLAN,
  connection, first and last seen. Search, sort by any column (empty values last), CSV export (cells that could be
  read as spreadsheet formulas are written as text), and a name takes you to the device on the map.

## [0.9.0] - 2026-11-08

### Added
- **Overview:** the *Map | Overview* switch in the header shows the network in cards - devices (online, offline,
  new, by type), the internet (uptime this month, outages, latency), throughput and this month's usage, the last
  speed test, problems (Zabbix problems, devices down, the network's own warnings), switches (ports up, CPU), top
  talkers today, recent activity and suggested links. A name takes you to that device on the map. Built from what
  the page already has: no new requests to the server.
- **Themes:** SpiderMap, *Clean*, *Midnight* and *Ops*, each in light and dark. Settings > General > *Theme* sets
  the default; the palette button in the header lets everyone choose their own theme and light / dark / automatic.

## [0.8.3] - 2026-11-08

### Changed
- **The code is split into modules** (settings, inference, WAN, speed tests, the gateway template), moved verbatim:
  the map from the same scan is identical before and after (compared line by line). `spidermap.py` went from 4,400
  to 3,200 lines.
- **Tests run automatically on GitHub** (Actions) on every push and pull request, on Python 3.11 and 3.12, with
  syntax checks of the Python files, `install.sh`, the page's JavaScript and the generated `spidermap.php`.

### Fixed
- One test wrote to the real `/var/lib/spidermap` when run as root. The tests now point every system path at a
  temporary folder before anything runs, and pass as a normal user.

## [0.8.2] - 2026-11-08

### Added
- **Traffic correlation** (Settings > Topology): links between switches without LLDP, found from the counters
  SpiderMap already reads every 5 seconds. Two ports on different switches, at the same speed and not already
  explained, are the two ends of one cable when what leaves one arrives at the other (within 5%, both ways) while the
  traffic rises and falls together. After three such measurements the pair is a suggested link (Confirm / Reject);
  a clear mismatch starts again. Steady or idle traffic proves nothing and is never used. It finds direct cables:
  through an unmanaged switch the totals differ.

## [0.8.1] - 2026-11-08

### Fixed
- Dragging the map selected the labels and icons it passed over (blue highlight), and an icon could be dragged away
  like an image. The map is no longer selectable and its icons are not draggable.
- *Open web UI* from the map inside the Zabbix dashboard: logins on the opened page (e.g. UniFi OS on a Cloud Key)
  did nothing, because the new tab inherited the dashboard's iframe sandbox, which blocks forms. Add
  `allow-popups-to-escape-sandbox` to Zabbix's iframe sandboxing exceptions (Administration > General > Security);
  the README shows the full line. The map itself stays sandboxed.

## [0.8.0] - 2026-11-08

### Added
- **Evidence for every link between switches:** LLDP / CDP, spanning tree (the switch names the bridge and port at
  the other end), LACP (the aggregation partner) and your confirmation, shown in the switch's panel. **Without LLDP
  the switch tree is now built from spanning tree or LACP.** Your confirmation outranks everything else.
- **Redundant cables:** ports that spanning tree blocks are real cables no MAC table shows; they are drawn as
  dashed lines between the switches ("STP blocked").
- **Link-state correlation:** ports on two switches that go down and up together (not when many ports change at
  once) become **suggested links** in the switches' panels, with Confirm / Reject. A rejected suggestion never comes
  back; *Confirm this link* and *Remove confirmation* work on every link between switches.
- **Speed and duplex:** a speed mismatch between the two ends of a link, and half duplex on a port, are warnings.
- **PoE:** ports that deliver power (class), PoE faults and the switch's total; a device on a PoE port says so
  (often an access point, camera or phone).
- **IPv6 neighbours** from the switches and routers, and the **TTL** of each device's ping reply: how many hops
  away, and a hint at its operating system (64 / 128 / 255).
- All of it from optional standard tables (BRIDGE-MIB, EtherLike-MIB, IEEE8023-LAG-MIB, POWER-ETHERNET-MIB,
  IP-MIB): switches without them work as before.

## [0.7.2] - 2026-11-07

### Added
- **UniFi** (Settings > Wireless & WAN): with a read-only local user on the controller - UniFi OS (Cloud Key Gen2,
  UDM) or the older controller - access points get their name, model and icon, and every wireless client is drawn
  under its access point (link source "UniFi"), with its network name and signal. Your own names and "connected
  through" always win. Independent of Zabbix: its own settings, not Zabbix macros. Collector health shows the
  controller's state.
- **Secrets kept apart:** the controller's password is stored only in `/etc/spidermap/secrets.json` (readable by the
  service only); it is never sent to the page, written to the audit log or exported.

## [0.7.1] - 2026-11-07

### Added
- **Free layout:** a button in the map controls switches between the tree and a free layout with straight links,
  where you drag nodes where you want them. Moved nodes keep their place after every scan (per browser, per view and
  orientation); *Reset layout* starts again from the tree. Settings > Topology > *Map layout* sets the default.
- **Greek** (Ελληνικά): the EN / ΕΛ button in the header switches the language for you; Settings > General >
  *Language* sets the default for everyone. Device and switch names, addresses and your own texts are never
  translated; a few rare messages may still appear in English.

## [0.7.0] - 2026-11-07

### Added
- **Hidden switches.** Several devices on one switch port without LLDP: if one of them is an access point or a
  hypervisor, the others are drawn behind it; otherwise they are drawn behind an "Unmanaged switch", outlined dashed
  because it is inferred (Settings > Topology > *Find hidden switches*).
- **Edit the map** (users who may edit): on an inferred switch *Confirm and name* (name, model, location), *It's
  something else* (access point, hypervisor, Wi-Fi bridge, hub, other) or *Not a device*; on any device on a switch
  port *Add an unmanaged switch here*. Stored by switch and port, separate from the devices: SpiderMap still places
  every device by what the switches report, the edit only says what sits in between. A scan starts right away.
- **Connected through** (Edit): a device placed behind another one, e.g. a media converter or powerline adapter.
  A link that would make a loop is ignored.
- **Zabbix problems on the map:** nodes that are Zabbix hosts get a ring in the colour of their worst active problem
  and a tag with the number; the panel lists them with a link to Zabbix. SpiderMap's own alerts are not counted.
- **CDP** for Cisco devices without LLDP (LLDP first where a port has both).
- **Logical view by subnet:** a button in the map controls switches between the physical view and one group per
  subnet under the gateway; Settings > General > *Default view* chooses which one opens.

### Changed
- One button expands or collapses the whole map (it shows which one it will do).

### Fixed
- An open form in the side panel (Edit) could be rebuilt by the automatic refresh while typing.

## [0.6.1] - 2026-11-07

### Added
- **Where each link comes from.** A device's panel says how SpiderMap knows its connection: LLDP (the device
  announced itself), the switch's MAC table, pinned in devices.json, the Proxmox API - or guessed: seen only through
  the gateway (Wi-Fi or its LAN ports), behind another device, or where it was last seen. Guessed links are drawn
  dotted (see the legend).
- **Approve** on new and unknown devices (for users who may edit): they are no longer marked, after every scan.
  Recorded in the audit log and the activity feed.
- **Stale devices:** devices not seen for 7 days (Settings > General > Devices) are marked "stale" until they are
  forgotten.

### Fixed
- A port that changed to a higher speed (e.g. 100 Mbps -> 1 Gbps) had its traffic ignored for up to 5 minutes:
  SpiderMap now reads the port's speed again at once.
- After a switch restart, new port numbers were picked up only at the next scheduled scan: a scan now starts right
  away (at most every 10 minutes per switch), noted in the activity feed.
- Stopped Proxmox guests no longer showed under a Proxmox host recognised automatically as a hypervisor (0.6.0).

## [0.6.0] - 2026-11-06

### Added
- **VLANs.** SpiderMap reads, where the switches offer it (Q-BRIDGE-MIB), the VLAN names, which VLANs each port
  carries (untagged / tagged) and the VLAN of every device (from the MAC table). A **VLAN filter** in the header
  shows only the VLANs you choose (remembered per browser); switches and the gateway always stay. The port table
  shows each port's VLANs, and a device's panel its VLAN. Switches without Q-BRIDGE-MIB work as before, without
  VLANs. Settings > Topology > *VLANs* switches it off.
- **Device types** (Settings > Device types, as in the design): 11 categories with their own icons - firewalls &
  gateways, ONT / provider modems, UPS & smart PDU, IP cameras & NVR, IP phones & PBX, printers, NAS & storage,
  hypervisors, industrial & IoT (only ever read), powerline & media converters, out-of-band management (iLO,
  iDRAC, IPMI). Each card says how the devices are recognised and how many are on your network, and can be
  switched off. A type set with Edit always wins.
- **Link aggregation and fibre on the map.** A link over a LAG is drawn as one thicker link, named after the LAG
  and its member ports (IEEE8023-LAG-MIB); links on SFP ports are drawn dashed in purple. The port table says which
  ports belong to a LAG.
- **Uninstall:** `bash install.sh --uninstall` shows exactly what it will remove, asks, writes a backup of your
  settings, devices and history to `/root/spidermap-backup-<time>.tar.gz`, then removes the service, the
  `spidermap` user, `/etc/spidermap`, `/var/lib/spidermap`, SpiderMap's files in the Zabbix web folder (only files
  that carry SpiderMap's mark) and, in Zabbix, only its `autotopo.*` items (with their triggers) and its
  dashboards and map. `--keep-data` keeps settings and history, `--keep-zabbix` leaves Zabbix untouched, `--yes`
  does not ask.

### Fixed
- An unknown device without a name showed its MAC address twice in the activity feed.

## [0.5.3] - 2026-11-06

### Changed
- **Throughput is the router's WAN, never a substitute.** When the router's WAN cannot be read, the card shows
  "WAN unavailable ⚠" with the reason, monthly usage shows "-", no graph of old values is drawn, and the activity
  feed says when measuring stops and starts again. The switch port to the gateway is used only when chosen in
  Settings > Measurements (0.5.2 fell back to it by itself).

### Added
- **The router's WAN over SNMP**, the most exact source (64-bit counters that do not stop): used when the gateway
  is a Zabbix host with an SNMP interface (MikroTik, OPNsense, pfSense, ...). SpiderMap finds the WAN interface from
  the router's default route, or uses the name set in Settings > Measurements > *Router's WAN interface*. UPnP is
  the second source. Collector health shows both.

## [0.5.2] - 2026-11-06

### Fixed
- **Throughput showed zero with some routers.** Their UPnP byte counters stop at 2,147,483,647 (or 4,294,967,295)
  after a few GB, and SpiderMap took the unchanging value as "no traffic". It now recognises stuck counters - and
  counters that do not move while the switch sees traffic - and falls back to the switch port to the gateway, says
  why on the card (amber "switch port ⚠") and in Collector health, and asks the router again every 10 minutes
  (after a router restart its counters work again for a while).

### Added
- SpiderMap reads the list of actions the router offers and uses a **64-bit byte counter** where one exists
  (these do not stop after a few GB).

## [0.5.1] - 2026-11-06

### Fixed
- **Throughput and monthly usage missed every device on the router's own Wi-Fi / LAN ports**: they were measured on
  the switch port to the gateway. SpiderMap now reads the **router's own WAN counters over UPnP**
  (WANCommonInterfaceConfig), so all internet traffic counts. Only the default gateway is trusted, not any device that
  answers. Settings > Measurements > *Throughput source*: router if available (default), router only, or switch port.
  The card says which one it uses.
- **Short internet outages were lost**: an outage needed two failed checks in a row (about 30 s). When Cloudflare,
  Google and Quad9 all fail, SpiderMap now checks again at once, so outages from about 15 s are recorded, from the
  first failed check.
- **Time without SpiderMap running counted as "online"**, so a month could show 100 % while nobody was watching.
  Such time is now *not monitored*: uptime is calculated over the monitored time, and the card shows how much of the
  month was monitored. An outage that was going on when SpiderMap stopped ends at the last moment it was seen.
- **Moving the map could get stuck** when the browser interrupted a drag; the map could also be dragged out of
  sight. Both fixed.
- Hiding offline devices no longer leaves gaps in the map.

### Added
- **Speed test providers:** Ookla (with a list of nearby servers), **Cloudflare** (speed.cloudflare.com) and
  **Fast.com** (Netflix), with download, upload, latency and latency under load. Fast.com has no official API:
  if Netflix changes it, its tests may stop until SpiderMap is updated. nPerf cannot be offered: it has no public
  interface. Cloudflare and Fast.com were tested against local imitations of their services.
- **Reset map** (Settings > General): forgets the devices seen, history, activity, who was online, totals and
  outages, then scans again. Everything is moved to a backup folder first; you choose whether to keep your device
  names and the speed test history.
- **Map controls** at the bottom right: zoom in / out, fit, **vertical or horizontal view**, **expand all**,
  **collapse all**, **full screen** (inside the Zabbix dashboard, where the browser may not allow it, the map opens
  on its own).
- **Moving around the map:** the mouse wheel zooms (or scrolls: Settings > General > *Mouse wheel*), Shift + wheel
  and two-finger scrolling move the map in every direction, Ctrl + wheel or a pinch zooms, one finger moves and two
  fingers zoom on touch screens, the arrow keys move and + / − / 0 zoom and fit.

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

[0.9.4]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.9.4
[0.9.3]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.9.3
[0.9.2]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.9.2
[0.9.1]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.9.1
[0.9.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.9.0
[0.8.3]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.8.3
[0.8.2]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.8.2
[0.8.1]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.8.1
[0.8.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.8.0
[0.7.2]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.7.2
[0.7.1]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.7.1
[0.7.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.7.0
[0.6.1]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.6.1
[0.6.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.6.0
[0.5.3]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.5.3
[0.5.2]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.5.2
[0.5.1]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.5.1
[0.5.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.5.0
[0.4.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.4.0
[0.3.1]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.3.1
[0.3.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.3.0
[0.2.3]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.2.3
[0.2.2]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.2.2
[0.2.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.2.0
[0.1.0]: https://github.com/christopher-theodorou/spidermap/releases/tag/v0.1.0
