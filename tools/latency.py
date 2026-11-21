#!/usr/bin/env python3
"""Server-side video latency of one feed: time from a screen change arriving from
the transmitter (a big non-key HEVC frame on UDP) to the same change leaving
mediamtx (a big non-key H.264 packet on RTSP). Needs something changing on the
screen (e.g. `top` refreshing). Run inside the kvm container:
  latency.py --id 5 --secs 20
"""
import argparse, socket, struct, subprocess, threading, time

ap = argparse.ArgumentParser()
ap.add_argument("--id", type=int, default=5)
ap.add_argument("--secs", type=float, default=20)
ap.add_argument("--iface", default="192.168.167.3")
ap.add_argument("--udp-min", type=int, default=3000, help="bytes: a 'change' frame in")
ap.add_argument("--rtsp-min", type=int, default=3000, help="bytes: a 'change' packet out")
a = ap.parse_args()
ins, outs = [], []

def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", 4399 + a.id))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton("239.1.1.1") + socket.inet_aton(a.iface))
    s.settimeout(0.5); size = 0; key = False; end = time.time() + a.secs
    while time.time() < end:
        try: p = s.recv(2048)
        except socket.timeout: continue
        idx, mark = struct.unpack("<HH", p[-4:])
        if idx == 0:
            size = 0; key = p[4:6] == b"\x40\x01"        # VPS = keyframe
        size += len(p) - 4
        if mark == 0x00AA and not key and size >= a.udp_min:
            ins.append(time.time())

def rtsp():
    cmd = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
           "-show_entries", "packet=size,flags", "-of", "csv=p=0",
           f"rtsp://127.0.0.1:8554/kvmweb{a.id}"]
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    end = time.time() + a.secs
    for line in pr.stdout:
        t = time.time()
        if t > end: break
        try: size, flags = line.strip().split(",")[:2]
        except ValueError: continue
        if "K" not in flags and int(size) >= a.rtsp_min:
            outs.append(t)
    pr.kill()

th = [threading.Thread(target=udp), threading.Thread(target=rtsp)]
[t.start() for t in th]; [t.join() for t in th]
lat = []
for t in ins:                       # first output change after each input change
    later = [o - t for o in outs if 0 <= o - t < 1.5]
    if later: lat.append(min(later))
lat.sort()
print(f"changes in: {len(ins)}, out: {len(outs)}, matched: {len(lat)}")
if lat:
    print("server latency ms: min %.0f  median %.0f  max %.0f" %
          (lat[0] * 1000, lat[len(lat) // 2] * 1000, lat[-1] * 1000))
