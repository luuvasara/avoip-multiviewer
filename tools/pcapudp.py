#!/usr/bin/env python3
"""Minimal stdlib pcap reader: yields (ts, src, sport, dst, dport, payload) for
UDP-over-IPv4-over-Ethernet packets. Avoids a tshark/scapy dependency."""
import struct, socket

def udp_packets(path):
    with open(path, "rb") as f:
        gh = f.read(24)
        magic = struct.unpack("<I", gh[:4])[0]
        if magic in (0xa1b2c3d4, 0xa1b23c4d):
            e = "<"
        else:
            e = ">"
            magic = struct.unpack(">I", gh[:4])[0]
        div = 1e9 if magic == 0xa1b23c4d else 1e6
        while True:
            ph = f.read(16)
            if len(ph) < 16:
                return
            sec, frac, caplen, _ = struct.unpack(e + "IIII", ph)
            d = f.read(caplen)
            off = 14
            et = d[12:14]
            if et == b"\x81\x00":
                et = d[16:18]; off = 18
            if et != b"\x08\x00" or d[off + 9] != 17:
                continue
            ihl = (d[off] & 15) * 4
            src = socket.inet_ntoa(d[off + 12:off + 16])
            dst = socket.inet_ntoa(d[off + 16:off + 20])
            u = off + ihl
            sport, dport, ulen = struct.unpack(">HHH", d[u:u + 6])
            yield sec + frac / div, src, sport, dst, dport, d[u + 8:u + ulen]
