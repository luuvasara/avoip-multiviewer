#!/bin/sh
# Host-network container start: give the NIC an address on the extender subnet
# (source of keyboard/mouse packets) and a receive ring big enough for a whole
# keyframe burst (an e1000e default ring of 256 dropped ~25 of 162 fragments).
set -e
NIC="${NIC:?set NIC to the network interface that reaches the extenders}"
HID_IP="${HID_IP:-192.168.167.3}"
[ -n "$LIBVA_DRIVER_NAME" ] || unset LIBVA_DRIVER_NAME   # empty = let libva choose
mkdir -p "$STATUS_DIR" "$STATE_DIR"
ip addr replace "$HID_IP/24" dev "$NIC" || echo "entrypoint: could not add $HID_IP on $NIC"
# Resizing the ring resets the link for a few seconds; wait so whatever started
# this container has finished talking to the host first.
if [ "${RX_RING:-4096}" != 0 ]; then
  ( sleep 8; ethtool -G "$NIC" rx "${RX_RING:-4096}" 2>/dev/null \
      && echo "entrypoint: $NIC rx ring set to ${RX_RING:-4096}" \
      || echo "entrypoint: $NIC rx ring unchanged (already set or unsupported)" ) &
fi
exec python3 /app/webclient/gateway.py --listen "$LISTEN" \
  --iface "$HID_IP" --bind "$HID_IP" --mtx rtsp://127.0.0.1:8554 \
  --status-dir "$STATUS_DIR"
