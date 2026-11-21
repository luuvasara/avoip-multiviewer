#!/usr/bin/env python3
"""Web KVM multiviewer gateway for TESmart HKE120-EH23 AV-over-IP extenders.

- Discovers transmitters: TX ID n multicasts video to 239.1.1.1:(4399+n) from
  192.168.167.(9+n) (measured for IDs 1, 2, 4, 5). Every candidate port is joined
  and watched; a feed is active while packets keep arriving.
- A transmitter whose computer is off or asleep sends no video at all (measured
  2026-11-20: TX5 with no HDMI input, 0 packets), so video alone would hide it.
  Each candidate address is therefore also probed for presence, and a present
  but silent transmitter is listed as "no signal" with a wake action.
- Runs one browser feed per active transmitter (rx/feed-web.sh <n>: reassemble
  HEVC + PCM audio -> H.264/Opus -> mediamtx path kvmweb<n>), restarts it if it
  dies, stops it when the transmitter goes quiet.
- Serves the matrix page and relays the viewer's keyboard/mouse to the selected
  transmitter in the extender's UDP HID format (rx/hid.py). No hardware receiver
  has to be tuned to the transmitter for input to work (measured 2026-11-20).
- Proxies the browser's WebRTC (WHEP) signalling to mediamtx, so the page, its
  video and its input all sit behind one origin and one login, and work behind
  a reverse proxy under any path prefix. Login and proxy headers: access.py.
"""
import argparse, asyncio, json, os, signal, socket, sys, time
import aiohttp
from aiohttp import web, WSMsgType

HERE = os.path.dirname(os.path.abspath(__file__))
RX = os.path.join(HERE, "..", "rx")
sys.path.insert(0, RX)
import hid  # noqa: E402
import txinfo  # noqa: E402
from access import Access  # noqa: E402

def _version():
    for p in (os.path.join(HERE, "..", "VERSION"), os.path.join(HERE, "VERSION")):
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            pass
    return "unknown"


VERSION = _version()      # vYYYY.MM.DD + letter, shown in the page footer
GROUP = "239.1.1.1"
IDS = range(1, 17)
PROBE_PORT = 9        # discard: nothing listens there on the units (netstat, 2026-11-20)
PROBE_EVERY = 5       # seconds between presence sweeps
# A channel is listed only after its signal has lasted DISCOVER_S (a transmitter
# whose ID button is being cycled visits every channel for a moment) and is
# dropped FORGET_S after the last sign of it (a unit moved to another channel).
DISCOVER_S = float(os.environ.get("DISCOVER_S", 5))
FORGET_S = float(os.environ.get("FORGET_S", 60))
UNWANTED_GRACE = 30   # s a feed keeps running after its last viewer left or hid
INFO_EVERY = 30      # seconds between telnet info reads per present unit
WAKE_GAP = 2          # seconds between wake taps per transmitter
TYPE_MAX = 20000      # characters per "type" request
TYPE_GAP = float(os.environ.get("TYPE_GAP_MS", 20)) / 1000   # default pause after each key report
OCR_LANG = os.environ.get("OCR_LANG", "eng")    # Tesseract language(s), ex. eng+fin
# Re-announce the multicast join this often (seconds; 0 = off). Linux reports a
# join only when it happens and then only in answer to an IGMP query; a switch
# with IGMP snooping and no querier on the network (ex. many unmanaged switches)
# forgets the membership after a few minutes and stops forwarding the video.
IGMP_REFRESH = float(os.environ.get("IGMP_REFRESH_S", 60))
OCR_MAX_BYTES = 4 * 1024 * 1024
OCR_MAX_PIXELS = 25_000_000   # PNG width x height; the page sends at most ~10 MP


def tx_ip(n):
    return f"192.168.167.{9 + n}"


