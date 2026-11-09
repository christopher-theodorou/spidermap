"""Settings: the schema behind the Settings dialog, validation, defaults, secrets, permissions.

Part of SpiderMap (see spidermap.py and docs/ARCHITECTURE.md)."""
import ipaddress
import os
import re

from spidermap_infer import DEVICE_CATEGORY_KEYS
from spidermap import CONF_DIR, _get, load_json, save_json  # noqa: E402 - spidermap.py loads this module at its end


# Each setting: key, type, default, tab, group, label, and optionally: range, unit, choices, help, preview (shown,
# not active yet), locked (always on). Types: bool, int, choice, str, time, digits, cidrs, ips, level.
def _S(key, typ, default, tab, group, label, **kw):
    return {"key": key, "type": typ, "default": default, "tab": tab, "group": group, "label": label, **kw}


_LEVEL3 = [[1, "Zabbix users"], [2, "Zabbix admins"], [3, "Zabbix Super admins"]]


SETTINGS_SCHEMA = [
    # ---- General
    _S("language", "choice", "en", "General", "General settings", "Language",
       choices=[["en", "English"], ["el", "Ελληνικά"]],
       help="The language the map opens in. Everyone can switch for themselves with the EN / ΕΛ button in the header."),
    _S("skin", "choice", "spidermap", "General", "General settings", "Theme",
       choices=[["spidermap", "SpiderMap"], ["green", "Green Spider"], ["blue", "Blue Spider"], ["red", "Red Spider"]],
       help="Colours and style for everyone. Each person can choose their own from the palette button in the header."),
    _S("map_name", "str", "", "General", "General settings", "Map name", maxlen=60, help="Shown in the header and the browser tab."),
    _S("default_view", "choice", "physical", "General", "General settings", "Default view",
       choices=[["physical", "Physical (switches and ports)"], ["l3", "Logical (by subnet)"]],
       help="Which view the map opens with. The button in the map controls switches between them."),
    _S("page_refresh_s", "choice", 60, "General", "General settings", "Map refresh",
       choices=[[10, "10 seconds"], [30, "30 seconds"], [60, "1 minute"], [0, "Manually"]],
       help="How often open pages reload the map. Live traffic keeps its own interval (Measurements)."),
    _S("wheel", "choice", "zoom", "General", "General settings", "Mouse wheel",
       choices=[["zoom", "Zooms the map"], ["pan", "Scrolls the map"]],
       help="Whatever you choose: Ctrl + wheel or a pinch always zooms, Shift + wheel and two-finger scrolling always move the map."),
    _S("show_offline", "bool", True, "General", "General settings", "Show offline devices",
       help="Devices that do not answer are shown in grey instead of being hidden from the map."),
    _S("ip_labels", "bool", True, "General", "General settings", "IP labels on nodes"),
    _S("zabbix_triggers", "bool", True, "General", "General settings", "Link with Zabbix triggers",
       help="Nodes that are Zabbix hosts get a coloured ring with the severity of their active problems."),
    _S("flag_hours", "int", 24, "General", "Devices", "Mark new and moved devices for", range=[1, 720], unit="h"),
    _S("stale_days", "int", 7, "General", "Devices", "Mark devices not seen for", range=[1, 365], unit="days",
       help="They stay on the map, marked 'stale', until they are forgotten."),
    _S("forget_after_days", "int", 30, "General", "Devices", "Forget unknown devices after", range=[1, 365], unit="days"),
    # ---- Scans
    _S("auto_scan", "bool", True, "Scans", "Scan schedule", "Automatic scan",
       help="When off, scans start only with the Scan now button. Traffic and status keep updating."),
    _S("icmp_discovery", "bool", True, "Scans", "Scan schedule", "Discovery via ICMP",
       help="Ping every address of the allowed networks to find active devices."),
    _S("snmp_collection", "bool", True, "Scans", "Scan schedule", "SNMP collection",
       help="Read LLDP, ARP and MAC tables from the switches. When off, the switches keep their last data."),
    _S("scan_interval_min", "int", 5, "Scans", "Scan schedule", "Full scan", range=[2, 1440], unit="min",
       choices=[[5, "5 minutes"], [15, "15 minutes"], [30, "30 minutes"], [60, "1 hour"], [360, "6 hours"]],
       help="How often all allowed networks are scanned again."),
    _S("rt_interval_s", "int", 5, "Scans", "Scan schedule", "Traffic measurements", range=[5, 300], unit="s",
       choices=[[5, "5 seconds"], [10, "10 seconds"], [30, "30 seconds"], [60, "1 minute"], [300, "5 minutes"]],
       help="How often the interface counters are read, for live traffic and link thickness."),
    _S("ping_size", "int", 56, "Scans", "Scan schedule", "ICMP payload size", range=[0, 65500], unit="bytes",
       help="Data bytes per ping packet (0-65500). Affects ICMP only, not SNMP."),
    _S("ping_timeout_ms", "int", 1000, "Scans", "Scan schedule", "Timeout", range=[100, 10000], unit="ms",
       help="How long to wait for each answer (100-10000 ms)."),
    _S("ping_parallel", "int", 64, "Scans", "Scan schedule", "Parallel checks", range=[1, 128], stepper=True,
       help="How many addresses are checked at the same time (1-128)."),
    _S("scan_subnets", "cidrs", [], "Scans", "Scan schedule", "Allowed networks",
       help="Networks in CIDR form, e.g. 10.0.0.0/16. Press Enter or comma to add. Empty: the /24 of each switch."),
    _S("scan_exclude", "ips", [], "Scans", "Scan schedule", "Never scan", help="Addresses or networks that are never pinged."),
    # ---- SNMP (the credentials stay on each host's SNMP interface in Zabbix)
    _S("snmp_max_repetitions", "int", 1, "SNMP", "Packet size", "Max-repetitions", range=[1, 100],
       help="How many rows each GETBULK asks for. 1 = one row per request (GETNEXT, the safest). Larger values mean "
            "fewer requests but bigger packets. SNMPv1 always uses GETNEXT."),
    _S("snmp_retries", "int", 1, "SNMP", "Packet size", "Retries", range=[0, 10]),
    _S("snmp_timeout_ms", "int", 3000, "SNMP", "Packet size", "Timeout", range=[500, 10000], unit="ms"),
    # ---- Topology
    _S("topo_lldp", "bool", True, "Topology", "Link sources", "LLDP"),
    _S("topo_cdp", "bool", True, "Topology", "Link sources", "CDP (Cisco)",
       help="Cisco Discovery Protocol, for Cisco devices without LLDP. LLDP is used first where a port has both."),
    _S("topo_vlans", "bool", True, "Topology", "Link sources", "VLANs (Q-BRIDGE-MIB)",
       help="Reads the VLANs of each switch port and of each device, for the VLAN filter on the map. Switches without "
            "Q-BRIDGE-MIB simply show no VLANs."),
    _S("topo_fdb", "bool", True, "Topology", "Link sources", "MAC tables (FDB)",
       help="Places devices without LLDP through the MAC addresses the switches learn. When off, devices are not "
            "placed on switch ports."),
    _S("topo_arp", "bool", True, "Topology", "Link sources", "ARP tables",
       help="IP addresses from the switches' and routers' ARP tables, in addition to this server's own."),
    _S("topo_traffic", "bool", True, "Topology", "Link sources", "Traffic correlation",
       help="Suggests links between switches without LLDP: two ports whose bytes match (what leaves one arrives at "
            "the other) while the traffic rises and falls together. Suggestions only - you confirm or reject them."),
    _S("infer_switches", "bool", True, "Topology", "Link sources", "Find hidden switches",
       help="Several devices on one port without LLDP: drawn behind an 'Unmanaged switch' (dashed), or behind the "
            "access point or hypervisor that is one of them."),
    _S("core_switch", "str", "", "Topology", "Link sources", "Core switch", maxlen=120,
       help="Name or IP of your main switch. Empty: the switch the gateway is connected to. Switches without any link "
            "evidence are drawn below it, dotted."),
    _S("detect_upstream", "bool", True, "Topology", "Link sources", "Find an upstream router",
       help="A router in front of your gateway (double NAT - a provider's router before a firewall) is drawn between "
            "the internet and the gateway, found from the second hop to the internet."),
    _S("layout", "choice", "tree", "Topology", "Layout", "Map layout",
       choices=[["tree", "Tree"], ["free", "Free: drag nodes where you want them"]],
       help="Free: links are straight lines and every node you move keeps its place after new scans (per browser). "
            "The button in the map controls switches for you; 'Reset layout' starts again from the tree."),
    # ---- Device types (the tab shows a card with a switch per category)
    _S("device_types_off", "list", [], "Device types", "Device types", "Categories switched off"),
    # ---- Wireless & WAN: UniFi controller (read-only user; independent of Zabbix)
    _S("unifi_url", "str", "", "Wireless & WAN", "UniFi controller", "Controller address", maxlen=120,
       help="e.g. https://192.168.1.5 (UniFi OS: Cloud Key Gen2, UDM) or https://192.168.1.5:8443 (older controllers). "
            "Empty: no UniFi."),
    _S("unifi_site", "str", "default", "Wireless & WAN", "UniFi controller", "Site", maxlen=40),
    _S("unifi_user", "str", "", "Wireless & WAN", "UniFi controller", "User", maxlen=60,
       help="A local user with the role View only."),
    _S("unifi_password", "secret", "", "Wireless & WAN", "UniFi controller", "Password",
       help="Stored only on the server (/etc/spidermap/secrets.json); never shown again or exported."),
    _S("unifi_fingerprint", "str", "", "Wireless & WAN", "UniFi controller", "Certificate fingerprint", maxlen=100,
       help="SHA-256 of the controller's certificate (works with its own self-signed one). Empty: not checked."),
    # ---- Measurements
    _S("wan_source", "choice", "router", "Measurements", "Traffic", "Throughput source",
       choices=[["router", "The router's WAN (SNMP, else UPnP)"], ["switch", "Switch port to the gateway"]],
       help="The router's WAN: read over SNMP when the gateway is a Zabbix host with an SNMP interface (best: 64-bit "
            "counters), else over UPnP. When neither works, the card shows a warning instead of other numbers."),
    _S("wan_interface", "str", "", "Measurements", "Traffic", "Router's WAN interface", maxlen=40,
       help="SNMP only. The interface name on the router, e.g. ether1, pppoe-out1, igb0. Empty: the interface of the "
            "default route."),
    _S("link_thickness", "bool", False, "Measurements", "Traffic", "Link thickness by traffic"),
    _S("units", "choice", "bits", "Measurements", "Traffic", "Units",
       choices=[["bits", "bits/s"], ["bytes", "bytes/s"], ["percent", "% of capacity"]]),
    _S("load_warn", "int", 70, "Measurements", "Traffic", "Load warning", range=[1, 100], unit="%"),
    _S("load_crit", "int", 90, "Measurements", "Traffic", "Load critical", range=[1, 100], unit="%",
       help="Must be higher than the warning."),
    _S("live_interval_s", "int", 60, "Measurements", "Traffic", "Status refresh", range=[30, 600], unit="s",
       choices=[[30, "30 seconds"], [60, "1 minute"], [120, "2 minutes"], [300, "5 minutes"]]),
    _S("events_keep_days", "int", 60, "Measurements", "History", "Keep history", range=[7, 365], unit="days",
       choices=[[7, "7 days"], [30, "30 days"], [60, "60 days"], [90, "90 days"], [365, "1 year"]]),
    _S("presence_keep_days", "int", 30, "Measurements", "History", "Keep who-was-online", range=[1, 90], unit="days"),
    _S("speedtest_daily", "time", "", "Measurements", "Speed test", "Daily speed test at", help="HH:MM, empty = only on request."),
    _S("speedtest_provider", "choice", "ookla", "Measurements", "Speed test", "Speed test provider",
       choices=[["ookla", "Ookla (speedtest.net)"], ["cloudflare", "Cloudflare (speed.cloudflare.com)"], ["fast", "Fast.com (Netflix)"]],
       help="Fast.com has no official API: if Netflix changes it, its tests may stop until SpiderMap is updated. "
            "nPerf cannot be used: it has no public interface."),
    _S("speedtest_server", "digits", "", "Measurements", "Speed test", "Ookla server", help="Empty = the nearest server. Ookla only."),
    # ---- Security
    _S("snmp_readonly", "bool", True, "Security", "Security", "Read-only SNMP", locked=True,
       help="SpiderMap never sends SET, even if the community may write. Always on."),
    _S("offpeak", "bool", False, "Security", "Security", "Scan only off-peak",
       help="Automatic full scans only inside this window. Scan now always works."),
    _S("offpeak_start", "time", "22:00", "Security", "Security", "Scan window from"),
    _S("offpeak_end", "time", "06:00", "Security", "Security", "Scan window until"),
    _S("scan_pps", "int", 500, "Security", "Security", "Packet limit", range=[10, 10000], unit="pps",
       help="Most ping packets per second for the whole scan, so that intrusion detection is not triggered."),
    _S("max_targets", "int", 1024, "Security", "Security", "Most addresses per scan", range=[16, 65536],
       help="A safety limit: a scan with more addresses is refused."),
    _S("permissions.settings", "level", 3, "Security", "Access", "Access to settings", range=[2, 3],
       choices=[[3, "Super admins only"], [2, "Admins and Super admins"]], help="Also: Scan now, Pause."),
    _S("permissions.view", "level", 3, "Security", "Access", "Who may open SpiderMap", range=[1, 3], choices=_LEVEL3),
    _S("permissions.edit", "level", 2, "Security", "Access", "Who may change devices", range=[2, 3], choices=_LEVEL3[1:],
       help="Never below Zabbix admins."),
    _S("permissions.speedtest", "level", 2, "Security", "Access", "Who may start speed tests", range=[1, 3], choices=_LEVEL3),
    # ---- Logging
    _S("log_level", "choice", "info", "Logging", "Logging", "Log level",
       choices=[["error", "Errors only"], ["warning", "Warnings"], ["info", "Information"], ["debug", "Debug"]]),
    _S("log_keep_days", "int", 14, "Logging", "Logging", "Keep log files", range=[1, 90], unit="days",
       choices=[[7, "7 days"], [14, "14 days"], [30, "30 days"]]),
]


