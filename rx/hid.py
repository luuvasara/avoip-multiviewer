#!/usr/bin/env python3
"""TESmart HKE120 software RX: send keyboard/mouse input to the transmitter.

Wire format, read from a public capture of the same product family
(apalrd.net, 2022; hardware RX 192.168.167.90 -> TX 192.168.167.10):
unicast UDP to TX port 4500, one datagram per USB boot-protocol report.

  keyboard  aa 01 | modifiers | 00 | up to 6 HID usage codes   (10 bytes)
  mouse     aa 02 | buttons | dx int8 | dy int8 | wheel int8    (6 bytes)

A key press is a report holding the key; the release is an all-zero report.

Tested on HKE120-EH23 units (2026-11-20): works with no hardware receiver tuned
to the transmitter. Whatever is typed lands on the machine plugged into the TX,
so try a harmless key first (ex. `hid.py key shift`).

  hid.py type 'hello'        hid.py key enter        hid.py key ctrl+alt+f2
  hid.py move 20 -5          hid.py click left
"""
import argparse, socket, struct, time

MODS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "gui": 0x08,
        "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rgui": 0x80}
NAMED = {"enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B,
         "space": 0x2C, "delete": 0x4C, "right": 0x4F, "left": 0x50,
         "down": 0x51, "up": 0x52, "home": 0x4A, "end": 0x4D,
         "pgup": 0x4B, "pgdn": 0x4E,
         **{f"f{n}": 0x39 + n for n in range(1, 13)}}
BUTTONS = {"left": 1, "right": 2, "middle": 4}
# US layout: character -> (usage, needs shift)
_PLAIN = "\n\x1b\b\t -=[]\\\0;'`,./"   # \0: 0x32 is the non-US # key, not "#"
_SHIFT = "\0\0\0\0\0_+{}|~:\"~<>?"
CHARS = {c: (0x04 + i, False) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
CHARS.update({c.upper(): (u, True) for c, (u, _) in list(CHARS.items())})
CHARS.update({c: (0x1E + i, False) for i, c in enumerate("1234567890")})
CHARS.update({c: (0x1E + i, True) for i, c in enumerate("!@#$%^&*()")})
CHARS.update({c: (0x28 + i, False) for i, c in enumerate(_PLAIN) if c != "\0"})
CHARS.update({c: (0x28 + i, True) for i, c in enumerate(_SHIFT) if c != "\0"})
CHARS["~"] = (0x35, True)              # shift+` on US (0x32 is the non-US key)
CHARS["\r"] = CHARS["\n"]            # Enter either way; "\r\n" is folded first


# Typographic characters pasted from documents -> the ASCII a US keyboard has
# (as PiKVM's kvmd/keyboard/printer.py does for quotes and dashes).
TYPOGRAPHIC = str.maketrans({"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2018": "'",
                             "\u2019": "'", "\u2013": "-", "\u2014": "--", "\u00a0": " ",
                             "\u2026": "...", "\u2212": "-"})


def plan(text):
    """Text -> list of (shift, usage) taps for a US-layout host, plus the
    characters that have no key there (non-ASCII, most control characters)."""
    taps, skipped = [], []
    text = text.replace("\r\n", "\n").translate(TYPOGRAPHIC)
    for ch in text:
        # Esc and Backspace inside pasted text would act on the target; skip them.
        k = None if ch in "\x1b\b" else CHARS.get(ch)
        if k is None:
            skipped.append(ch)
        else:
            taps.append((k[1], k[0]))
    return taps, skipped


class Hid:
    def __init__(self, tx="192.168.167.10", port=4500, bind="192.168.167.2",
                 gap=0.03):
        self.addr, self.gap = (tx, port), gap
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind((bind, 0))

    def keyboard(self, mods=0, keys=()):
        keys = tuple(keys)[:6]
        self.s.sendto(b"\xaa\x01" + bytes([mods, 0]) +
                      bytes(keys).ljust(6, b"\0"), self.addr)
        time.sleep(self.gap)

    def mouse(self, buttons=0, dx=0, dy=0, wheel=0):
        self.s.sendto(b"\xaa\x02" + struct.pack("<Bbbb", buttons, dx, dy, wheel),
                      self.addr)
        time.sleep(self.gap)

    def tap(self, mods=0, key=0):
        self.keyboard(mods, (key,) if key else ())
        self.keyboard()

    def type(self, text):
        for ch in text:
            usage, shift = CHARS[ch]
            self.tap(MODS["shift"] if shift else 0, usage)

    def combo(self, spec):
        mods, key = 0, 0
        for part in spec.lower().split("+"):
            if part in MODS:
                mods |= MODS[part]
            elif part in NAMED:
                key = NAMED[part]
            elif part in CHARS:
                key = CHARS[part][0]
            else:
                raise SystemExit(f"unknown key '{part}' (ex. enter, f2, ctrl+c)")
        self.tap(mods, key)

    def move(self, dx, dy):
        while dx or dy:                      # int8 per report, so send in steps
            sx, sy = max(-127, min(127, dx)), max(-127, min(127, dy))
            self.mouse(0, sx, sy)
            dx, dy = dx - sx, dy - sy

    def click(self, button="left"):
        self.mouse(BUTTONS[button])
        self.mouse(0)


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--id", type=int, default=1,
                    help="transmitter ID 1-16; ID n is 192.168.167.(9+n), "
                         "measured for 1 and 2. Port 4499+n is a guess by "
                         "analogy with video/audio (ex. --id 2)")
    ap.add_argument("--bind", default="192.168.167.2",
                    help="local IP on the extender subnet")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("type").add_argument("text")
    sub.add_parser("key").add_argument("combo")
    m = sub.add_parser("move")
    m.add_argument("dx", type=int)
    m.add_argument("dy", type=int)
    sub.add_parser("click").add_argument("button", choices=BUTTONS,
                                         nargs="?", default="left")
    a = ap.parse_args()
    h = Hid(f"192.168.167.{9 + a.id}", 4499 + a.id, a.bind)
    if a.cmd == "type":
        h.type(a.text)
    elif a.cmd == "key":
        h.combo(a.combo)
    elif a.cmd == "move":
        h.move(a.dx, a.dy)
    else:
        h.click(a.button)


if __name__ == "__main__":
    main()
