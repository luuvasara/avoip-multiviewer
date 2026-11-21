#!/usr/bin/env python3
"""Minimal raw-socket telnet client for the TESmart HKE120's open root shell.

Python 3.13 removed telnetlib, so this speaks just enough of the protocol:
connect, refuse every IAC option (WONT/DONT), log in as root (the units ship an
empty-password root login), then run a batch of commands read from --cmdfile and
print the combined session. Strictly a diagnostic helper -- it only sends the
commands you give it; keep that list read-only.

Usage: telnet_probe.py <host> --cmdfile cmds.txt [--user root] [--timeout 30]
"""
import argparse, socket, sys, time

IAC, DONT, DO, WONT, WILL, SB, SE = 0xff, 0xfe, 0xfd, 0xfc, 0xfb, 0xfa, 0xf0
MARKER = "=====PROBE_DONE_7f3a2b====="


def strip_iac(sock, data, buf):
    """Append data to buf, pull out IAC negotiations (refusing each option),
    return the plain bytes. Leaves an incomplete trailing IAC in buf."""
    buf += data
    out, resp, i, n = bytearray(), bytearray(), 0, len(buf)
    while i < n:
        b = buf[i]
        if b != IAC:
            out.append(b); i += 1; continue
        if i + 1 >= n:
            break
        c = buf[i + 1]
        if c in (DO, DONT):
            if i + 2 >= n: break
            resp += bytes([IAC, WONT, buf[i + 2]]); i += 3
        elif c in (WILL, WONT):
            if i + 2 >= n: break
            resp += bytes([IAC, DONT, buf[i + 2]]); i += 3
        elif c == SB:
            j = i + 2
            while j < n and buf[j] != SE:
                j += 1
            if j >= n: break
            i = j + 1
        elif c == IAC:
            out.append(IAC); i += 2
        else:
            i += 2
    del buf[:i]
    if resp:
        sock.sendall(bytes(resp))
    return bytes(out)


def read_until(s, buf, subs, timeout):
    """Read until any needle in subs appears, or timeout. Returns (text, needle)."""
    acc = bytearray()
    end = time.time() + timeout
    s.settimeout(1.0)
    while time.time() < end:
        try:
            d = s.recv(4096)
        except socket.timeout:
            d = b""
        if d:
            acc += strip_iac(s, d, buf)
            for p in subs:
                if p in acc:
                    return acc.decode("latin1"), p
        else:
            time.sleep(0.1)
    return acc.decode("latin1"), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--user", default="root")
    ap.add_argument("--cmdfile", required=True)
    ap.add_argument("--timeout", type=float, default=30)
    a = ap.parse_args()
    with open(a.cmdfile) as f:
        cmds = f.read()

    buf = bytearray()
    s = socket.create_connection((a.host, 23), timeout=6)

    txt, m = read_until(s, buf, [b"ogin:", b"sername:", b"# ", b"$ "], 15)
    sys.stdout.write(txt)
    if m in (b"ogin:", b"sername:"):
        s.sendall(a.user.encode() + b"\r\n")
        txt, m = read_until(s, buf, [b"assword", b"# ", b"$ ", b"~#"], 12)
        sys.stdout.write(txt)
        if m == b"assword":
            s.sendall(b"\r\n")  # empty password
            txt, _ = read_until(s, buf, [b"# ", b"$ ", b"~#"], 12)
            sys.stdout.write(txt)

    # run the read-only batch, bounded by a trailing marker echo
    s.sendall(cmds.strip().encode() + b"\r\necho " + MARKER.encode() + b"\r\n")
    txt, hit = read_until(s, buf, [MARKER.encode() + b"\r"], a.timeout)
    sys.stdout.write(txt)
    sys.stdout.write("\n[probe: marker seen]\n" if hit else "\n[probe: TIMEOUT, no marker]\n")
    try:
        s.sendall(b"exit\r\n")
    except OSError:
        pass
    s.close()


if __name__ == "__main__":
    main()