SETTINGS_BY_KEY = {f["key"]: f for f in SETTINGS_SCHEMA}


SETTINGS_KEYS = set(SETTINGS_BY_KEY)


SECRET_KEYS = ("unifi_password",)


SECRETS_FILE = os.path.join(CONF_DIR, "secrets.json")        # root:spidermap 640 - never published, never exported


def load_secrets():
    return load_json(SECRETS_FILE, {}) if os.path.exists(SECRETS_FILE) else {}


def save_secrets(sec):
    save_json(SECRETS_FILE, sec)
    try:
        os.chmod(SECRETS_FILE, 0o640)
    except OSError:
        pass


def effective_settings(settings):
    """Every schema setting with its current value (or default)."""
    out = {}
    for f in SETTINGS_SCHEMA:
        key = f["key"]
        v = _get(settings, key, None)
        if key == "permissions.view" and v is None:
            v = settings.get("view_min_user_type")                     # before 0.4.0
        if key == "core_switch" and not v:
            v = (settings.get("site_topology") or {}).get("core_ip") or None   # a declared site topology (earlier forks)
        if key == "skin" and v in ("clean", "midnight", "ops"):
            v = "spidermap"                                            # 0.9.0-0.9.3 colour themes, removed
        if key == "wan_source" and v == "auto":
            v = "router"                                               # 0.5.1: "auto" meant the router first
        if key == "auto_scan" and v is None and "discovery_paused" in settings:
            v = not settings["discovery_paused"]                       # first 0.5.0 drafts
        if f.get("locked"):
            v = f["default"]
        out[key] = f["default"] if v is None else v
    return out


