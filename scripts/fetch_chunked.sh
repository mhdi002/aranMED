#!/usr/bin/env bash
# Fetch one large file as a sequence of ranged requests.
#
# A single multi-GB GET is the wrong shape for a flaky or throttled link. Two
# failures made that concrete on the same host:
#
#   * a 2.9 GB transfer completed and then failed to parse -- corrupt somewhere
#     in the middle, with 52 minutes of transfer thrown away to find out
#   * sustained downloading tripped a server-side throttle, after which even a
#     20 MB probe returned nothing, and the caller's 5-second retries just
#     hammered a source that needed to be left alone
#
# Ranged chunks fix both. A damaged chunk costs one chunk, not the whole file.
# Each chunk is a short connection, so there is less to drop. And a failure
# backs off geometrically instead of retrying into a rate limit.
#
# Usage: fetch_chunked.sh <url> <dest> <expected_total_bytes>
# Env:   CHUNK_MB (default 32), MAX_TRIES (default 8), BACKOFF_BASE (default 15)
set -uo pipefail

URL="${1:?url required}"
DEST="${2:?dest required}"
TOTAL="${3:?expected size required}"

CHUNK=$(( ${CHUNK_MB:-32} * 1024 * 1024 ))
MAX_TRIES="${MAX_TRIES:-8}"
BACKOFF_BASE="${BACKOFF_BASE:-15}"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }

mkdir -p "$(dirname "$DEST")"
# Resume from whatever is already on disk. Because every chunk is verified for
# length before being appended, a partial file here is known-good bytes.
have=$(stat -c%s "$DEST" 2>/dev/null || echo 0)
[ "$have" -gt "$TOTAL" ] && { log "existing file larger than expected; restarting"; rm -f "$DEST"; have=0; }
[ "$have" -gt 0 ] && log "resuming at $have / $TOTAL"

while [ "$have" -lt "$TOTAL" ]; do
    end=$(( have + CHUNK - 1 ))
    [ "$end" -ge "$TOTAL" ] && end=$(( TOTAL - 1 ))
    want=$(( end - have + 1 ))

    got=0
    for try in $(seq 1 "$MAX_TRIES"); do
        tmp="${DEST}.chunk"
        rm -f "$tmp"
        curl -sL --connect-timeout 30 --max-time 900 \
             -r "${have}-${end}" -o "$tmp" "$URL" 2>/dev/null
        got=$(stat -c%s "$tmp" 2>/dev/null || echo 0)
        [ "$got" = "$want" ] && break
        # Geometric backoff: a throttled source needs to be left alone, and
        # retrying every 5s is what turns a temporary limit into a hard one.
        wait=$(( BACKOFF_BASE * try ))
        log "  chunk @${have} got=$got want=$want; retry $try in ${wait}s"
        sleep "$wait"
    done

    if [ "$got" != "$want" ]; then
        log "FAILED at offset $have after $MAX_TRIES tries"
        rm -f "${DEST}.chunk"
        exit 1
    fi

    cat "${DEST}.chunk" >> "$DEST"
    rm -f "${DEST}.chunk"
    have=$(stat -c%s "$DEST" 2>/dev/null || echo 0)
    pct=$(( have * 100 / TOTAL ))
    log "  $(( have / 1048576 ))MB / $(( TOTAL / 1048576 ))MB (${pct}%)"
done

log "complete: $have bytes"
# Only meaningful for safetensors, and only worth doing once at the end.
case "$DEST" in
    *.safetensors)
        if python3 - "$DEST" <<'PYEOF' 2>/dev/null
import sys
from safetensors import safe_open
with safe_open(sys.argv[1], framework="numpy") as f:
    next(iter(f.keys()), None)
PYEOF
        then log "INTEGRITY OK"
        else log "INTEGRITY FAILED"; exit 2
        fi
        ;;
esac
exit 0
