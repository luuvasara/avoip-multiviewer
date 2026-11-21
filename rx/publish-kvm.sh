#!/bin/bash
# Live software receiver → a local mediamtx. Publishes the extender's HEVC
# video (copied, not re-encoded) and its PCM audio (as Opus) to
# rtsp://127.0.0.1:8554/kvm so LAN viewers can watch it.
#   ID        transmitter ID 1-16 (default 1); ID n uses video port 4399+n
#             and audio port 4299+n on the same group 239.1.1.1 (measured for 1 and 2)
#   IFACE_IP  local IP on the extender subnet (default 192.168.167.2)
#   AUDIO=0   video only, ex. when chasing a sync problem
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IFACE_IP="${IFACE_IP:-192.168.167.2}"
ID="${ID:-1}"; VPORT=$((4399 + ID)); APORT=$((4299 + ID))
AUDIO_IN=() ; AUDIO_OUT=(-an)
if [ "${AUDIO:-1}" = 1 ]; then
  # The audio port carries headerless PCM; the byte rate (190.9 kB/s measured)
  # fits 48 kHz 16-bit stereo. Channel layout is unconfirmed until a real
  # signal is played through the TX.
  AUDIO_IN=(-f s16le -ar 48000 -ac 2
            -i "udp://239.1.1.1:${APORT}?localaddr=${IFACE_IP}&reuse=1&fifo_size=100000&overrun_nonfatal=1")
  AUDIO_OUT=(-map 0:v:0 -map 1:a:0 -af aresample=async=1000 -c:a libopus -b:a 96k)
fi
python3 "$HERE/reassemble.py" --iface "$IFACE_IP" --port "$VPORT" "$@" | \
  ffmpeg -hide_banner -loglevel warning \
    -fflags +genpts -use_wallclock_as_timestamps 1 -f hevc -i - \
    "${AUDIO_IN[@]}" \
    -c:v copy "${AUDIO_OUT[@]}" \
    -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/kvm
