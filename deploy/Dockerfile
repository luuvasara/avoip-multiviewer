# Web KVM multiviewer for TESmart HKE120-EH23 AV-over-IP extenders:
# gateway + one feed pipeline per transmitter that is being viewed.
# Debian's ffmpeg has software HEVC decode and h264_vaapi (Intel GPU encode).
# VAAPI drivers: i965 for Intel up to 9th gen (Haswell..Coffee Lake),
# iHD (intel-media-va-driver) for Broadwell and newer; libva picks one itself
# unless LIBVA_DRIVER_NAME says otherwise.
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg i965-va-driver intel-media-va-driver \
      python3 python3-aiohttp python3-jwt python3-cryptography \
      iproute2 ethtool \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY VERSION VERSION
COPY rx/ rx/
COPY tools/ tools/
COPY webclient/ webclient/
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh rx/*.sh rx/*.py webclient/gateway.py
ENV STATUS_DIR=/run/kvm STATE_DIR=/var/lib/kvm LISTEN=0.0.0.0:8820
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python3 -c "import os,urllib.request;b=os.environ.get('BASE_PATH','').rstrip('/');p=os.environ['LISTEN'].rsplit(':',1)[1];urllib.request.urlopen(f'http://127.0.0.1:{p}{b}/healthz',timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