def validate_settings(new, current=None):
    """Check settings sent from the page. Returns (clean dict in devices.json shape, errors).
    Only schema keys are accepted; nothing is written when there is an error."""
    errors, clean = [], {}
    if not isinstance(new, dict):
        return {}, ["Settings must be an object."]
    unknown = sorted(k for k in new if k not in SETTINGS_KEYS)
    if unknown:
        errors.append("Unknown setting(s): " + ", ".join(unknown[:5]))
    for f in SETTINGS_SCHEMA:
        key, typ, label, rng = f["key"], f["type"], f["label"], f.get("range")
        if key not in new:
            continue
        v = new[key]
        try:
            if f.get("locked") or f.get("preview"):
                if v != f["default"]:
                    raise ValueError("cannot be changed" + (f" yet (planned for {f['preview']})" if f.get("preview") else ""))
            if typ == "bool":
                if not isinstance(v, bool):
                    raise ValueError("must be on or off")
            elif typ == "secret":
                v = str(v)
                if len(v) > 128 or re.search(r"[\x00-\x1f]", v):
                    raise ValueError("at most 128 characters, no control characters")
            elif typ == "list":
                if not isinstance(v, list) or any(x not in DEVICE_CATEGORY_KEYS for x in v):
                    raise ValueError("unknown category")
                v = sorted(set(v))
            elif typ == "choice":
                ok = [c[0] for c in f["choices"] if len(c) < 3]          # options marked "planned" are not accepted
                if v not in ok:
                    raise ValueError("is not one of the options")
            elif typ == "str":
                v = str(v).strip()
                if len(v) > f.get("maxlen", 200) or re.search(r"[<>\x00-\x1f]", v):
                    raise ValueError(f"at most {f.get('maxlen', 200)} characters, no < > or control characters")
            elif typ in ("int", "level"):
                if isinstance(v, bool) or not isinstance(v, (int, str)) or not re.fullmatch(r"\d{1,6}", str(v).strip()):
                    raise ValueError("must be a whole number")
                v = int(str(v).strip())
                if not rng[0] <= v <= rng[1]:
                    raise ValueError(f"must be between {rng[0]} and {rng[1]}")
            elif typ in ("cidrs", "ips"):
                if isinstance(v, str):
                    v = [x for x in re.split(r"[\s,]+", v) if x]
                if not isinstance(v, list) or len(v) > 256:
                    raise ValueError("must be a list (at most 256 entries)")
                out = []
                for x in v:
                    net = ipaddress.ip_network(str(x).strip(), strict=False)
                    if net.version != 4:
                        raise ValueError(f"{x}: only IPv4 is scanned")
                    if typ == "cidrs" and net.prefixlen < 16:
                        raise ValueError(f"{x}: too large (at most /16)")
                    out.append(str(net) if "/" in str(x) or typ == "cidrs" else str(net.network_address))
                v = out
            elif typ == "time":
                v = str(v).strip()
                m = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
                if v and not (m and int(m.group(1)) < 24 and int(m.group(2)) < 60):
                    raise ValueError("must be HH:MM, e.g. 03:30, or empty")
                if m:
                    v = f"{int(m.group(1)):02d}:{m.group(2)}"
            elif typ == "digits":
                v = str(v).strip()
                if v and not re.fullmatch(r"\d{1,8}", v):
                    raise ValueError("must be a number, or empty")
        except ValueError as e:
            errors.append(f"{label}: {e}")
            continue
        if "." in key:
            a, b = key.split(".", 1)
            clean.setdefault(a, {})[b] = v
        else:
            clean[key] = v
    if not errors:
        merged = effective_settings({**(current or {}), **clean,
                                     "permissions": {**((current or {}).get("permissions") or {}), **clean.get("permissions", {})}})
        n = scan_target_count(merged["scan_subnets"], merged["scan_exclude"])
        if n > merged["max_targets"]:
            errors.append(f"Allowed networks: {n} addresses, more than the limit of {merged['max_targets']} (Security).")
        if merged["load_crit"] <= merged["load_warn"]:
            errors.append("Load critical must be higher than Load warning.")
        if merged["offpeak"] and merged["offpeak_start"] == merged["offpeak_end"]:
            errors.append("The scan window needs different start and end times.")
        if merged["offpeak"] and (not merged["offpeak_start"] or not merged["offpeak_end"]):
            errors.append("The scan window needs a start and an end time.")
    return (clean, errors) if not errors else ({}, errors)


