#!/usr/bin/env bash
# Fetch model weights from ModelScope instead of Hugging Face.
#
# Hugging Face's file CDN is unreachable from some networks even when its API
# responds: small files (config.json) download fine, large ones read-timeout
# against us.aws.cdn.hf.co, and the Xet backend fails separately. The result is
# a stack that starts, reports healthy, and only fails at first inference --
# the aborted download leaves enough metadata behind to look like a cache hit.
#
# ModelScope hosts the same weights and, on the affected network, served them
# at ~6.5 MB/s where HF served 0. Everything here is env-driven so the source
# stays a deployment choice rather than something baked into an image.
#
#   MODELS_DIR       where to write            (default /opt/aranmed/models)
#   MS_WHISPER_REPO  ModelScope whisper repo   (default AI-ModelScope/whisper-large-v3)
#   MS_QWEN_REPO     ModelScope core-LLM repo  (default Qwen/Qwen3.5-4B)
#   FETCH_WHISPER / FETCH_QWEN   set to 0 to skip either
set -uo pipefail

MODELS_DIR="${MODELS_DIR:-/opt/aranmed/models}"
MS_API="${MS_API:-https://modelscope.cn/api/v1/models}"
MS_WHISPER_REPO="${MS_WHISPER_REPO:-AI-ModelScope/whisper-large-v3}"
MS_QWEN_REPO="${MS_QWEN_REPO:-Qwen/Qwen3.5-4B}"
FETCH_WHISPER="${FETCH_WHISPER:-1}"
FETCH_QWEN="${FETCH_QWEN:-1}"

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }

# List "<size> <name>" for a repo.
list_files() {
    curl -s --connect-timeout 30 "${MS_API}/$1/repo/files?Revision=master" \
      | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for f in d.get("Data", {}).get("Files", []):
    n = f.get("Name", "")
    if n and not n.endswith("/") and f.get("Type") != "tree":
        print(f.get("Size", 0), n)
'
}

# Download one file, resuming and retrying. Verifies the final size matches
# what the API advertised -- a truncated weight file is the failure mode this
# whole script exists to avoid, and it is silent unless checked.
fetch_file() {
    local repo="$1" name="$2" want="$3" dest="$4"
    local url="${MS_API}/${repo}/repo?Revision=master&FilePath=${name}"
    mkdir -p "$(dirname "$dest")"
    for attempt in 1 2 3 4 5; do
        # Resume only a file this loop itself started. `curl -C -` against a
        # leftover from an aborted earlier run appends rather than repairs,
        # producing a file that is LARGER than expected and silently corrupt
        # -- observed: whisper model.safetensors at 3.56 GB against 3.09 GB
        # advertised, which passed every size check and failed only on
        # "Error while deserializing header: incomplete metadata".
        if [ "$attempt" = "1" ] && [ ! -f "$dest.part" ]; then
            rm -f "$dest"
        fi
        : > "$dest.part"
        curl -sL --connect-timeout 30 --max-time 7200 -C - -o "$dest" "$url" && :
        local got
        got=$(stat -c%s "$dest" 2>/dev/null || echo 0)
        # Accept got >= want, not equality: ModelScope's advertised Size is
        # not byte-exact for text files (observed merges.txt got=543870
        # want=493869), which would retry a perfectly complete file forever.
        # Truncation is the failure that matters, and that is got < want.
        if [ "$want" = "0" ] || [ "$got" -ge "$want" ] 2>/dev/null; then
            # A size check cannot tell a complete file from a corrupt one, so
            # actually parse the container before declaring success.
            case "$dest" in
                *.safetensors)
                    if ! python3 - "$dest" <<'PYEOF' 2>/dev/null
import sys
from safetensors import safe_open
with safe_open(sys.argv[1], framework="numpy") as f:
    next(iter(f.keys()), None)
PYEOF
                    then
                        log "    corrupt $name; re-fetching from scratch"
                        rm -f "$dest" "$dest.part"
                        sleep 3
                        continue
                    fi
                    ;;
            esac
            rm -f "$dest.part"
            log "    ok   $name ($got bytes)"
            return 0
        fi
        log "    retry $attempt: $name got=$got want=$want"
        sleep 5
    done
    log "    FAIL $name (got=$(stat -c%s "$dest" 2>/dev/null || echo 0) want=$want)"
    return 1
}

fetch_repo() {
    local repo="$1" out="$2"
    log "=== $repo -> $out"
    local listing
    listing=$(list_files "$repo") || { log "    cannot list $repo"; return 1; }
    [ -z "$listing" ] && { log "    empty listing for $repo"; return 1; }

    local rc=0
    while read -r size name; do
        case "$name" in
            *.safetensors|*.json|*.txt|*.model) ;;
            *) continue ;;
        esac
        # These repos ship the same weights several times over: fp32 shards,
        # a flax msgpack, and pytorch .bin alongside the safetensors we want.
        # Taking everything turns a 3 GB fetch into ~20 GB, which on a slow
        # link is the difference between minutes and hours.
        case "$name" in
            *fp32*|*flax*|*.msgpack|pytorch_model*|*.bin|*consolidated*)
                log "    skip $name (duplicate format)"; continue ;;
        esac
        # ModelScope publishes shards as "model.safetensors-00001-of-00002.safetensors";
        # transformers/vLLM expect the name the index actually references, so
        # normalise to "model-00001-of-00002.safetensors".
        local target="$name"
        case "$name" in
            model.safetensors-*-of-*.safetensors)
                target="model-${name#model.safetensors-}"
                target="${target%.safetensors}"
                target="${target}.safetensors"
                ;;
        esac
        fetch_file "$repo" "$name" "$size" "$out/$target" || rc=1
    done <<< "$listing"
    return $rc
}

overall=0
if [ "$FETCH_WHISPER" = "1" ]; then
    fetch_repo "$MS_WHISPER_REPO" "$MODELS_DIR/whisper-large-v3" || overall=1
fi
if [ "$FETCH_QWEN" = "1" ]; then
    fetch_repo "$MS_QWEN_REPO" "$MODELS_DIR/qwen3.5-4b" || overall=1
fi

log "=== result ==="
du -sh "$MODELS_DIR"/* 2>/dev/null
[ "$overall" = "0" ] && log "ALL OK" || log "SOME FILES FAILED"
log "=== DONE ==="
exit $overall
