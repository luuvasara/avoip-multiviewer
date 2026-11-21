# TESmart HKE120 firmware internals (read-only investigation, 2026-11-20)

All of this came from the units' **open root telnet (no password)** — a vendor
debug leftover on every unit. Probed read-only with `tools/telnet_probe.py`
(Python 3.13+ dropped `telnetlib`, so it's a raw-socket client). Raw captures in
`captures/firmware/` (`pass1-*`, `pass2-*`, `pass3-*`, `tx5-14`, `tx2-11`, `rx-90`).
The `captures/` files named in this document (telnet logs, packet captures, screenshots,
vendor binaries) are kept privately and are not part of the published repository.

> **Security:** every unit answers `root` with no password over telnet. Anyone on
> the `192.168.167.0/24` extender segment has root on all of them. Keep that
> segment isolated (it effectively is — the units only talk among themselves).

## Units seen on the wire

MAC is `00:70:33:00:00:<ID>`, where `<ID>` is the device's communication ID.

| Role | LAN IP        | MAC last octet | Kernel build            | SoC            |
|------|---------------|----------------|-------------------------|----------------|
| TX   | .10 (ID1)     | 10             | —                       | SigmaStar SSC337 |
| TX2  | .11 (ID2)     | 11             | #5  PREEMPT 2021-03-11  | SigmaStar SSC337 (SStar, ARMv7 Cortex-A7, single core) |
| TX5  | .14 (ID5)     | 14             | #5  PREEMPT 2021-03-11  | same, **byte-identical firmware to TX2** |
| RX   | .90 (every RX) | 02 (every RX, any channel; see 2026-11-21 below) | #11 **SMP** 2022-04-13  | SigmaStar, **dual-core**, probably SSD203 |

- TX5 (case label `HKE12MMA20-TX`; the unit's host name is `(none)`) and TX2 share the **same kernel and the same app config** —
  important for the truncation diagnosis below.
- RX runs a newer, SMP kernel. (Corrected 2026-11-21: this is NOT the vendor's old/new
  difference; an old and a new receiver run the same kernel, see the side-by-side section.)
- Common to all: Linux 4.9.84, BusyBox 1.20.2, ~44 MB RAM, flash MTD layout
  `BOOT / KERNEL / rootfs / miservice / customer`. The vendor app lives in the
  `customer` partition (JFFS2, writable). No version string file anywhere; the
  firmware is a **repurposed SigmaStar reference app** (dead `hostapd.conf`,
  `dnsmasq.conf`, `wlan.json`, `wpa_supplicant.conf`, `p2p0` WiFi bits all
  commented out in the run scripts).

## How a stream actually works

**TX** (`/customer/hdmiap/`, launched by `demo.sh` → `run.eth0.dhcp.337.ip.sh`):

1. `it6616_MIPI.ko` (ITE **IT6616** HDMI receiver, 4 lanes) captures HDMI in.
2. SigmaStar MI pipeline from `2M_AUDIO.ini`:
   `VIF → VPE (1920x1080) → VENC (H.265)` + `AI` (I2S audio 48 kHz).
3. `hdmi_convertor 2M_AUDIO.ini` exposes it as RTSP **`main_stream`** (video+audio).
4. `./trans` bridges that onto the wire; we observe it as **multicast
   `239.1.1.1:(4399+ID)`** and it also answers RTSP on a `172.19.24.x`
   control-plane address (see below).

**RX** (`/customer/hdmiplayer/`, launched by `run.eth0.dhcp.203.ip.sh`):

- `./rtspclient rtsp://172.19.24.11/main_stream RTSP_CLIENT_HDMI.ini 1920 1080`
- Pipeline from `RTSP_CLIENT_HDMI.ini`:
  `RTSP_CLIENT → VDEC (H.265, REF_FRAME=2) → DISP (HDMI out, drop_frame=1)` + `AO`.
- Loads `usbhid` (the receiver's USB keyboard/mouse), `mi_cipher`/`mdrv_crypto`.
- Has a second NIC `eth1` (`00:30:1b:ba:02:db`, no IP) and a stale 43 MB `core`
  dump (rtspclient has crashed at some point).

**Addressing.** Each unit's real LAN address is `192.168.167.x` (set by `trans`,
overriding the static `192.168.2.120` / `172.19.25.x` lines left in the run
scripts). The RX pulls RTSP from `172.19.24.11` — an internal control-plane the
`trans` daemons maintain, mapping a TX communication-ID to an address. The
per-unit RTSP URL baked in the ini (`rtsp://192.168.1.101/main_stream`) is a
placeholder; the command-line URL wins.

## TX5 keyframe truncation — root cause

The encoder block in `/customer/hdmiap/2M_AUDIO.ini`:

```
[VENC_CH0_DEV0]
STREAM_W/H   = 1920 / 1080
EN_TYPE      = 13      # H.265
EN_FPS       = 60
BIT_RATE     = 4       # low rate (the "2M" filename is stale)
MULTI_SLICE  = 0       # whole frame encoded as ONE slice / ONE NAL
SLICE_ROW_CNT= 3       # ignored while MULTI_SLICE=0 (comment: "MULTI Slice h265:9")
```

**Mechanism:** with `MULTI_SLICE=0` each frame is a single NAL. On a dense/complex
1080p screen the I-frame (keyframe) gets large; as one giant NAL it exceeds
`trans`'s per-frame transport buffer (~114 KB, measured earlier) and is cut off
→ the bottom of the frame garbles until the next keyframe.

**Why it's not a TX5 defect:** TX2 and TX5 run byte-identical firmware and the
identical `2M_AUDIO.ini`. The truncation is **content-dependent** — whichever
unit is showing the densest image hits the ceiling. Consistent with the garbling
also appearing on the physical monitor.

**Candidate fix (NOT applied — mutation on a production appliance, needs explicit
go-ahead, test on one unit first):** set `MULTI_SLICE = 1` and `SLICE_ROW_CNT = 9`
so each frame is emitted as several smaller slice-NALs that fit the buffer — the
standard remedy for this overflow, and the ini's own comment points at `h265:9`.
The change persists in the writable `customer` JFFS2 partition and needs
`hdmi_convertor` restarted. Both the hardware RX and our `reassemble.py` already
handle multi-NAL frames. Risk: a bad edit bricks the stream until reverted; keep
a copy of the original ini.

## Firmware "update"

No vendor updater exists (confirmed separately: TESmart publishes none for the
HKE120, only for its USB-KVM switch line). There is no version metadata on the
device. "Updating" would mean writing MTD partitions by hand — high risk, out of
scope. The TX5 issue is far better addressed by the `2M_AUDIO.ini` slice tweak
above than by any flash.

## Online corroboration of the slice fix (2026-11-20)

SigmaStar's own MI SDK docs validate the `MULTI_SLICE`/`SLICE_ROW_CNT` remedy
(sources: `wx.comake.online/.../mi_venc.html`, `git.sigmastar.com.cn/.../venc.html`):

- **`MI_VENC_SetH265SliceSplit`** — `bSplitEnable` (= ini `MULTI_SLICE`) +
  `u32SliceRowCount` (= ini `SLICE_ROW_CNT`): "When bSplitEnable is set to true,
  the u32SliceRowCount macroblock row is used as a slice to encode separately.
  After each slice is encoded, the user can get the stream data of the slice."
  → each slice is a separate, smaller NAL, so a dense I-frame stops overrunning
  one buffer. Exactly our fix.
- **Caveat:** "the smaller the u32SliceRowCount is … the easier it is to cause
  bit stream output memory fragmentation." So 9 rows/slice (~8 slices/1080p),
  not 1.
- Related buffer knobs corroborate the root cause: `u32OneStreamBuffer`
  (0=multi-package / 1=single-package), `u32H265eMiniBufMode`, and CBR caps
  `u32MaxISize`/`u32MaxPSize`; the stream buffer is "allocated according to
  resolution" — i.e. a fixed per-frame ceiling a complex I-frame can exceed.

Platform is well-documented (linux-chenxing SSC337DE datasheet, OpenIPC/thingino,
full SigmaStar MI docs). This specific TESmart HDMI-**TX** app (`hdmi_convertor` /
`2M_AUDIO.ini`) is not documented anywhere public; the famous HDMI-over-IP RE
(danman, apalrd, Hackaday) targets the older **Lenkeng LKV373**, a different chip.
apalrd's `00:70:33` notes (captures/apalrd/) are the closest prior art.

## Second online pass (2026-11-20 09:10 EET) — what it changes

Sources read in full or by section, all on 2026-11-20:
`https://support.tesmart.com/hc/en-us/articles/41230548979481-HKE120-EH23-FAQ` (page dated 2024-12-16),
`https://www.tesmart.com/products/hke120-eh23`,
`https://forum.mikrotik.com/t/hdmi-over-ip-switch-configs/184019` (2025-06, 17/17 posts),
`https://wx.comake.online/doc/d8clf27cnes2-SSD20X/customer/development/mi/en/mi_venc.html` (§1.3, §2.13, §2.16, §2.64, §3.18),
`https://sysctl-explorer.net/net/ipv4/force_igmp_version/`.

### 1. The 114 KB ceiling is NOT the encoder's buffer (weakens the fix's certainty)

SigmaStar's default encoder output buffer is far bigger than our cut-off:

> "Ispahan | Ring pool: WidthxHeight/2 | Ring pool: WidthxHeight/2" (MI VENC §1.3)

1920×1080/2 = 1 036 800 B, about 9× the observed cut at exactly 114 × 1024 B. So the
truncation happens **after** the encoder — in `hdmi_convertor`'s RTSP layer or in `trans` —
which matches the "no end marker" symptom (an encoder overflow would drop the frame, not
ship a clipped one). Consequence: `MULTI_SLICE=1` only helps if that downstream buffer is
filled **per NAL / per pack**. If it is filled per frame (all packs of a frame concatenated,
which is what `MI_VENC_GetStream` hands over: "U32PackCount … is the number of code streams
in a complete frame"), slicing changes nothing. Unverified either way; this is a
hypothesis until tested on one unit.

Read-only checks that would settle it before any edit: `strings` on `trans` and
`hdmi_convertor` for `116736` / `0x1C800` / buffer-size messages, and
`cat /proc/mi_modules/mi_venc/mi_venc0` for the real stream-buffer size and any drop counters.

### 2. Alternatives the SDK offers (need app support — not confirmed as ini keys)

- `MI_VENC_SetSuperFrameCfg`: "When super frame mode is E_MI_VENC_SUPERFRM_REENCODE,MI will
  increase the QP of the frame by 4 and reencode the super frame." With
  `u32SuperIFrmBitsThr` under 114 KB × 8 an oversized keyframe would be re-encoded smaller
  instead of clipped. This attacks the actual symptom (frame too big) regardless of where
  the 114 KB limit sits. Default is `E_MI_VENC_SUPERFRM_NONE`.
- CBR `u32MaxISize` (already noted above) — same idea, cap the I-frame.
- Whether `hdmi_convertor` reads any such key from the ini is unknown; check its `strings`.

### 3. IGMP "version split" — vendor statement, and a contradiction with our units

> "the new version of RX cannot receive the video signal from the old version of TX, and
> the new version of TX cannot send the video signal to the old version of RX" (TESmart FAQ)

> "if the factory date is after June 2024, it is a new version" (same FAQ)

- The split is by **factory date (June 2024)**, checked on the tamper sticker. Our kernels
  are built 2021-03 (TX) and 2022-04 (RX) — both long before the cut-off, and they do
  interoperate. So the earlier line in this file calling the RX's SMP kernel "the new-version
  vs old-version difference" is **not supported**; kernel build date ≠ factory date, and the
  sticker is the only vendor-defined test. Corrected here; the table above is left as measured.
- The old-version manual, quoted in the MikroTik thread: "For normal use, it is necessary to
  disable the IGMP when connecting to an Ethernet switch". Our switches already run with
  igmp-snooping off (MLD incident), which is the vendor-recommended state for old units.
- TESmart's remedy is a free return-to-factory upgrade — so a firmware image for the new
  IGMP behaviour exists, but is not published (searched: `TESmart HKE120 firmware`, support
  Download category; only the PKS0402A20 switch tool turns up on GitHub).
- Kernel side: `force_igmp_version` 0 = "No enforcement of a IGMP version, IGMPv1/v2
  fallback allowed". A v2/v3 difference between units is readable with
  `cat /proc/sys/net/ipv4/conf/all/force_igmp_version` and `/proc/net/igmp` — read-only.

### 4. Not found

No public teardown, firmware dump or protocol write-up for this SigmaStar-based TESmart
unit. Queries: `HDMI extender over IP 239.1.1.1 telnet root no password SigmaStar "00:70:33"`
(0 results), `SigmaStar SSC337DE HDMI encoder hdmi_convertor IT6616 RTSP main_stream`,
`HDMI over IP extender SigmaStar SSC337 teardown H.265 "IT6616"`. Patent CN115396725A
(Shenzhen Xinlongpeng, 2022) describes the same IT6616 → MIPI CSI → SoC encoder chain but on
Rockchip RV1126 — same design pattern, not evidence of the OEM.

## Measured comparison of four units (2026-11-20 09:40 EET) — supersedes parts above

All read-only: telnet `cat`/`md5sum`/`top`/`netstat` (`tools/probe-cmds4..9.txt`, raw output in
`captures/firmware/pass4..9-<host>.txt`), passive multicast statistics (`tools/framestats.py`),
and local disassembly of two binaries copied off TX2 as hex dumps (md5 verified against the
device). Nothing was written to any unit.

### Units

| | TX2 (.11) | TX4 (.13) | TX5 (.14) | RX (.90) |
|---|---|---|---|---|
| Sticker date | 12/2025 | **11/2024** | 12/2025 | — |
| Kernel | 4.9.84 #5, 2021-03-11 | same | same | 4.9.84 #11 SMP, 2022-04-13 |
| `hdmi_convertor` md5 | `4465df48…` | same | same | n/a (`rtspclient`) |
| `2M_AUDIO.ini` md5 | `0264f6f6…` | same | same | n/a |
| `/customer/trans` | present, `f94552d5…` | **absent** | present, same md5 | present, same md5 |
| `demo.sh` size | 2023 B | 2001 B (no `./trans &`) | 2023 B | 2892 B |
| Listening | tcp 23, **tcp 9981**, udp 9980 | tcp 23 only | tcp 23, **tcp 9981**, udp 9980 | (not listed) |
| Uptime at probe | 289 days | 2 min | 12.5 h | 30 min |
| IGMP mode (`/proc/net/igmp`) | V3, joins only 224.0.0.1 | same | same | V3, joins 239.1.1.1 |
| `force_igmp_version` | 0 | 0 | 0 | 0 |

So the only software difference between the 11/2024 and the 12/2025 transmitter is the added
`trans` daemon. By TESmart's June-2024 rule all four are "new version". There is **no IGMP
version difference** between any of these units — the "IGMP split" does not exist in this set.

### `trans` is not the video transport (corrects "How a stream actually works")

Disassembly of the 17 368-byte, unstripped binary (`captures/firmware/bin/trans`):

- `main` starts two threads (`tcp_server`, `udp_server`) and then spins in `b .` (`e7fe` at
  0x115a6) — a busy loop. Measured cost: 18–45 % of the single CPU core on TX2/TX5, where
  `hdmi_convertor` gets 23–72 %; on TX4 (no `trans`) `hdmi_convertor` gets 72–91 %. Every unit
  shows 0 % idle either way, so `hdmi_convertor` busy-polls too.
- `tcp_server` listens on **TCP 9981**; `client_thread` reads a header (`aa bb`), a file name,
  runs `pkill -f <name>` through `system()`, then `fopen(name, "wb+"/"ab+")` and writes the
  received data. Strings: `FileName : %s`, `allLen %d`, `rec over %d %d`, `This is Test Program`.
  That is an **unauthenticated remote file-write-and-kill service running as root** — the
  factory update channel. It answers the "no updater exists" question (the mechanism is on the
  device; no vendor image or client is public) and it is a second root hole beside telnet,
  present only on the 12/2025 units and the RX. Not exercised: sending to it would be a write.
- The multicast video is sent by `hdmi_convertor` itself (its `Rtsp` module holds the strings
  `239.1.1.1`, `ifconfig eth0 192.168.167.%d`, `ifconfig eth0 hw ether 00:70:33:00:00:%d`), and
  it owns the UDP sockets 4500+n / 4600+n. The "172.19.24.x control plane maintained by trans"
  statement above is unsupported.

### Encoder numbers (TX2, `/proc/mi_modules/mi_venc/mi_venc0`)

| Field | Value |
|---|---|
| Output `BufSize` | 1 036 800 B (= W×H/2, as the SDK doc says); ring 0xfe000 |
| Rate control | VBR, GOP 120 (keyframe every 2 s), MaxBitrate 4 194 304, QP 20–48 |
| Frames encoded | 44 493 047, output `DropCnt` 0, `ReEncCnt` 0 |
| `IsrBufFullCnt` | 433 in 289 days |
| UDP `SndbufErrors` | TX2 2, TX4 0, TX5 0; `wmem_default` 524 288 on all |

### Stream measurements

| Source | Capture | Keyframes | Keyframe size | Frames ending without end marker |
|---|---|---|---|---|
| TX2 live, 90 s | 2026-11-20 09:20 | 43 | 114 015 B, all identical | 0 (2 frames with fragment gaps) |
| TX2 live, 60 s | 09:25 | 28 | 114 015 B | 1 × 112 640 B (tail lost, see below) |
| TX4 live, 60 s | 09:25 | 30 | 58 595–59 162 B | 0, no gaps |
| TX5 live | 09:20, 09:25 | — | no packets: VIF/VENC show 0 enabled channels = no HDMI input | — |
| TX2 `both.pcap`/`tx11.pcap` | 2026-11-19 22:36/22:50 | 7 | **121 271 B, complete** | 0 |
| TX5 `both.pcap` | 22:36 | 3 | 76 794–76 809 B | 0 |
| TX5 `tx5.pcap` (5 s) | 20:50 | 0 | — | 2 × **116 736 B** (114 × 1024) |

What this does and does not show:

1. **There is no fixed 114 KB per-frame ceiling.** TX2 sent complete 121 271-byte keyframes
   (119 fragments) with the same binary and ini that produced TX5's 116 736-byte cut. The
   "buffer overrun at 114 KB" root cause above is therefore wrong as stated, and the
   `MULTI_SLICE=1` fix has no measured basis. Do not apply it on this evidence.
2. The TX5 cut is supported by exactly **two frames in one 5-second capture**, taken about
   3 minutes after TX5 booted. Two identical sizes point at a deterministic limit, but the
   sample is too small to say which one.
3. A frame that loses its last fragments on the way looks identical to a sender-side cut
   (TX2's 112 640 B frame today is 110 of 112 fragments — a tail loss, since that keyframe is
   known to be 114 015 B). The `unterminated` counter cannot tell the two apart by itself;
   only a repeated identical size above the largest complete frame can.
4. `hdmi_convertor` does contain a size check next to `Rtsp::GetVideoDataDirect`:
   `Data too large! limitation %d, cur %d`. The limit value was not located (Thumb-2 code with
   PC-relative addressing; three xref searches failed). It prints to the serial console.

### What would settle the TX5 question

TX5 needs an HDMI source again. Then run `tools/framestats.py 2 4 5 --secs 600` while TX5 shows
the dense screen: if cut frames repeat at one size while TX2 carries larger complete keyframes,
the limit is real and unit- or state-specific; if the sizes vary, it is loss. A UART on the
TX5 console would show the `Data too large! limitation` line with the actual number.

## 10-minute measurement + switch check (2026-11-20 09:34 EET) — settles the cut question

`tools/framestats.py 2 4 5 --secs 600` (`captures/framestats-20261120-0935-10min.json`),
after waking all sources with a Shift tap (`rx/hid.py --id N key shift`):

| TX | fps | keyframes | keyframe size (min/p50/max) | unterminated | distinct cut sizes | frames w/ gaps |
|----|-----|-----------|------------------------------|--------------|--------------------|----------------|
| 2 (.11) | 59.9 | 121 | 114015 / 114015 / 114015 | 19 | 14 sizes, 90112–113664, **all < 114015** | 160 |
| 4 (.13, 11/2024) | 50.1 | 206 | 56000 / 56000 / 56019 | 11 | 7 sizes, 43008–55296, **all < 56000** | 34 |
| 5 (.14) | 59.96 | 192 | 76794 / 76794 / 76809 | 15 | 9 sizes, 64512–76800, **all < 76800** | 93 |

**Conclusion — the truncation is network packet loss, not a sender frame cap.**
In 10 minutes across all three units, every "unterminated" frame is a *different* size
and every one is *below* that unit's stable complete-keyframe size, and each is
accompanied by many mid-frame fragment gaps (160/34/93). A sender-side buffer cap would
show one repeated size at or above the largest complete frame; nothing like that appears.
TX5's "116736 twice" from yesterday's 5-second capture did not recur in 600 s and was
almost certainly the same loss caught in a tiny sample right after boot. **So the
`MULTI_SLICE=1` fix is doubly unjustified: there is no cap to raise.** The fix to pursue
is the loss itself — the capture host's network card showed `dropped:12194` RX earlier, so the
receive path (NIC ring, host, or a switch port) is dropping fragments under the ~0.5 Mbit
but 60-fragment-burst keyframes. Each unit's complete keyframe size is content-stable, so
the per-tile reassembler can now flag "cut below the known keyframe size" as loss with
confidence.

Per-unit keyframe sizes also differ by content/rate: TX4 (the 11/2024 unit) runs its
source at 50 fps / ~56 KB keyframes vs 60 fps on the others — a source-mode difference,
not a firmware one.

### Switches are NOT filtering multicast (checked 2026-11-20, read-only)

All three MikroTik switches on the path (an RB5009 and two CRS-series switches):

- `igmp-snooping=no` on **every** bridge — so none is pruning 239.1.1.1 by IGMP membership.
- Every bridge mdb table is empty (`:len [/interface bridge mdb find]` = 0), i.e. no
  multicast-group forwarding state that could drop a group.
- No bridge filter rules; the only switch-chip rule anywhere is a dynamic RoMON
  redirect on the RB5009 (`mac-protocol=romon`), which does not touch multicast data.
- All four unit MACs (`00:70:33:00:00:02/11/13/14`) are learned on the expected ports on
  all three switches, so L2 reaches them.

So the switches flood 239.1.1.1 to every port (the vendor-recommended state with snooping
off) and are not the cause of either the missing-TX5 symptom or the fragment loss.

### Why TX5 was missing from the multiviewer (answered)

Not the switches and not the gateway: TX5's **computer was asleep / had no HDMI input**,
so TX5 encoded nothing and sent zero packets (its VENC showed 0 enabled channels). The
gateway only listed transmitters it had seen on the video multicast, so a silent unit was
invisible. Fixed on 2026-11-20: the gateway now probes each unit's presence on the
network independently of video (a UDP poke to closed port 9, ICMP-unreachable = up) and
lists a present-but-silent unit as "No signal" with a **Wake source** button that sends
one Shift tap. Confirmed working: a Shift tap to TX5 brought its
stream back within ~8 s.

## Input needs no linked receiver (measured 2026-11-20 15:35 EET)

Earlier note (2026-11-19): "keys to TX 2 did nothing once the RX moved to ID 5" — one trial.
Re-test with the RX tuned to ID 6 (no such transmitter; read from its `config_file` before and
after): `rx/hid.py --id 2 move 80 60` from a separate host moved TX2's mouse pointer, and `move -80 -60`
moved it back (`captures/unlinked-input/compare.png`: before / moved / moved back). So a
transmitter accepts input with no receiver tuned to it. Consistent with the wire: in
`captures/rx-mirror.pcap` the receiver sends the transmitter only ARP and `aa01`/`aa02` datagrams
to 4500+n (18-byte payload, ours are 10 and 6) — there is no link or keepalive message to copy.
Not shown for TX4/TX5: both displayed text consoles without a pointer
(`captures/unlinked-input/tx4-tx5.png`), and a Shift tap leaves no visible trace.
Why the 2026-11-19 trial failed is unknown.

## Related open-source projects (searched 2026-11-20 23:30 EET, unfinished)

Queries: GitHub repositories `tesmart`, `tesmart kvm`, `lkv373`, `hdmi extender over ip`;
GitHub code `hdmi_convertor`; web `TESmart HKE120 KVM over IP reverse engineering protocol`,
`"00:70:33" HDMI extender IP multicast reverse engineer` (no results).

- **Nothing for the HKE120 extender itself.** The 63 GitHub repositories matching `tesmart`
  are for TESmart KVM/HDMI *switches* (LAN/RS232 port-select protocol), a different product:
  e.g. https://github.com/pschmitt/tesmart.sh , https://github.com/krohrbaugh/tesmart-homeassistant ,
  https://github.com/ferrohd/tesmart . Only the first 30 of 63 were listed.
- **`hdmi_convertor` is a SigmaStar SDK demo name.** GitHub code search finds it in
  https://github.com/industio/PurPle-Pi-R1 (GPL-2.0, "base on SSD20X V30 SDK") at
  `sdk/verify/mi_demo/amigos/hdmi_convertor/` (`main.cpp`, `configs/`, `hdmi_convertor.mk`),
  with the shared code in `amigos/internal/base`. NOT YET READ: whether that source matches our
  units' binary (same strings, the 4500+n input sockets, the ini files) is unverified.
- **Same device class, other chip:** the Lenkeng LKV373 family has ~20 projects (receivers,
  OBS plugin, firmware work), e.g. https://github.com/jhol/otl-lkv373a-tools ,
  https://github.com/bp2008/HdmiExtender , https://github.com/kittennbfive/free-373 .
- **Closest write-up:** https://www.apalrd.net/posts/2022/hdmi_ip/ (uses the HKE12MMA20, the
  HKE120's earlier name); not re-read today for code links.

### SigmaStar demo source downloaded and compared (2026-11-20 23:55 EET)

Sparse clone of https://github.com/industio/PurPle-Pi-R1 at `a0fddc2`, path
`sdk/verify/mi_demo/amigos` (1.8 MB, 4688 lines of C++ in `internal/base/src`), kept locally in
`captures/sigmastar-sdk/` (git-ignored; GPL-2.0, re-fetch with
`git clone --depth 1 --filter=blob:none --sparse` + `git sparse-checkout set sdk/verify/mi_demo/amigos`).

- **Same framework, not the same program.** The demo is an HDMI-in (TC358743 chip) → encoder →
  RTSP server app, config file `2M_AUDIO.ini` with the same section names our units use
  (`[ROOT] [RTSP] [VIF_CH0_DEV0] [VPE_CH0_DEV0] [VENC_CH0_DEV0] [AI_CH0_DEV0] …`). 21 of the
  source's 59 string literals of 12+ characters appear in our `hdmi_convertor` binary
  (`VENC_CH0_DEV0`, `IS_USE_SNR_FMT`, `FILE_WRITE_PATH`, live555 messages).
- **The TESmart parts are not in it.** No file in the public source contains `239.1.1.1`,
  `192.168.167`, `config_file`, `00:70:33`, `hw ether`, `IP_ADD_MEMBERSHIP` or `sendto`, all of
  which are in our binary. So the multicast framing (1024 B + 4 B trailer), the ID→address
  scheme and the keyboard/mouse path were added by the device maker and stay closed.
- Their `2M_AUDIO.ini` differs from ours (md5 c62bd8e8 / 2f195007 vs our 0264f6f6).
- Use: the source documents every ini key and the VIF→VPE→VENC module chain, so the encoder
  settings on the units can be read against real code instead of guessed.

### Idea: a TESmart HDK404-P23 KVM switch behind a transmitter (noted 2026-11-20, not tested)

Source: https://www.tesmart.com/products/hdk404-p23 and
https://manuals.plus/tesmart/hdk404-p23-kvm-switch-manual (read 2026-11-20).
- 4 computers × 4 HDMI monitors. "Network Port | No"; "Switching Methods | Keyboard Hotkey,
  Front Panel Button, IR Remote"; "Assignable Hotkeys | Right-CTRL, Scroll Lock, Left-CTRL".
- So the LAN/RS232 control projects on GitHub (tesmart.sh etc.) do NOT apply to it; the only
  remote route is its keyboard hotkey, typed through the extender's keyboard channel.
- Cabling it would need: one HDK404 HDMI output → TX HDMI in, TX USB → an HDK404 console
  keyboard port ("Use a wired external keyboard and mouse in the dedicated console ports for
  hotkeys"). Whether the switch accepts the TX's emulated keyboard for hotkeys is unverified.
- Clash: its default leader key Right-Ctrl is also the receiver's and this page's channel hotkey;
  it would have to be reassigned to Scroll Lock or Left-Ctrl.
- The exact hotkey sequences are in the PDF manual, not read yet.

## Old and new revisions, and what "IGMP authentication" can be (2026-11-21)

- **TESmart's FAQ** (copy: [vendor/tesmart-hke120-eh23-faq.md](vendor/tesmart-hke120-eh23-faq.md))
  says the new version "supports IGMP authentication", old and new cannot be mixed, and old units
  are upgraded for free. IGMP has no authentication of its own; RFC 3376 §9.4 only mentions
  optional IPsec AH. The FAQ is DeepL-translated, so the likely meaning is "IGMP support"
  (receivers announce their groups, so snooping switches forward the video to them).
- **Owner's units:** TESmart confirmed the owner's earlier units need the upgrade and sent
  new-revision units. Old and new work together on this network.
- **Why they still work together here:** IGMP snooping is off on every switch (2026-11-20), so
  239.1.1.1 floods to all ports. A passive 150 s `tcpdump igmp` on the extender network
  (2026-11-21 17:25) saw no IGMP query (no querier) and no IGMP from 192.168.167.x; only an
  unrelated LAN host's mDNS joins. With no querier, hosts report only when they join, so the
  receiver's boot-time join of 239.1.1.1 (seen in its `/proc/net/igmp`) was not on the wire.
- **Measured software differences between revisions** (section "Measured comparison of four
  units" above): the transmitters' kernel, `hdmi_convertor` (md5 4465df48) and `2M_AUDIO.ini` are
  identical across the 11/2024 and 12/2025 stickers; IGMP setup identical (v3,
  `force_igmp_version` 0). Only `trans` (update service) and its `demo.sh` line differ. So the
  upgrade's change is not visible in the transmitters' video or IGMP code; candidates: the
  receiver (`rtspclient`), IGMP timing, or a later push through `trans`. Not determined.
- **Next measurement that would settle it (read-only):** an old-revision receiver and a
  new-revision receiver compared side by side: `md5sum` of `rtspclient` and its ini,
  `/proc/net/igmp`, and a `tcpdump igmp` while each one boots (its join on the wire).

## Old and new receiver side by side (2026-11-21 17:44 EET, read-only)

Two receivers on one switch, one without and one with the update service (the owner's old and
new units), probed read-only with `tools/probe-rx-compare.txt` (raw output kept privately).

| | Receiver without `trans` | Receiver with `trans` |
|---|---|---|
| Kernel | 4.9.84 #11 SMP PREEMPT 2022-04-13 | same |
| CPU | 2 × ARMv7 rev 5, "SStar Soc" | same |
| `rtspclient` md5 | 7fa2a6c9 | same |
| `RTSP_CLIENT_*.ini`, `preview`, `run*.sh` | same md5s | same |
| `demo.sh` | d3576bf0 | 31db4001: two extra lines, `cd /customer` and `./trans &` |
| `/customer/trans` | absent | f94552d5 (as on the 12/2025 transmitters) |
| IGMP (`/proc/net/igmp`) | V3, joined 239.1.1.1 | same |
| Listening | tcp 23; udp 239.1.1.1:43xx/44xx | adds tcp 9981, udp 9980 |
| Channel / MAC / IP | CH02 / 00:70:33:00:00:02 / .90 | **CH06** / 00:70:33:00:00:02 / .90 |

- **Receivers do not take a channel-based MAC.** Both answer as 192.168.167.90 with MAC
  `00:70:33:00:00:02` on different channels; the earlier "RX MAC last octet = target ID" note
  above was a coincidence of the RX being on channel 2. An ARP request got two identical replies,
  and telnet reached whichever unit last moved the MAC on the switch.
- **Conclusion for old vs new:** on both sides (transmitters 2026-11-20, receivers now) the only
  software difference between the owner's old and new units is the `trans` update service. The
  IGMP behaviour TESmart's FAQ attributes to the new version is already present on the old units.
- The receiver's start script is `run.eth0.dhcp.203.ip.sh` where the transmitters' is
  `run.eth0.dhcp.337.ip.sh` (SSC337), so the receiver SoC is probably a SigmaStar SSD203
  (dual Cortex-A7). Inferred from the name only.

### Labels (owner's reading, 2026-11-21)

Corrected 2026-11-21 17:56 after comparing the units side by side:

| Transmitter | Model label | Inspection sticker | Build |
|---|---|---|---|
| vB (with `trans`) | **HKE12MMA20-TX** | 25/12 (December 2025) | 4465df48 + trans f94552d5 |
| vA (no `trans`) | **HKE12MMA20** (no suffix) | 24/11 (November 2024) | 4465df48 |

Both carry the earlier model name, so the name alone does not separate old from new (an earlier
note here said it did; wrong). TESmart said the owner's older units need the upgrade, yet the
FAQ's rule calls 11/2024 "new". Whether the missing "-TX" suffix marks the old revision is not
decidable from two units. The page shows the label in the hardware-variant tooltip (`LABELS` in
`webclient/gateway.py`).
