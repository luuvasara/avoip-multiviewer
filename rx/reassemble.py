#!/usr/bin/env python3
"""TESmart HKE120 AV-over-IP software RX: reassemble the HEVC video multicast.

Reads UDP multicast group:port (video 239.1.1.1:4400), strips the 4-byte
per-packet trailer (LE16 fragment index + LE16 end-marker; 0xaa00 = last
fragment of a frame), and writes the raw Annex-B HEVC elementary stream to
stdout for ffmpeg. Own-hardware interop tool.

Frames are emitted whole. A frame with a missing or out-of-order fragment is
dropped instead of being written with a hole, because a truncated NAL unit
corrupts the decoder until the next IDR (about 2 s) while a cleanly missing
frame is concealed. Drop counts go to stderr every 10 s when non-zero.

--pcap FILE replays a capture instead of joining the group (offline testing).
"""
import argparse, json, os, socket, struct, sys, time

END = 0x00AA  # bytes "aa 00" read as LE16
MAX_FRAGS = 4096  # 4 MB/frame; real keyframes seen so far are <= 162 fragments


def live(group, port, iface):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.bind(("", port))
    mreq = socket.inet_aton(group) + socket.inet_aton(iface)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    while True:
        yield s.recv(2048)


def replay(path, port):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    from pcapudp import udp_packets
    for _, _, _, _, dport, payload in udp_packets(path):
        if dport == port:
            yield payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="239.1.1.1")
    ap.add_argument("--port", type=int, default=4400)
    ap.add_argument("--iface", default="192.168.167.2",
                    help="local IP of the NIC on the extender subnet")
    ap.add_argument("--pcap", help="replay this capture instead of going live")
    ap.add_argument("--no-skip", action="store_true",
                    help="pass incomplete frames and their dependents to the decoder "
                         "(shows partial/garbled pictures instead of holding the last good one)")
    ap.add_argument("--status", help="write running totals as JSON to this file "
                    "every 2 s (read by the web gateway to flag a degraded source)")
    a = ap.parse_args()

    src = replay(a.pcap, a.port) if a.pcap else live(a.group, a.port, a.iface)
    out = sys.stdout.buffer
    parts, expect, bad = [], 0, False
    frames = dropped = 0
    last_report = last_status = time.monotonic()
    total = {"frames": 0, "dropped": 0, "unterminated": 0, "skipped": 0,
             "last_cut_bytes": None, "cut_sizes": {}}
    # Byte size of each incomplete frame: the same size every time = the
    # transmitter's own frame limit; varying sizes = loss on the way.
    # After an incomplete frame, every following frame references a broken
    # picture; decoding them shows garbage ("blinking"). Instead drop them all
    # until the next complete keyframe, so the viewer holds the last good image.
    skipping = True       # also at start: nothing decodes before the first keyframe

    def emit(data):
        nonlocal frames, skipping
        if skipping and not a.no_skip:
            if not data[4:6] == b"\x40\x01":    # not a keyframe (VPS first)
                total["skipped"] += 1
                return
            skipping = False
        out.write(data)
        out.flush()
        frames += 1
        total["frames"] += 1

    def broken():
        nonlocal skipping
        skipping = True

    def write_status():
        # Totals since start; atomic replace so the reader never sees half a file.
        if a.status:
            tmp = a.status + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(total, ts=time.time()), f)
            os.replace(tmp, a.status)

    for pkt in src:
        if len(pkt) <= 4:
            continue
        idx, mark = struct.unpack("<HH", pkt[-4:])
        if idx == 0:                      # a new frame starts
            # A new idx 0 with no end marker on the previous frame means that
            # frame ended without one. The SigmaStar SSC337DE encoder does this
            # when a keyframe overruns its per-frame bitstream buffer: it sends
            # a keyframe cut off at exactly 114 x 1024 B (the bottom then garbles
            # on the hardware receiver too, as the bytes are never sent). If the
            # unfinished frame had no gaps it is emitted whole under --no-skip;
            # it is counted as `unterminated` with its cut size either way.
            if parts and not bad:
                total["unterminated"] += 1
                cut = sum(len(x) for x in parts)
                total["last_cut_bytes"] = cut
                sizes = total["cut_sizes"]
                if str(cut) in sizes or len(sizes) < 8:
                    sizes[str(cut)] = sizes.get(str(cut), 0) + 1
                if a.no_skip:
                    emit(b"".join(parts))
                else:
                    total["skipped"] += 1
                    broken()
            elif parts or bad:
                dropped += 1
                total["dropped"] += 1
                broken()
            parts, expect, bad = [], 0, False
        if idx != expect:
            bad = True
        expect = idx + 1
        if len(parts) >= MAX_FRAGS:       # malformed stream: never grow without bound
            bad = True
        else:
            parts.append(pkt[:-4])
        if mark == END:
            if bad:
                dropped += 1
                total["dropped"] += 1
                broken()
            else:
                emit(b"".join(parts))
            parts, expect, bad = [], 0, False
        now = time.monotonic()
        if now - last_status > 2:
            write_status()
            last_status = now
        if dropped and now - last_report > 10:
            print(f"reassemble: {frames} frames ok, {dropped} dropped "
                  f"(lost/reordered fragments)", file=sys.stderr, flush=True)
            frames = dropped = 0
            last_report = now

    if a.pcap:
        write_status()
        print(f"reassemble: {total['frames']} frames out, {total['dropped']} dropped, "
              f"{total['unterminated']} unterminated, {total['skipped']} skipped, "
              f"cut sizes {total['cut_sizes']}", file=sys.stderr)


if __name__ == "__main__":
    main()