class Matrix:
    def __init__(self, iface, mtx, stale, status_dir, idle_after, state_dir=None):
        self.iface, self.mtx, self.stale = iface, mtx, stale
        # Idle: with nobody watching (no /feeds poll, no control session) for
        # idle_after s, stop every feed and leave the multicast groups; the next
        # page load wakes it. 0 = never idle. A viewer is an input socket whose
        # page is VISIBLE: a background or minimised tab says so and stops
        # counting, or a forgotten tab would keep the feeds running for good
        # (measured 2026-11-20: 6 h at ~0.8 core per feed with nobody watching).
        self.idle_after, self.last_use, self.visible = idle_after, time.monotonic(), set()
        # Publishing keeps it awake while the target stream is on air (see watch_air).
        self.on_air = False
        # Feeds run on demand: transmitter n is converted only while a visible
        # page has asked to view it (or the stream copy is on air). Each feed
        # costs ~0.8 core of software HEVC decode, so an open page that shows
        # only "Click to view" tiles must cost nothing. socket -> set of IDs, or
        # None for a page that predates the "view" message (it gets every feed).
        self.want, self.unwanted_at = {}, {}
        self.socks, self.loop, self.asleep = {}, None, False
        self.status_dir = status_dir
        self.last, self.src, self.pkts = {}, {}, {}
        self.present = {}     # n -> monotonic time the unit last answered a probe
        self.run_since = {}   # n -> start of the current unbroken run of video packets
        self.probe_since = {} # n -> start of the current unbroken run of probe answers
        self.listed = {}      # n -> last time the channel met the DISCOVER_S test
        self.info = {}        # n -> live values (uptime, cpu, memory, channel)
        self.info_at = {}     # n -> monotonic time of that read
        # n -> facts that only change on reboot/reflash (role, kernel, firmware
        # fingerprint). Read once per unit boot, kept in a JSON file so a gateway
        # restart shows them at once instead of after the next telnet round.
        self.static_path = os.path.join(state_dir, "unit-static.json") if state_dir else None
        self.static = self._load_static()
        # Hardware variant letters (A, B, ...) per role, one per distinct build
        # fingerprint, handed out in the order first seen and kept on disk so a
        # letter never moves to another build. Seeded from the cache, whose order
        # put the 11/2024 TX first, so vA = 11/2024 and vB = 12/2025.
        self.variants_path = os.path.join(state_dir, "variants.json") if state_dir else None
        self.variants = self._load_json(self.variants_path, {})
        for st in self.static.values():
            self._variant(st)
        self._stat_prev = None
        self.procs, self.spawned = {}, {}
        # Optional copy of each feed to another RTSP server as <PUBLISH_BASE>/tx<n>.
        # Off unless PUBLISH_BASE is set, so the matrix never depends on that
        # server. The URL may carry a password: never print it.
        self.publish = os.environ.get("PUBLISH_BASE", "").rstrip("/")
        self.relays, self.relay_spawned = {}, {}

    # -- discovery ------------------------------------------------------------
    def watch(self, loop):
        self.loop = loop
        self.join()

    def join(self):
        for n in IDS:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", 4399 + n))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         socket.inet_aton(GROUP) + socket.inet_aton(self.iface))
            s.setblocking(False)
            self.loop.add_reader(s.fileno(), self._drain, n, s)
            self.socks[n] = s

    def igmp_report(self):
        """Send one IGMPv3 membership report for GROUP, exactly what the kernel
        sends when a querier asks (RFC 3376 4.2: record type 2, MODE_IS_EXCLUDE,
        no sources; to 224.0.0.22, TTL 1, Router Alert option)."""
        rec = bytes([2, 0, 0, 0]) + socket.inet_aton(GROUP)
        msg = bytearray(bytes([0x22, 0, 0, 0, 0, 0, 0, 1]) + rec)
        c = sum(int.from_bytes(msg[i:i + 2], "big") for i in range(0, len(msg), 2))
        while c >> 16:
            c = (c & 0xFFFF) + (c >> 16)
        msg[2:4] = (~c & 0xFFFF).to_bytes(2, "big")
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IGMP)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.iface))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_OPTIONS, bytes([0x94, 4, 0, 0]))
            s.sendto(bytes(msg), ("224.0.0.22", 0))
        finally:
            s.close()

    async def igmp_refresh(self):
        warned = False
        while True:
            await asyncio.sleep(IGMP_REFRESH)
            if not self.socks:
                continue                  # not joined (idle): nothing to keep alive
            try:
                self.igmp_report()
                warned = False
            except OSError as e:          # ex. no CAP_NET_RAW: say so once, keep running
                if not warned:
                    print(f"IGMP refresh failed ({e}); video may stop behind a snooping "
                          "switch without a querier. Set IGMP_REFRESH_S=0 to silence.", flush=True)
                    warned = True

    def leave(self):
        for s in self.socks.values():
            self.loop.remove_reader(s.fileno())
            s.close()                 # closing drops the group membership (IGMP leave)
        self.socks = {}

    def touch(self):
        """Someone is using the matrix: note it, and wake up if idle."""
        self.last_use = time.monotonic()
        if self.asleep:
            print("waking: viewer back, rejoining the transmitters", flush=True)
            self.asleep = False
            self.join()

    def _drain(self, n, s):
        try:
            while True:
                _, addr = s.recvfrom(2048)
                now = time.monotonic()
                if n not in self.last or now - self.last[n] > self.stale:
                    self.run_since[n] = now
                self.last[n] = now
                self.src[n] = addr[0]
                self.pkts[n] = self.pkts.get(n, 0) + 1
        except BlockingIOError:
            pass

    def active(self, n):
        return n in self.last and time.monotonic() - self.last[n] < self.stale

    # -- presence (independent of video) ----------------------------------------
    async def _probe_one(self, n):
        """True if the unit at TX n's address is up. One UDP byte to the discard
        port (9, closed on the units): a live host answers ICMP port-unreachable,
        which a connected UDP socket reports as ConnectionRefusedError. Needs no
        privileges and touches no service on the unit."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        try:
            s.bind((self.iface, 0))
            s.connect((tx_ip(n), PROBE_PORT))
            s.send(b"\0")
            for _ in range(5):                 # error arrives within a few ms on a LAN
                await asyncio.sleep(0.1)
                try:
                    s.recv(16)
                except BlockingIOError:
                    continue
                except ConnectionRefusedError:
                    return True
                except OSError:                # host/net unreachable: not there
                    return False
            return False
        except ConnectionRefusedError:
            return True
        except OSError:
            return False
        finally:
            s.close()

    async def probe(self):
        while True:
            if not self.asleep:
                up = await asyncio.gather(*(self._probe_one(n) for n in IDS))
                now = time.monotonic()
                for n, ok in zip(IDS, up):
                    if ok:
                        if n not in self.present or now - self.present[n] > 2 * PROBE_EVERY + 1:
                            self.probe_since[n] = now
                        if n not in self.present:
                            print(f"tx {n}: present at {tx_ip(n)}", flush=True)
                        self.present[n] = now
            await asyncio.sleep(PROBE_EVERY)

    def _load_static(self):
        try:
            with open(self.static_path) as f:
                return {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return default

    # Sticker (factory) dates seen on units of each build, keyed (app_md5, upd_md5).
    # The unit cannot report its sticker, so this is our own observation; add a
    # date here when another unit is checked, to narrow the changeover window.
    STICKERS = {("4465df48", None): "11/2024",          # TX4, 192.168.167.13
                ("4465df48", "f94552d5"): "12/2025"}    # TX2 and TX5
    # Model name printed on the units' labels, where the owner has read it.
    LABELS = {("4465df48", None): "HKE12MMA20",         # 11/2024 unit: no suffix = old version
              ("4465df48", "f94552d5"): "HKE12MMA20-TX"}  # 12/2025 unit
    VARIANT_KEYS = ("role", "kernel", "kernel_build", "kernel_date", "smp", "app_md5", "upd_md5")

    def _variant(self, st):
        """Letter of this unit's build fingerprint within its role; assigns the
        next free letter (and saves) the first time a fingerprint is seen."""
        if not st or not st.get("role"):
            return None
        sig = json.dumps([st.get(k) for k in self.VARIANT_KEYS])
        if sig not in self.variants:
            used = sum(1 for k in self.variants if json.loads(k)[0] == st["role"])
            self.variants[sig] = chr(ord("A") + used)
            print(f"new {st['role']} variant v{self.variants[sig]}: {sig}", flush=True)
            if self.variants_path:
                try:
                    with open(self.variants_path + ".tmp", "w") as f:
                        json.dump(self.variants, f, indent=1)
                    os.replace(self.variants_path + ".tmp", self.variants_path)
                except OSError as e:
                    print(f"variants not saved: {e}", flush=True)
        return self.variants[sig]

    def _save_static(self):
        if not self.static_path:
            return
        tmp = self.static_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.static, f, indent=1)
            os.replace(tmp, self.static_path)
        except OSError as e:
            print(f"unit static cache not saved: {e}", flush=True)

    def _need_static(self, n):
        """Static facts are re-read only when missing or the unit has rebooted
        since they were read (boot time moved by more than a minute)."""
        st = self.static.get(n)
        live = self.info.get(n)
        if not st:
            return True
        if live and "uptime_s" in live and "boot_epoch" in st:
            boot_now = time.time() - live["uptime_s"]
            return abs(boot_now - st["boot_epoch"]) > 60
        return False

    async def _read_unit(self, n):
        want_static = self._need_static(n)
        info = await txinfo.read_info(tx_ip(n), self.iface, static=want_static)
        if not info:
            return
        static_keys = ("kernel", "kernel_build", "kernel_date", "smp", "role",
                       "app_md5", "upd_md5", "mem_total_kb")
        if want_static and "kernel" in info:
            st = {k: info[k] for k in static_keys if k in info}
            if "uptime_s" in info:
                st["boot_epoch"] = round(time.time() - info["uptime_s"])
            st["read_at"] = round(time.time())   # epoch; the page shows it in local time
            prev = self.static.get(n)
            what = ("read" if not prev else
                    "CHANGED (reflashed?)" if prev.get("app_md5") != st.get("app_md5")
                    or prev.get("upd_md5") != st.get("upd_md5") else "re-read after reboot")
            self.static[n] = st
            self._save_static()
            self._variant(st)
            print(f"tx {n}: static facts {what}: "
                  f"{st.get('role')} kernel {st.get('kernel_build')} app {st.get('app_md5')} "
                  f"update-svc {st.get('upd_md5') or 'absent'}", flush=True)
        self.info[n] = {k: v for k, v in info.items() if k not in static_keys}
        self.info_at[n] = time.monotonic()

    async def poll_info(self):
        """Every INFO_EVERY s, read live values (uptime, CPU, memory, channel) from
        each present unit over its root telnet, plus the static facts once per unit
        boot (read-only, docs/firmware-internals.md). Best-effort: a unit that does
        not answer keeps its last reading, whose age the page shows."""
        while True:
            if not self.asleep:
                due = [n for n in IDS if self.is_present(n) and self.shown(n)
                       and time.monotonic() - self.info_at.get(n, 0) > INFO_EVERY]
                if due:
                    await asyncio.gather(*(self._read_unit(n) for n in due))
            await asyncio.sleep(2)

    def host_stats(self):
        """Load, CPU and memory of THIS machine (the one running the gateway).
        loadavg and /proc/stat are the host's even inside the container
        (host network+PID is used, and loadavg is a global kernel figure). CPU% is
        the busy fraction since the previous call."""
        out = {}
        try:
            with open("/proc/loadavg") as f:
                out["load"] = [float(x) for x in f.read().split()[:3]]
        except OSError:
            pass
        out["ncpu"] = os.cpu_count() or 1
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()[1:]
                vals = [int(x) for x in parts]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                total = sum(vals)
                prev = self._stat_prev
                self._stat_prev = (idle, total)
                if prev and total > prev[1]:
                    out["cpu_pct"] = round(100 * (1 - (idle - prev[0]) / (total - prev[1])))
        except (OSError, IndexError, ZeroDivisionError):
            pass
        try:
            mi = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    mi[k] = int(v.split()[0])
            tot, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
            if tot:
                out["mem_total_mb"] = round(tot / 1024)
                out["mem_used_pct"] = round((tot - avail) * 100 / tot)
        except (OSError, ValueError):
            pass
        return out

    def info_for(self, n):
        st, live = self.static.get(n), self.info.get(n)
        if not st and not live:
            return None
        out = dict(st or {})
        out.pop("boot_epoch", None)
        if st:
            out["variant"] = self._variant(st)
            out["sticker"] = self.STICKERS.get((st.get("app_md5"), st.get("upd_md5")))
            out["label"] = self.LABELS.get((st.get("app_md5"), st.get("upd_md5")))
        if live:
            out.update(live)
            out["age"] = round(time.monotonic() - self.info_at.get(n, 0), 1)
            tot, free = out.get("mem_total_kb"), live.get("mem_free_kb")
            if tot and free is not None:
                out["mem_used_pct"] = round((tot - free) * 100 / tot)
        else:
            out["age"] = None           # static facts from the cache, no live read yet
        return out

    def shown(self, n):
        """Listed on the page: its video or its probe answers have lasted DISCOVER_S
        without a break, and the last such moment is under FORGET_S ago."""
        now = time.monotonic()
        steady = ((self.active(n) and now - self.run_since.get(n, now) >= DISCOVER_S) or
                  (self.is_present(n) and now - self.probe_since.get(n, now) >= DISCOVER_S))
        if steady:
            self.listed[n] = now
            return True
        if n in self.listed and now - self.listed[n] < FORGET_S:
            return True
        if n in self.listed:
            del self.listed[n]
            print(f"tx {n}: gone for {FORGET_S:.0f} s, no longer listed", flush=True)
        return False

    def is_present(self, n):
        """Answered a probe recently, or is sending video (which proves it too)."""
        return self.active(n) or (
            n in self.present
            and time.monotonic() - self.present[n] < 3 * PROBE_EVERY + 1)

    # -- per-feed pipelines ----------------------------------------------------
    def wanted(self, n):
        if self.publish and self.on_air:
            return True
        return any(w is None or n in w for ws, w in self.want.items() if ws in self.visible)

    async def watch_air(self):
        """While publishing is configured: with STREAM_STATE_URL set, ask it every
        10 s whether the target stream is on air (JSON with "air": true|false) and
        copy only then; unreachable counts as off air. Without it, always copy."""
        if not self.publish:
            return
        url = os.environ.get("STREAM_STATE_URL", "").strip()
        if not url:
            print("publishing: no STREAM_STATE_URL, copying feeds continuously", flush=True)
            self.on_air = True
            self.touch()
            return
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as s:
            while True:
                try:
                    async with s.get(url) as r:
                        air = bool((await r.json(content_type=None)).get("air"))
                except Exception as e:           # any failure = treat as off air
                    air, why = False, f" ({type(e).__name__})"
                else:
                    why = ""
                if air != self.on_air:
                    print(f"stream {'on air: staying awake' if air else 'off air'}{why}", flush=True)
                    self.on_air = air
                    if air:
                        self.touch()
                    else:
                        self.last_use = time.monotonic()
                await asyncio.sleep(10)

    async def supervise(self):
        env = dict(os.environ, IFACE_IP=self.iface, MTX=self.mtx,
                   STATUS_DIR=self.status_dir)
        env.pop("PUBLISH_BASE", None)    # feeds don't need the stream password
        while True:
            if (self.idle_after and not self.asleep
                    and not (self.publish and self.on_air)
                    and not self.visible
                    and time.monotonic() - self.last_use > self.idle_after):
                print(f"idle: no viewer for {self.idle_after:.0f} s, stopping feeds "
                      f"and leaving the multicast groups", flush=True)
                for n in list(self.procs):
                    self.stop(n)
                self.leave()
                self.asleep = True
            if self.asleep:
                await asyncio.sleep(2)
                continue
            for n in IDS:
                p = self.procs.get(n)
                alive = p is not None and p.returncode is None
                now = time.monotonic()
                if self.wanted(n):
                    self.unwanted_at.pop(n, None)
                elif alive and now - self.unwanted_at.setdefault(n, now) > UNWANTED_GRACE:
                    print(f"feed {n}: nobody viewing it, stopping", flush=True)
                    self.stop(n)
                    alive = False
                if self.active(n) and self.wanted(n) and not alive and \
                        time.monotonic() - self.spawned.get(n, 0) > 3:
                    print(f"feed {n}: starting (TX {self.src.get(n)})", flush=True)
                    self.procs[n] = await asyncio.create_subprocess_exec(
                        os.path.join(RX, "feed-web.sh"), str(n), env=env,
                        start_new_session=True)
                    self.spawned[n] = time.monotonic()
                elif not self.active(n) and alive:
                    print(f"feed {n}: transmitter quiet, stopping", flush=True)
                    self.stop(n)
                if self.publish:
                    await self.relay(n, alive and self.active(n))
            await asyncio.sleep(2)

    async def relay(self, n, want):
        """Stream-copy kvmweb<n> (video only) to PUBLISH_BASE/tx<n>; restart on exit."""
        r = self.relays.get(n)
        up = r is not None and r.returncode is None
        if want and not up and time.monotonic() - self.relay_spawned.get(n, 0) > 5:
            print(f"feed {n}: publishing copy as tx{n}", flush=True)
            self.relays[n] = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-i", f"{self.mtx}/kvmweb{n}",
                "-map", "0:v:0", "-c", "copy",
                "-f", "rtsp", "-rtsp_transport", "tcp", f"{self.publish}/tx{n}",
                start_new_session=True, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)   # stderr could echo the URL
            self.relay_spawned[n] = time.monotonic()
        elif not want and up:
            os.killpg(r.pid, signal.SIGTERM)

    def stop(self, n):
        p = self.procs.get(n)
        if p is not None and p.returncode is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def health(self, n):
        """Reassembler totals for feed n, or None before its first report."""
        try:
            with open(os.path.join(self.status_dir, f"tesmart-feed-{n}.json")) as f:
                h = json.load(f)
        except (OSError, ValueError):
            return None
        h["updated"] = round(time.time() - h.pop("ts", 0), 1)
        return h

    def running(self, n):
        p = self.procs.get(n)
        return p is not None and p.returncode is None


# -- HTTP ----------------------------------------------------------------------
async def index(request):
    return web.FileResponse(os.path.join(HERE, "index.html"),
                            headers={"Cache-Control": "no-cache"})


async def healthz(request):
    """Liveness for Docker/Kubernetes/proxies; needs no login, reveals nothing."""
    return web.json_response({"ok": True})


async def host_stats_route(request):
    m, acc = request.app["m"], request.app["access"]
    return web.json_response({"name": request.app["args"].host_name, **m.host_stats(),
                              "version": VERSION, "auth": {"mode": acc.mode, "user": request.get("user")}})


WHEP_PASS_HEADERS = ("Content-Type", "Link", "ETag", "Accept-Patch", "Allow")


async def whep(request):
    """Relay WHEP signalling to mediamtx on this host (127.0.0.1 only), so the
    browser never talks to mediamtx's HTTP port: the video is behind the same
    login and origin as the page. Only the SDP exchange passes here; the media
    itself flows over WebRTC (UDP/TCP ICE) straight from mediamtx."""
    n = int(request.match_info["n"])
    if n not in IDS:
        raise web.HTTPNotFound()
    rest = request.match_info.get("rest", "")
    url = f"{request.app['args'].webrtc_url}/kvmweb{n}/whep" + (f"/{rest}" if rest else "")
    fwd = {k: request.headers[k] for k in ("Content-Type", "If-Match")
           if k in request.headers}
    try:
        async with request.app["http"].request(request.method, url, headers=fwd,
                                               data=await request.read()) as r:
            body = await r.read()
            out = {k: r.headers[k] for k in WHEP_PASS_HEADERS if k in r.headers}
            loc = r.headers.get("Location")
            if loc:
                # mediamtx answers /kvmweb<n>/whep/<session>; relative to this
                # request (.../whep/<n>) that resource is "<n>/<session>".
                out["Location"] = f"{n}/{loc.rstrip('/').rsplit('/', 1)[-1]}"
            return web.Response(status=r.status, body=body, headers=out)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"whep {n}: video server unreachable ({type(e).__name__})", flush=True)
        raise web.HTTPBadGateway(text="video server unreachable")


async def feeds(request):
    m = request.app["m"]
    if request.query.get("bg") != "1":    # a hidden tab's poll must not keep it awake
        m.touch()
    out = []
    for n in sorted(n for n in set(m.last) | set(m.present) if m.shown(n)):
        out.append({"id": n, "tx": m.src.get(n, tx_ip(n)), "active": m.active(n),
                    "running": m.running(n), "present": m.is_present(n),
                    # null = no video seen since the gateway started
                    "age": round(time.monotonic() - m.last[n], 1) if n in m.last else None,
                    "info": m.info_for(n),
                    # The unit let the gateway log in as root with no password
                    # (how unit details are read): proof its telnet is open.
                    "telnet_open": n in m.info or n in m.static,
                    "health": m.health(n) if m.running(n) else None,
                    "whep": f"whep/{n}"})     # relative: works under any proxy prefix
    return web.json_response(out)


async def ocr(request):
    """Recognise the text in one PNG (a region the viewer selected on a video
    frame, cropped and enlarged in the browser) with Tesseract, run locally.
    Nothing is recognised unless a viewer asks; images are not kept."""
    acc = request.app["access"]
    if not acc.origin_ok(request):         # only this page may spend the CPU
        raise web.HTTPForbidden(text="cross-origin request refused")
    if request.content_type != "image/png":
        raise web.HTTPUnsupportedMediaType(text="send image/png")
    slots = request.app["ocr_slots"]
    if slots.locked():
        raise web.HTTPTooManyRequests(text="text recognition is busy, try again in a moment")
    body = await request.read()            # whole body; client_max_size caps it
    if len(body) < 24 or not body.startswith(b"\x89PNG\r\n\x1a\n") or body[12:16] != b"IHDR":
        raise web.HTTPBadRequest(text="not a PNG image")
    w, h = int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big")
    if not w or not h or w * h > OCR_MAX_PIXELS:
        raise web.HTTPBadRequest(text=f"image of {w}x{h} pixels is too large")
    async with slots:
        try:
            p = await asyncio.create_subprocess_exec(
                "tesseract", "stdin", "stdout", "-l", OCR_LANG, "--psm", "6",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # one thread per run: Tesseract's OpenMP threads slow it down on
                # small images and would compete with the video conversions
                env=dict(os.environ, OMP_THREAD_LIMIT="1"))
        except FileNotFoundError:
            raise web.HTTPNotImplemented(text="text recognition is not installed (tesseract)")
        try:
            out, err = await asyncio.wait_for(p.communicate(body), 30)
        except asyncio.TimeoutError:
            raise web.HTTPGatewayTimeout(text="text recognition took over 30 s")
        finally:
            if p.returncode is None:       # timeout, shutdown or cancelled request
                p.kill()
                await p.wait()
    if p.returncode != 0:
        msg = err.decode("utf-8", "replace").strip().splitlines()
        print(f"ocr failed ({p.returncode}): {msg[-1] if msg else 'no message'}", flush=True)
        raise web.HTTPInternalServerError(
            text=f"text recognition failed: {msg[-1] if msg else 'exit ' + str(p.returncode)}")
    text = out.decode("utf-8", "replace").replace("\f", "").rstrip()
    return web.json_response({"text": text, "lang": OCR_LANG})


SKIP_NAMES = {"\x1b": "Esc", "\b": "Backspace"}   # readable names for skipped control keys


async def type_text(ws, h, text, gap=TYPE_GAP):
    """Type text on the transmitter as key taps (US layout), reporting progress
    to the page. Cancelled by "type-stop", live keys, a channel switch or the
    page leaving. A send error ends it as "error", never as "done"."""
    taps, skipped = hid.plan(text[:TYPE_MAX])
    total, done, state = len(taps), 0, "done"
    try:
        for shift, usage in taps:
            mods = hid.MODS["shift"] if shift else 0
            h.keyboard(mods, (usage,))
            await asyncio.sleep(gap)
            h.keyboard()
            await asyncio.sleep(gap)
            done += 1
            if done % 20 == 0 and not ws.closed:
                await ws.send_json({"t": "typed", "state": "running", "done": done, "total": total})
    except asyncio.CancelledError:
        state = "stopped"
        raise
    except Exception as e:                  # ex. sendto failing, socket closing
        state = "error"
        print(f"typing stopped by an error after {done} of {total}: {e!r}", flush=True)
    finally:
        try:
            h.keyboard()                    # never leave a key held
        except OSError:
            pass
        if not ws.closed:
            try:
                await ws.send_json({"t": "typed", "state": state, "done": done, "total": total,
                                    "skipped": " ".join(SKIP_NAMES.get(c, c) if c.isprintable() or c in SKIP_NAMES
                                                        else f"U+{ord(c):04X}" for c in sorted(set(skipped)))[:200],
                                    "truncated": len(text) > TYPE_MAX})
            except Exception:
                pass


def hid_for(app, n):
    cache = app["hids"]
    if n not in cache:
        try:
            cache[n] = hid.Hid(tx_ip(n), 4499 + n, app["args"].bind, gap=0)
        except OSError as e:          # alias missing: let the kernel pick a source
            print(f"hid bind {app['args'].bind} failed ({e}); using any", flush=True)
            cache[n] = hid.Hid(tx_ip(n), 4499 + n, "0.0.0.0", gap=0)
    return cache[n]


def clamp8(v):
    return max(-127, min(127, int(v)))


def release(app, n):
    if n is not None:
        h = hid_for(app, n)
        h.keyboard()
        h.mouse()


async def ws_input(request):
    # Only this page may drive the consoles (access.Access.origin_ok).
    acc = request.app["access"]
    who = f"{acc.forwarded(request)['for']} ({request.get('user')})"
    if not acc.origin_ok(request):
        print(f"input refused: origin {request.headers.get('Origin')!r} is not this page "
              f"({acc.forwarded(request)['host']!r}; add it to ALLOWED_ORIGINS if it is)", flush=True)
        raise web.HTTPForbidden(text="cross-origin input refused")
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    app, sel, typing = request.app, None, None
    m = app["m"]

    def stop_typing():
        nonlocal typing
        if typing and not typing.done():
            typing.cancel()
        typing = None
    m.touch()
    m.visible.add(ws)         # pages that predate the "vis" message count as visible
    m.want[ws] = None         # ...and, until they send "view", want every feed
    print(f"input client connected: {who}", flush=True)
    try:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                e = json.loads(msg.data)
                t = e.get("t")
                if t == "sel":
                    n = int(e["id"])
                    if n in IDS and n != sel:
                        stop_typing()
                        release(app, sel)
                        sel = n
                elif t == "wake":
                    # One Left Shift tap: wakes a sleeping computer without typing
                    # anything. Needs no linked hardware receiver (measured on TX 2
                    # 2026-11-20, README).
                    n = int(e["id"])
                    now = time.monotonic()
                    if n in IDS and now - app["woke"].get(n, 0) >= WAKE_GAP:
                        app["woke"][n] = now
                        h = hid_for(app, n)
                        h.keyboard(0x02)
                        await asyncio.sleep(0.05)
                        h.keyboard()
                        print(f"wake tap sent to tx {n} ({tx_ip(n)})", flush=True)
                elif t == "view":
                    # The tiles this page is showing video for (clicked to view).
                    m.want[ws] = {int(x) for x in e.get("ids", [])} & set(IDS)
                elif t == "vis":
                    # The page reports its tab visibility; only visible tabs are viewers.
                    if e.get("v") == "hidden":
                        if ws in m.visible:
                            m.visible.discard(ws)
                            m.last_use = time.monotonic()
                            release(app, sel)
                            print(f"input client hidden: {who}", flush=True)
                    else:
                        if ws not in m.visible:
                            print(f"input client visible: {who}", flush=True)
                        m.visible.add(ws)
                        m.touch()
                elif sel is None:
                    continue
                elif t == "k":
                    stop_typing()             # live keys take over from typed text
                    hid_for(app, sel).keyboard(
                        int(e.get("mods", 0)) & 0xFF,
                        [int(x) & 0xFF for x in e.get("keys", [])][:6])
                elif t == "m":
                    hid_for(app, sel).mouse(
                        int(e.get("b", 0)) & 0xFF, clamp8(e.get("dx", 0)),
                        clamp8(e.get("dy", 0)), clamp8(e.get("w", 0)))
                elif t == "reset":
                    release(app, sel)
                elif t == "type":
                    # Paste as keystrokes into the selected transmitter, one
                    # typist per transmitter so two pages cannot interleave.
                    stop_typing()
                    busy = app["typing"].get(sel)
                    if busy is not None and not busy.done():
                        await ws.send_json({"t": "typed", "state": "busy", "done": 0, "total": 0})
                        continue
                    text = str(e.get("text", ""))
                    print(f"typing {min(len(text), TYPE_MAX)} characters to tx {sel} for {who}", flush=True)
                    # The page may ask for a slower pace (5-200 ms per report)
                    # for computers that drop keys at the default.
                    gap = e.get("gap_ms")
                    gap = TYPE_GAP if gap is None else max(5, min(200, int(gap))) / 1000
                    typing = asyncio.create_task(type_text(ws, hid_for(app, sel), text, gap))
                    app["typing"][sel] = typing
                elif t == "type-stop":
                    stop_typing()
            except (ValueError, KeyError, TypeError, OverflowError):
                continue
    finally:
        stop_typing()
        m.visible.discard(ws)
        m.want.pop(ws, None)
        m.last_use = time.monotonic()
        release(app, sel)             # never leave a key or button held
        print(f"input client gone: {who}", flush=True)
    return ws


async def on_start(app):
    app["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    app["ocr_slots"] = asyncio.Semaphore(2)     # each run is one Tesseract process
    m = app["m"]
    m.watch(asyncio.get_running_loop())
    app["sup"] = asyncio.create_task(m.supervise())
    app["probe"] = asyncio.create_task(m.probe())
    app["info"] = asyncio.create_task(m.poll_info())
    app["air"] = asyncio.create_task(m.watch_air())
    if IGMP_REFRESH > 0:
        app["igmp"] = asyncio.create_task(m.igmp_refresh())


async def on_stop(app):
    app["sup"].cancel()
    app["probe"].cancel()
    app["info"].cancel()
    app["air"].cancel()
    if "igmp" in app:
        app["igmp"].cancel()
    await app["http"].close()
    for n in list(app["m"].procs):
        app["m"].stop(n)
    for r in app["m"].relays.values():
        if r.returncode is None:
            os.killpg(r.pid, signal.SIGTERM)


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--iface", default="192.168.167.2",
                    help="local IPv4 on the NIC that carries the extender LAN "
                         "(used for the multicast joins)")
    ap.add_argument("--bind", default="192.168.167.2",
                    help="source IP for keyboard/mouse packets (an address in "
                         "192.168.167.0/24 on that NIC)")
    ap.add_argument("--listen", default="127.0.0.1:8820", help="host:port for the UI")
    ap.add_argument("--mtx", default="rtsp://127.0.0.1:8554",
                    help="mediamtx RTSP base the feeds publish to")
    ap.add_argument("--webrtc-url", default=os.environ.get("WEBRTC_URL", "http://127.0.0.1:8889"),
                    help="mediamtx WebRTC HTTP base the gateway relays WHEP to "
                         "(keep it on 127.0.0.1; browsers go through the gateway)")
    ap.add_argument("--base-path", default=os.environ.get("BASE_PATH", ""),
                    help="path prefix when a reverse proxy forwards /prefix/... "
                         "unchanged (ex. /kvm); leave empty if the proxy strips it")
    ap.add_argument("--state-dir", default=os.environ.get("STATE_DIR", ""),
                    help="persistent directory for the unit-facts cache "
                         "(ex. /var/lib/tesmart); empty = keep it in memory only")
    ap.add_argument("--status-dir", default=os.environ.get("STATUS_DIR", "/tmp"),
                    help="where the feeds write their health JSON")
    ap.add_argument("--idle", type=float, default=float(os.environ.get("IDLE_SECS", 120)),
                    help="seconds without any viewer before feeds stop and the "
                         "multicast groups are left (0 = always on)")
    ap.add_argument("--host-name", default=os.environ.get("HOST_NAME", "service host"),
                    help="label for the machine running this gateway, shown in the UI")
    ap.add_argument("--stale", type=float, default=15,
                    help="seconds without packets before a feed counts as gone")
    a = ap.parse_args()

    base = "/" + a.base_path.strip("/") if a.base_path.strip("/") else ""
    print(f"multiviewer v{VERSION}", flush=True)
    access = Access(state_dir=a.state_dir)
    for line in access.startup_notice():
        print(line, flush=True)
    app = web.Application(middlewares=[access.middleware(base)],
                          client_max_size=OCR_MAX_BYTES)   # largest body: an OCR image
    app["access"], app["args"], app["hids"], app["woke"] = access, a, {}, {}
    app["typing"] = {}            # transmitter -> its typing task (one typist each)
    app["args"].webrtc_url = a.webrtc_url.rstrip("/")
    app["m"] = Matrix(a.iface, a.mtx, a.stale, a.status_dir, a.idle, a.state_dir)
    routes = [web.get(base + "/", index), web.get(base + "/healthz", healthz),
              web.get(base + "/feeds", feeds), web.get(base + "/host", host_stats_route),
              web.get(base + "/input", ws_input), web.post(base + "/ocr", ocr),
              web.route("*", base + r"/whep/{n:\d+}", whep),
              web.route("*", base + r"/whep/{n:\d+}/{rest:[A-Za-z0-9_-]+}", whep),
              web.static(base + "/static", HERE, show_index=False)]
    if base:
        # /kvm -> /kvm/ : the page uses relative URLs, which need the slash
        async def slash(request):
            raise web.HTTPFound(access.forwarded(request)["prefix"] + base + "/")
        routes.append(web.get(base, slash))
    app.add_routes(routes)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    host, _, port = a.listen.rpartition(":")
    web.run_app(app, host=host or "0.0.0.0", port=int(port), print=None)


if __name__ == "__main__":
    main()
