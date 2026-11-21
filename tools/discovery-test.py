#!/usr/bin/env python3
"""Checks the discovery threshold and forget time-out (Matrix.shown) without a network.
Run inside the gateway container: docker exec <container> python3 /app/tools/discovery-test.py"""
import sys, time, types
sys.path.insert(0, "/app/webclient"); sys.argv = ["x"]
import gateway as g
o = types.SimpleNamespace(last={}, present={}, run_since={}, probe_since={}, listed={}, stale=15, asleep=False)
for name in ("active", "is_present", "shown"):
    setattr(o, name, types.MethodType(getattr(g.Matrix, name), o))
t = time.monotonic()
checks = []
o.last[7] = t; o.run_since[7] = t - 2;  checks.append(("2 s burst not listed", not o.shown(7)))
o.run_since[7] = t - 6;                  checks.append(("6 s steady listed", o.shown(7)))
o.last[7] = t - 30; o.listed[7] = t - 30; checks.append(("gone 30 s still listed", o.shown(7)))
o.listed[7] = t - 61;                    checks.append(("gone 61 s dropped", not o.shown(7) and 7 not in o.listed))
o.present[8] = t; o.probe_since[8] = t;  checks.append(("one probe answer not listed", not o.shown(8)))
o.probe_since[8] = t - 5;                checks.append(("probe answers for 5 s listed", o.shown(8)))
o.listed[9] = t - 600; o.asleep = True;  checks.append(("asleep: list frozen", o.shown(9)))
o.asleep = False
for name, ok in checks:
    print("PASS" if ok else "FAIL", name)
sys.exit(0 if all(ok for _, ok in checks) else 1)
