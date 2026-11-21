#!/usr/bin/env python3
"""Read-only facts about one TESmart HKE120 unit over its open root telnet.

The units ship a passwordless root telnet on :23 (a vendor debug leftover,
documented in docs/firmware-internals.md). This speaks just enough of the
protocol to log in and run ONE fixed read-only batch, then parses the output
into a dict for the multiviewer: firmware vintage, uptime, CPU load, channel
ID, and live encoder statistics. It never writes anything to the unit.

Python 3.13 dropped telnetlib, so this is a tiny raw-socket client (same shape
as tools/telnet_probe.py). asyncio so the gateway can poll several units
without blocking its event loop.
"""
import asyncio, re

IAC, DONT, DO, WONT, WILL, SB, SE = 0xff, 0xfe, 0xfd, 0xfc, 0xfb, 0xfa, 0xf0
MARK = "__TXINFO_7c1d__"

# One batch; each value on its own line as KEY=... so parsing is trivial.
# Facts that only change when the unit reboots or is reflashed: read once per boot.
STATIC = (
    'echo VER=$(cat /proc/version)\n'
    'echo ROLE=$([ -d /customer/hdmiap ] && echo TX || echo RX)\n'
    # Firmware fingerprint: checksum of the main app (encoder on a TX, player on
    # the RX) and of the factory update service, if present. Same checksum =
    # same build; 4465df48 is the encoder on every TX seen so far (2026-11-20).
    'echo APP=$(md5sum /customer/hdmiap/hdmi_convertor /customer/hdmiplayer/rtspclient 2>/dev/null | head -1 | cut -c1-8)\n'
    'echo UPD=$(md5sum /customer/trans 2>/dev/null | cut -c1-8)\n'
    'echo MEMTOTAL=$(awk "/MemTotal/{print \\$2}" /proc/meminfo)\n'
)
# Values that move: read on every poll.
LIVE = (
    'echo UP=$(cat /proc/uptime)\n'
    'echo LOAD=$(cat /proc/loadavg)\n'
    'echo CH=$(cat /customer/config_file 2>/dev/null)\n'
    'echo TRANS=$(pidof trans >/dev/null && echo yes || echo no)\n'
    'echo MEMFREE=$(awk "/MemAvailable/{print \\$2}" /proc/meminfo)\n'
    'top -bn1 2>/dev/null | grep -m1 -i cpu: | sed "s/^/CPU=/"\n'
)


_num = r"-?\d+\.?\d*"




async def read_info(host, iface_ip=None, timeout=6.0, static=True):
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, 23), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        return await asyncio.wait_for(_session(r, w, static), timeout * 2)
    except (OSError, asyncio.TimeoutError, ValueError):
        return None
    finally:
        w.close()
        try:
            await w.wait_closed()
        except OSError:
            pass


async def _drain_iac(r, w, want, deadline):
    """Read until `want` (bytes) appears, refusing every telnet option; return text."""
    acc = bytearray()
    while want not in acc:
        try:
            d = await asyncio.wait_for(r.read(4096), max(0.1, deadline - _now()))
        except asyncio.TimeoutError:
            break
        if not d:
            break
        i, out, resp = 0, bytearray(), bytearray()
        while i < len(d):
            b = d[i]
            if b != IAC:
                out.append(b); i += 1; continue
            if i + 2 >= len(d):
                break
            c = d[i + 1]
            if c in (DO, DONT):
                resp += bytes([IAC, WONT, d[i + 2]])
            elif c in (WILL, WONT):
                resp += bytes([IAC, DONT, d[i + 2]])
            i += 3
        if resp:
            w.write(bytes(resp)); await w.drain()
        acc += out
    return acc.decode("latin1", "replace")


def _now():
    return asyncio.get_event_loop().time()


async def _session(r, w, static=True):
    deadline = _now() + 8
    txt = await _drain_iac(r, w, b"login:", deadline)
    w.write(b"root\n"); await w.drain()
    await _drain_iac(r, w, b"# ", deadline)
    batch = (STATIC if static else '') + LIVE + f'echo {MARK}\n'
    w.write(batch.encode()); await w.drain()
    out = await _drain_iac(r, w, MARK.encode(), deadline)
    return _parse(out)


def _parse(out):
    kv = {}
    for line in out.splitlines():
        m = re.match(r"([A-Z]+)=(.*)", line.strip())
        if m:
            kv[m.group(1)] = m.group(2)
    info = {}
    ver = kv.get("VER", "")
    m = re.search(r"#(\d+)\s+(\w*)\s*PREEMPT.*?(\d{4})", ver)
    km = re.search(r"Linux version (\S+)", ver)
    if km:
        info["kernel"] = km.group(1)
    if m:
        info["kernel_build"] = f"#{m.group(1)}"
        info["smp"] = "SMP" in ver
        bd = re.search(r"(\w{3})\s+(\w{3})\s+(\d+).*?(\d{4})", ver)
        if bd:
            info["kernel_date"] = f"{bd.group(4)}-{bd.group(2)}-{int(bd.group(3)):02d}"
    if "UP" in kv:
        try:
            info["uptime_s"] = int(float(kv["UP"].split()[0]))
        except (ValueError, IndexError):
            pass
    if "LOAD" in kv:
        p = kv["LOAD"].split()
        if len(p) >= 3:
            info["load"] = [float(x) for x in p[:3]]
            # single core on these SoCs; load ~ busy fraction, cap display at 100%
            info["cpu_pct"] = min(100, round(float(p[0]) * 100))
    if kv.get("CH", "").startswith("CH"):
        # /customer/config_file stores the communication ID minus one: TX ID 2
        # (.11) holds CH01, ID 5 (.14) CH04, and the RX holding CH04 is the one
        # linked to ID 5 (measured 2026-11-20). Report the ID the panel shows.
        raw = int(re.sub(r"\D", "", kv["CH"]) or 0)
        info["channel_raw"] = raw
        info["channel"] = raw + 1
    if "ROLE" in kv:
        info["role"] = kv["ROLE"]
    if "APP" in kv:
        info["app_md5"] = kv["APP"] or None
        info["upd_md5"] = kv.get("UPD") or None
    if "TRANS" in kv:
        info["trans"] = kv["TRANS"] == "yes"
    try:
        if kv.get("MEMTOTAL"):
            info["mem_total_kb"] = int(kv["MEMTOTAL"])
        if kv.get("MEMFREE"):
            info["mem_free_kb"] = int(kv["MEMFREE"])
    except ValueError:
        pass
    # Real CPU from top: "CPU: 81.8% usr 18.1% sys 0.0% nic 0.0% idle ..."
    m = re.search(r"idle", kv.get("CPU", ""))
    if m:
        mi = re.search(r"([\d.]+)%?\s*idle", kv["CPU"])
        if mi:
            info["cpu_pct"] = round(100 - float(mi.group(1)))
    return info or None


if __name__ == "__main__":
    import json, sys
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.167.11"
    print(json.dumps(asyncio.run(read_info(host)), indent=1))
