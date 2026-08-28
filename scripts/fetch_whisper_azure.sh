#!/bin/bash
# Whisper via OpenAI's own Azure CDN.
#
# The HF and ModelScope CDNs are SNI-filtered on this network (TCP connects,
# TLS handshake dies), so neither hf_hub_download nor curl can reach the
# weights. openaipublic.azureedge.net is unrelated to both and serves at
# ~6.4 MB/s here. It publishes the original OpenAI checkpoint, so the file
# needs converting to the HF layout that transformers and the Triton compat
# server expect.
set -uo pipefail

URL="${WHISPER_PT_URL:-https://openaipublic.azureedge.net/main/whisper/models/e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb/large-v3.pt}"
DEST="${WHISPER_PT:-/root/large-v3.pt}"
MIN_BYTES="${WHISPER_PT_MIN:-3000000000}"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }

log "=== downloading large-v3.pt from Azure ==="
for try in 1 2 3 4 5 6; do
    curl -4 -sL -C - --connect-timeout 30 --max-time 3600 -o "$DEST" "$URL"
    sz=$(stat -c%s "$DEST" 2>/dev/null || echo 0)
    log "attempt $try: $((sz / 1048576)) MB"
    [ "$sz" -ge "$MIN_BYTES" ] && break
    sleep 15
done

sz=$(stat -c%s "$DEST" 2>/dev/null || echo 0)
log "final size: $sz bytes"
if [ "$sz" -lt "$MIN_BYTES" ]; then
    log "=== WHISPER PT FAILED ==="
    exit 1
fi
log "=== WHISPER PT DONE ==="
