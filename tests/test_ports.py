#!/usr/bin/env python3
"""
Tests for port identity, labels and traffic counters, using interface tables of real switches
(MikroTik CRS310 / CRS112, TP-Link Omada SG3218XP-M2 / SG2206MP, Huawei VRP) and Cisco-style names.

    python3 tests/test_ports.py

No network needed. Run it before every release. Every line must say OK.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ZABBIX_TOKEN", "test")
import spidermap as sm  # noqa: E402
import spidermap_ports as zp  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"{'OK ' if cond else 'BAD'} {name}" + (f"\n      {detail}" if detail and not cond else ""))


# ifIndex: (name, ifType, ifHighSpeed, ifOperStatus) - as the switches report them
CRS310 = {**{str(i): (f"ether{i}", "6", 0, "1") for i in range(1, 9)},
          "9": ("sfp-sfpplus1", "6", 0, "6"), "10": ("sfp-sfpplus2", "6", 0, "6"),
          "11": ("bridge1", "209", 0, "1"), "12": ("vlan", "209", 0, "1"), "13": ("lo", "1", 0, "1")}
CRS112 = {**{str(i): (f"ether{i}", "6", 0, "1") for i in range(1, 9)},
          **{str(i): (f"sfp{i}", "6", 0, "6") for i in range(9, 13)},
          "13": ("bridge", "209", 0, "1"), "14": ("lo", "1", 0, "1")}
SG3218 = {"1": ("Vlan-interface1", "6", 0, "1"),
          **{str(49152 + i): (f"two-gigabitEthernet 1/0/{i}", "6", 1000, "1") for i in range(1, 17)},
          "49169": ("ten-gigabitEthernet 1/0/17", "6", 0, "2"), "49170": ("ten-gigabitEthernet 1/0/18", "6", 0, "2")}
SG2206 = {"1": ("Vlan-interface1", "6", 0, "1"),
          **{str(49152 + i): (f"gigabitEthernet 1/0/{i}", "6", 1000, "1") for i in range(1, 7)}}
HUAWEI = {str(5 + i): (f"GigabitEthernet0/0/{i}", "6", 1000, "1") for i in range(1, 13)}
HUAWEI_XGE = {**{str(5 + i): (f"GigabitEthernet0/0/{i}", "6", 1000, "1") for i in range(1, 25)},
              **{str(40 + i): (f"XGigabitEthernet0/0/{i}", "6", 10000, "1") for i in range(1, 5)},
              "60": ("Eth-Trunk1", "161", 2000, "1"), "61": ("MEth0/0/1", "6", 100, "1"),
              "62": ("Vlanif1", "136", 0, "1"), "63": ("NULL0", "1", 0, "1")}
CISCO_2960 = {**{str(10000 + i): (f"Fa0/{i}", "6", 100, "1") for i in range(1, 25)},
              "10101": ("Gi0/1", "6", 1000, "1"), "10102": ("Gi0/2", "6", 1000, "1"),
              "1": ("Vl1", "53", 0, "1"), "14501": ("Nu0", "1", 0, "1"), "5001": ("Po1", "53", 2000, "1")}
CISCO_STACK = {**{str(1000 + i): (f"Gi1/0/{i}", "6", 1000, "1") for i in range(1, 5)},
               "1101": ("Te1/1/1", "6", 10000, "1"),
               **{str(2000 + i): (f"Gi2/0/{i}", "6", 1000, "1") for i in range(1, 5)}}


def labels_of(table, hw=None, override=None):
    ports = []
    for idx in sorted(table, key=int):
        name, t, _, _ = table[idx]
        kind, _ = zp.iface_kind(name, t)
        if kind:
            ports.append({"ifindex": idx, "name": name, "kind": kind, "media_hw": (hw or {}).get(idx, "")})
    sm.label_ports(ports, override)
    return {p["name"]: p["label"] for p in ports}, ports


def test_labels():
    lab, ports = labels_of(CRS310)
    check("MikroTik CRS310: ether1-8 -> Port 1-8, sfp-sfpplus1-2 -> Port 9-10 (SFP+)",
          [lab[f"ether{i}"] for i in range(1, 9)] == [f"Port {i}" for i in range(1, 9)]
          and lab["sfp-sfpplus1"] == "Port 9 (SFP+)" and lab["sfp-sfpplus2"] == "Port 10 (SFP+)", lab)
    check("MikroTik CRS310: bridge1, vlan, lo are not ports", not ({"bridge1", "vlan", "lo"} & set(lab)), lab)

    lab, _ = labels_of(CRS112)
    check("MikroTik CRS112: ether1-8 -> Port 1-8, sfp9-12 -> Port 9-12 (SFP)",
          [lab[f"ether{i}"] for i in range(1, 9)] == [f"Port {i}" for i in range(1, 9)]
          and [lab[f"sfp{i}"] for i in range(9, 13)] == [f"Port {i} (SFP)" for i in range(9, 13)], lab)

    lab, _ = labels_of(SG3218)
    check("TP-Link SG3218XP-M2: 1/0/1-18 -> Port 1-18, Vlan-interface1 is not a port",
          "Vlan-interface1" not in lab and lab["two-gigabitEthernet 1/0/1"] == "Port 1"
          and lab["ten-gigabitEthernet 1/0/17"] == "Port 17" and len(lab) == 18, lab)

    lab, _ = labels_of(SG2206)
    check("TP-Link SG2206MP: gigabitEthernet 1/0/1-6 -> Port 1-6, Vlan-interface1 is not a port",
          "Vlan-interface1" not in lab and [lab[f"gigabitEthernet 1/0/{i}"] for i in range(1, 7)]
          == [f"Port {i}" for i in range(1, 7)], lab)

    lab, _ = labels_of(HUAWEI)
    check("Huawei GigabitEthernet0/0/1-12 -> Port 1-12 (same as before 0.3.0)",
          [lab[f"GigabitEthernet0/0/{i}"] for i in range(1, 13)] == [f"Port {i}" for i in range(1, 13)], lab)

    lab, _ = labels_of(HUAWEI_XGE)
    check("Huawei GE0/0/1-24 + XGE0/0/1-4 -> Port 1-28, Eth-Trunk1 -> LAG 1, MEth/Vlanif/NULL are not ports",
          lab["GigabitEthernet0/0/24"] == "Port 24" and lab["XGigabitEthernet0/0/1"] == "Port 25"
          and lab["Eth-Trunk1"] == "LAG 1" and not ({"MEth0/0/1", "Vlanif1", "NULL0"} & set(lab)), lab)

    lab, _ = labels_of(CISCO_2960)
    check("Cisco Fa0/1-24 + Gi0/1-2 -> Port 1-26, Po1 -> LAG 1, Vl1/Nu0 are not ports",
          lab["Fa0/1"] == "Port 1" and lab["Fa0/24"] == "Port 24" and lab["Gi0/1"] == "Port 25"
          and lab["Gi0/2"] == "Port 26" and lab["Po1"] == "LAG 1" and "Vl1" not in lab, lab)

    lab, _ = labels_of(CISCO_STACK)
    check("Cisco stack: Gi1/0/x -> Port 1/x, Gi2/0/x -> Port 2/x",
          lab["Gi1/0/1"] == "Port 1/1" and lab["Te1/1/1"] == "Port 1/5" and lab["Gi2/0/4"] == "Port 2/4", lab)

    for nm, table in (("CRS310", CRS310), ("CRS112", CRS112), ("SG3218", SG3218), ("SG2206", SG2206),
                      ("HUAWEI_XGE", HUAWEI_XGE), ("CISCO_2960", CISCO_2960), ("CISCO_STACK", CISCO_STACK)):
        lab, ports = labels_of(table)
        check(f"{nm}: every label and every identity is unique",
              len(set(lab.values())) == len(lab) and len({p['pid'] for p in ports}) == len(ports), lab)


def test_sfp():
    lab, _ = labels_of(SG3218)
    check("TP-Link 'ten-gigabitEthernet' is not guessed (it can be SFP+ or 10GBASE-T copper)",
          lab["ten-gigabitEthernet 1/0/17"] == "Port 17", lab)
    lab, _ = labels_of(SG3218, override={"17": "SFP+", "18": "SFP+"})
    check("devices.json port_media marks ports 17-18 of the SG3218XP as SFP+",
          lab["ten-gigabitEthernet 1/0/17"] == "Port 17 (SFP+)" and lab["ten-gigabitEthernet 1/0/18"] == "Port 18 (SFP+)"
          and lab["two-gigabitEthernet 1/0/16"] == "Port 16", lab)
    lab, _ = labels_of(CRS310, override={"9": "copper"})
    check("port_media 'copper' removes a mark", lab["sfp-sfpplus1"] == "Port 9" and lab["sfp-sfpplus2"] == "Port 10 (SFP+)", lab)
    lab, _ = labels_of(HUAWEI_XGE, hw={"41": "SFP+", "42": "SFP+"})
    check("hardware inventory (ENTITY-MIB) marks Huawei XGE ports as SFP+",
          lab["XGigabitEthernet0/0/1"] == "Port 25 (SFP+)" and lab["XGigabitEthernet0/0/3"] == "Port 27"
          and lab["GigabitEthernet0/0/1"] == "Port 1", lab)
    _, ports = labels_of(CRS310)
    p9 = next(p for p in ports if p["name"] == "sfp-sfpplus1")
    check("an SFP port keeps its identity (ifIndex) and its number", p9["pid"] == "9" and p9["base"] == "Port 9", p9)
    texts = {"SFP+ port": "SFP+", "1000BaseSFP": "SFP", "QSFP28 100G": "QSFP28", "10GBase-T copper": "",
             "Gigabit Ethernet Port": "", "sfp-sfpplus1": "SFP+", "SFP28 port": "SFP28"}
    check("SFP types are read from text only when it says so",
          all(zp.media_from_text(t) == want for t, want in texts.items()),
          {t: zp.media_from_text(t) for t in texts})
    real = zp.at.snmpwalk
    walks = {zp.ENT_ALIAS: {"1001.0": ".1.3.6.1.2.1.2.2.1.1.10101", "1002.0": ".1.3.6.1.2.1.2.2.1.1.10001",
                            "1003.0": ".1.3.6.1.2.1.2.2.1.1.10102"},
             zp.ENT_DESCR: {"1001": "SFP+ port", "1002": "Gigabit Ethernet Port", "1003": "10/100/1000BaseTX"},
             zp.ENT_NAME: {"1001": "Te1/1/1", "1002": "Gi1/0/1", "1003": "Gi1/0/2"}}
    zp.at.snmpwalk = lambda auth, target, oid: walks.get(oid, {})
    try:
        got = zp.entity_media([], "x")
    finally:
        zp.at.snmpwalk = real
    check("hardware inventory: only the port described as SFP+ is marked", got == {"10101": "SFP+"}, got)
    zp.at.snmpwalk = lambda auth, target, oid: {}
    try:
        none = zp.entity_media([], "x")
    finally:
        zp.at.snmpwalk = real
    check("hardware inventory: a device without ENTITY-MIB just gives no marks", none == {}, none)


def test_report():
    def report(table):
        names = {i: v[0] for i, v in table.items()}
        types = {i: v[1] for i, v in table.items()}
        speeds = {i: str(v[2]) for i, v in table.items()}
        opers = {i: v[3] for i, v in table.items()}
        return sm.check_ports_report("sw", names, types, speeds, opers)
    for nm, table, clash in (("CRS310", CRS310, True), ("SG3218XP", SG3218, True), ("SG2206MP", SG2206, True),
                             ("CRS112", CRS112, False), ("Huawei", HUAWEI, False)):
        text, found = report(table)
        check(f"--check-ports finds the 0.2.x conflict on {nm}: {'yes' if clash else 'none'}", found == clash, text)
    text, _ = report(CRS310)
    check("--check-ports names the clashing pair ether1 + sfp-sfpplus1", "ether1 + sfp-sfpplus1" in text, text)


def test_counters():
    G = 10 ** 9
    first = sm.counter_rate(None, (0, 0, 0, 100, 0, False), 1000)
    check("counters: the first sample is only a baseline", first[4] == "first")
    r = sm.counter_rate((0.0, 1000, 2000, 100, 0, False), (5.0, 1000 + 100_000_000, 2000 + 5_000_000, 600, 0, False), 1000)
    check("counters: 100 MB in 5 s on a 1 Gbps link = 160 Mbps",
          r[4] is None and abs(r[0] - 160e6) < 1 and abs(r[1] - 8e6) < 1, r)
    r = sm.counter_rate((0.0, 5 * G, 5 * G, 9_000_000, 0, False), (5.0, 1000, 1000, 50, 0, False), 1000)
    check("counters: device restart (sysUpTime back) is not traffic", r[4] == "reboot", r)
    r = sm.counter_rate((0.0, 5 * G, 5 * G, 100, 0, False), (5.0, 10, 10, 600, 0, False), 1000)
    check("counters: a 64-bit counter going back is a reset, not traffic", r[4] == "reset", r)
    r = sm.counter_rate((0.0, 1000, 1000, 100, 0, False), (5.0, 2000, 2000, 600, 77, False), 1000)
    check("counters: ifCounterDiscontinuityTime changed -> sample ignored", r[4] == "discontinuity", r)
    r = sm.counter_rate((0.0, 0, 0, 100, 0, False), (5.0, 2 * G, 0, 600, 0, False), 1000)
    check("counters: 3.2 Gbps on a 1 Gbps link is impossible", r[4] == "impossible", r)
    r = sm.counter_rate((0.0, 2 ** 32 - 1000, 10, 100, None, True), (5.0, 500, 20, 600, None, True), 100)
    check("counters: 32-bit counter wrapping around is handled", r[4] is None and r[2] == 1500, r)
    r = sm.counter_rate((0.0, 0, 0, 100, 0, False), (0.4, 10, 10, 100, 0, False), 1000)
    check("counters: samples too close together are skipped", r[4] == "short", r)
    # the 0.2.x bug: ether1 (36 days of traffic) compared with sfp-sfpplus1 (no module, counters 0)
    r = sm.counter_rate((0.0, 0, 0, 318975100, 0, False), (5.0, 7 * 10 ** 12, 3 * 10 ** 12, 318975600, 0, False), 2500)
    check("counters: the 0.2.x mix-up (36 days of traffic in 5 s) would now be rejected", r[4] == "impossible", r)
    r = sm.counter_rate((0.0, 1000, 1000, 100, 0, False), (5.0, 10 ** 12, 1000, 600, 0, False), 0)
    check("counters: unknown link speed still has a sane upper limit", r[4] == "impossible", r)


def test_zabbix_items():
    by_name = {"two-gigabitEthernet 1/0/1": "49153"}
    cases = [("net.if.in[ifHCInOctets.49153]", "", ("rx", "49153")),
             ("net.if.out[ifHCOutOctets.7]", "", ("tx", "7")),
             ("net.if.status[ifOperStatus.12]", "", ("st", "12")),
             ("net.if.in.errors[ifInErrors.5]", "", ("ein", "5")),
             ("net.if.in.discards[ifInDiscards.5]", "", (None, None)),
             ("net.if.in[custom]", "Interface two-gigabitEthernet 1/0/1(Port1): Bits received", ("rx", "49153"))]
    ok = all(sm.zabbix_port_item(k, n, by_name) == want for k, n, want in cases)
    check("Zabbix items are matched by ifIndex (also TP-Link names with spaces)", ok,
          [(k, sm.zabbix_port_item(k, n, by_name)) for k, n, _ in cases])


def main():
    test_labels()
    test_sfp()
    test_report()
    test_counters()
    test_zabbix_items()
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