def scan_targets(subnets, exclude):
    """The addresses a scan will ping (without network and broadcast addresses)."""
    ex = [ipaddress.ip_network(x, strict=False) for x in exclude or []]
    for n in subnets:
        for ip in ipaddress.ip_network(n, strict=False).hosts():
            if not any(ip in e for e in ex):
                yield str(ip)


def scan_target_count(subnets, exclude):
    if not exclude:
        return sum(max(ipaddress.ip_network(n, strict=False).num_addresses - 2, 1) for n in subnets)
    return sum(1 for _ in scan_targets(subnets, exclude))


def permissions(settings):
    """Minimum Zabbix user type per action: 1 = user, 2 = admin, 3 = super admin.
    "permissions": {"view": 3, "edit": 2, "speedtest": 2}; the older "view_min_user_type" still works."""
    p = settings.get("permissions") or {}

    def lvl(key, default, lo):
        try:
            return max(lo, min(3, int(p.get(key, default))))
        except (TypeError, ValueError):
            return default
    view = lvl("view", settings.get("view_min_user_type", 3) or 3, 1)
    # changes never below admin; nobody may do more than they may see (lower values are raised, not refused)
    return {"view": view, "edit": max(view, lvl("edit", 2, 2)), "speedtest": max(view, lvl("speedtest", 2, 1)),
            "settings": max(view, lvl("settings", 3, 2))}
