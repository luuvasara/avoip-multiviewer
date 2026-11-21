#!/usr/bin/env python3
"""Infrared (IR) sending through a transmitter -- PLACEHOLDER, not working yet.

What the hardware does: the HKE120-EH23 has "one-way IR pass-through". An IR
receiver cable on the receiver (RX) picks up a remote control, and the IR
emitter cable on the transmitter (TX) repeats it at the source device (a TV box,
a projector, ...). The signal crosses the network like keyboard and mouse do.

What is missing: the network format of that IR message. It has not been
captured yet. To capture it: plug the IR receiver cable into the RX, press a
button on any remote aimed at it, and record what the RX (192.168.167.90) sends
to the TX it is tuned to, ex.:
    tcpdump -ni <nic> -w ir.pcap 'src host 192.168.167.90 and udp'
Keyboard is aa01 and mouse aa02 on UDP 4499+ID (rx/hid.py); IR is likely a
third message type on the same port, carrying either raw mark/space timings or
a decoded protocol code. IrTransport.send() below is where that goes.

What already works here: turning a remote's button into the raw timings an
emitter needs, without an IR reader, because the codes of most remotes are
already public:
  - irdb (https://github.com/probonopd/irdb) lists remotes by manufacturer and
    device as protocol, device, subdevice, function (ex. NEC1 4,-1 function 8).
  - IrpTransmogrifier (https://github.com/bengtmartensson/IrpTransmogrifier)
    renders any of its ~150 protocols to raw timings or Pronto Hex:
        irptransmogrifier render --pronto -p NEC1 -n D=4,F=8
  - Pronto Hex is the common interchange format; from_pronto() reads it.
  - nec() renders the NEC protocol (most TVs and boxes) directly.

Usage (prints timings; nothing is sent):
  ir.py nec 4 8                      # NEC device 4, function 8
  ir.py pronto "0000 006D 0022 0002 0157 00AC ..."
"""
import argparse, sys

NEC_UNIT = 562.5           # microseconds; every NEC duration is a multiple of it


def nec(device, function, subdevice=None):
    """Raw timings for one NEC frame: (carrier Hz, [mark, space, mark, ...] in us).
    subdevice None = standard NEC (address, then its inverse); a number = the
    extended form with a 16-bit address (irdb writes it as device,subdevice)."""
    if not (0 <= device <= 255 and 0 <= function <= 255):
        raise ValueError("NEC device and function are 0-255")
    hi = (~device & 0xFF) if subdevice is None or subdevice < 0 else subdevice & 0xFF
    data = device | hi << 8 | function << 16 | (~function & 0xFF) << 24
    t = [16 * NEC_UNIT, 8 * NEC_UNIT]                   # leader: 9 ms mark, 4.5 ms space
    for i in range(32):                                  # least significant bit first
        t += [NEC_UNIT, (3 if data >> i & 1 else 1) * NEC_UNIT]
    t.append(NEC_UNIT)                                   # stop mark
    t.append(108000 - sum(t))                            # gap to the 108 ms frame period
    return 38000, [round(x) for x in t]


def from_pronto(text):
    """Pronto Hex (learned/raw form, first word 0000) -> (carrier Hz, timings us)."""
    w = [int(x, 16) for x in text.split()]
    if len(w) < 4 or w[0] != 0:
        raise ValueError("only raw Pronto Hex (starting 0000) is supported")
    carrier = 1_000_000 / (w[1] * 0.241246)
    period = 1_000_000 / carrier
    pairs = (w[2] + w[3]) * 2
    body = w[4:4 + pairs]
    if len(body) != pairs:
        raise ValueError(f"Pronto Hex says {pairs} durations but has {len(body)}")
    return round(carrier), [round(n * period) for n in body]


class IrTransport:
    """Sends raw IR timings to transmitter `tx_id` through the extender network.
    Not implemented: the wire format is unknown (see the module docstring)."""

    def __init__(self, tx_id, bind="192.168.167.2"):
        self.tx_id, self.bind = tx_id, bind

    def send(self, carrier, timings):
        raise NotImplementedError(
            "IR over the extender network is not implemented: its packet format has "
            "not been captured yet (see rx/ir.py for how to capture it)")


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("nec", help="render an NEC code (device, function as in irdb)")
    n.add_argument("device", type=int)
    n.add_argument("function", type=int)
    n.add_argument("--subdevice", type=int, default=None)
    p = sub.add_parser("pronto", help="decode raw Pronto Hex")
    p.add_argument("hex")
    a = ap.parse_args()
    try:
        carrier, t = (nec(a.device, a.function, a.subdevice) if a.cmd == "nec"
                      else from_pronto(a.hex))
    except ValueError as e:
        sys.exit(f"ir.py: {e}")
    print(f"carrier {carrier} Hz, {len(t)} durations (us, mark first):")
    print(" ".join(f"{'+' if i % 2 == 0 else '-'}{x}" for i, x in enumerate(t)))
    print("not sent: IR over the extender network is not implemented yet")


if __name__ == "__main__":
    main()
