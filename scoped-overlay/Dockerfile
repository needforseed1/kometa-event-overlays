FROM lscr.io/linuxserver/kometa:2.4.6

COPY patch_overlay_scope.py /tmp/patch_overlay_scope.py
RUN python3 /tmp/patch_overlay_scope.py /app/kometa/modules/overlays.py \
    && rm /tmp/patch_overlay_scope.py \
    && mkdir -p /config \
    && chown 1000:100 /config

COPY overlay_worker.py /app/overlay-worker.py
