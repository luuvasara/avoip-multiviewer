#!/bin/bash
# One discovered transmitter -> browser-ready feed. Reassembles the extender's
# HEVC video (239.1.1.1:4399+ID), encodes H.264 on the
# iGPU (browsers can't take H.265 over WebRTC), and publishes to
# mediamtx path kvmweb<ID> for the web client's WHEP player.
#   feed-web.sh <ID>      IFACE_IP=.. MTX=rtsp://host:8554 STATUS_DIR=.. override defaults
set -euo pipefail
# VIDEO ONLY: mixing the transmitter's audio (a second independent real-time
# input) into the same ffmpeg made it stall intermittently - it stopped reading
# the video pipe and padded with duplicated frames - and the browser's A/V sync
# held video back. Audio (239.1.1.1:4299+ID, 48 kHz s16le stereo) can be added
# later as its own path.
# Output is a steady 30 fps: with pass-through timing, frames read from the pipe
# in bursts got near-identical timestamps and browsers dropped ~all of them
# (TX 5 tile: 415 of 416 dropped). -g 30 = keyframe every 1 s, so a damaged
# frame (e.g. a truncated TX 5 keyframe) clears within a second.
# Low latency: slice-threaded decode (frame threads hold back several frames),
# encoder async_depth 1, constant QP (no rate-control lookahead; static screens
# become tiny frames instead of CBR padding).
HERE="$(cd "$(dirname "$0")" && pwd)"
ID="${1:?usage: feed-web.sh <transmitter-ID 1-16>}"
IFACE_IP="${IFACE_IP:-192.168.167.2}"
MTX="${MTX:-rtsp://127.0.0.1:8554}"
STATUS_DIR="${STATUS_DIR:-/tmp}"
# SKIP_INCOMPLETE=0 shows incomplete frames (partial/garbled) instead of holding
# the last good picture until the next complete keyframe.
SKIP_OPT=""; [ "${SKIP_INCOMPLETE:-1}" = 0 ] && SKIP_OPT="--no-skip"   # reassembler health JSON, read by the gateway
VPORT=$((4399 + ID))
python3 "$HERE/reassemble.py" --iface "$IFACE_IP" --port "$VPORT" \
    --status "$STATUS_DIR/tesmart-feed-$ID.json" $SKIP_OPT | \
  ffmpeg -hide_banner -loglevel warning -nostats \
    -fflags nobuffer+genpts -flags low_delay -analyzeduration 0 -thread_type slice \
    -use_wallclock_as_timestamps 1 -f hevc -i - \
    -vaapi_device /dev/dri/renderD128 -vf 'format=nv12,hwupload' \
    -fps_mode cfr -r 30 \
    -c:v h264_vaapi -profile:v main -level 42 -rc_mode CQP -qp 22 -async_depth 1 -g 30 -bf 0 \
    -map 0:v:0 -an \
    -f rtsp -rtsp_transport tcp "$MTX/kvmweb${ID}"
