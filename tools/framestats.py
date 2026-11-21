#!/usr/bin/env python3
"""Passive per-transmitter frame statistics for the TESmart HKE120 video multicast.

Joins 239.1.1.1 on the video port of each given transmitter ID for --secs seconds
and reports, per ID: frame counts, keyframe/other size distribution, frames that
ended without the end marker (with their sizes), fragment gaps, NAL units per
frame and bitrate. Receive-only; sends nothing to the units.

Usage: framestats.py 2 5 [--secs 60] [--iface 192.168.167.2] [--json out.json]
"""
import argparse, json, select, socket, struct, time

END = 0x00AA


def sock(group, port, iface):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.bind((group, port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(group) + socket.inet_aton(iface))
    return s


def pct(v, p):
    return sorted(v)[min(len(v) - 1, int(len(v) * p))] if v else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", type=int, nargs="+")
    ap.add_argument("--secs", type=float, default=60)
    ap.add_argument("--group", default="239.1.1.1")
    ap.add_argument("--iface", default="192.168.167.2")
    ap.add_argument("--json")
    a = ap.parse_args()
    st = {}
    socks = {}
    for i in a.ids:
        socks[sock(a.group, 4399 + i, a.iface)] = i
        st[i] = dict(pkts=0, bytes=0, parts=[], expect=0, bad=False, key=[], other=[],
                     unterminated=[], gaps=0, nals=[], srcs=set(), maxpay=0)
    end = time.monotonic() + a.secs

    def finish(d, complete):
        data = b"".join(d["parts"])
        if d["bad"]:
            d["gaps"] += 1
        elif not complete:
            d["unterminated"].append(len(data))
        else:
            iskey = data[4:6] == b"\x40\x01"
            (d["key"] if iskey else d["other"]).append(len(data))
            d["nals"].append(data.count(b"\x00\x00\x01"))
        d["parts"], d["expect"], d["bad"] = [], 0, False

    while time.monotonic() < end:
        r, _, _ = select.select(list(socks), [], [], 0.5)
        for s in r:
            pkt, addr = s.recvfrom(2048)
            d = st[socks[s]]
            d["srcs"].add(addr[0])
            if len(pkt) <= 4:
                continue
            d["pkts"] += 1
            d["bytes"] += len(pkt)
            d["maxpay"] = max(d["maxpay"], len(pkt) - 4)
            idx, mark = struct.unpack("<HH", pkt[-4:])
            if idx == 0 and (d["parts"] or d["bad"]):
                finish(d, False)
            if idx != d["expect"]:
                d["bad"] = True
            d["expect"] = idx + 1
            d["parts"].append(pkt[:-4])
            if mark == END:
                finish(d, True)
    out = {}
    for i, d in st.items():
        k, o, n = d["key"], d["other"], d["nals"]
        out[i] = dict(
            sources=sorted(d["srcs"]), packets=d["pkts"], max_payload=d["maxpay"],
            mbit_s=round(d["bytes"] * 8 / a.secs / 1e6, 3),
            fps=round((len(k) + len(o) + len(d["unterminated"]) + d["gaps"]) / a.secs, 2),
            keyframes=len(k), key_min=min(k, default=None), key_p50=pct(k, .5), key_max=max(k, default=None),
            other_frames=len(o), other_p50=pct(o, .5), other_p99=pct(o, .99), other_max=max(o, default=None),
            unterminated=len(d["unterminated"]), unterminated_sizes=sorted(set(d["unterminated"])),
            frames_with_gaps=d["gaps"], nals_per_frame=sorted(set(n)))
    print(json.dumps(out, indent=1))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(dict(ts=time.strftime("%Y-%m-%d %H:%M %Z"), secs=a.secs, stats=out), f, indent=1)


if __name__ == "__main__":
    main()
