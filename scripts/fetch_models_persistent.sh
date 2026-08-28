#!/bin/bash
# Persistent model fetch.
#
# The upstream block on model-hosting sites fluctuates on this network:
# ModelScope served 1.2 MB/s, then returned nothing an hour later, while PyPI
# and Azure kept working throughout. So the job is to be running when a window
# opens rather than to fail fast. Long backoff, many attempts per chunk, and a
# resumable file so nothing already fetched is lost between windows.
M=/opt/aranmed/models
API=https://modelscope.cn/api/v1/models
export CHUNK_MB="${CHUNK_MB:-32}"
export MAX_TRIES="${MAX_TRIES:-40}"
export BACKOFF_BASE="${BACKOFF_BASE:-20}"

mkdir -p "$M/whisper-large-v3" "$M/qwen3.5-4b"

pass=0
while true; do
    pass=$((pass + 1))
    echo "=== pass $pass starting $(date +%H:%M:%S) ==="

    W=0; Q1=0; Q2=0

    /root/fc.sh \
      "$API/AI-ModelScope/whisper-large-v3/repo?Revision=master&FilePath=model.safetensors" \
      "$M/whisper-large-v3/model.safetensors" 3087130976 && W=1

    # Small files: cheap, and they succeed even while bulk transfer is blocked.
    for f in config.json configuration.json merges.txt normalizer.json \
             preprocessor_config.json special_tokens_map.json added_tokens.json \
             tokenizer.json tokenizer_config.json vocab.json generation_config.json; do
        [ -s "$M/whisper-large-v3/$f" ] || curl -sL --retry 5 --connect-timeout 30 \
          -o "$M/whisper-large-v3/$f" \
          "$API/AI-ModelScope/whisper-large-v3/repo?Revision=master&FilePath=$f" 2>/dev/null
    done
    for f in config.json configuration.json merges.txt model.safetensors.index.json \
             preprocessor_config.json tokenizer.json tokenizer_config.json vocab.json; do
        [ -s "$M/qwen3.5-4b/$f" ] || curl -sL --retry 5 --connect-timeout 30 \
          -o "$M/qwen3.5-4b/$f" \
          "$API/Qwen/Qwen3.5-4B/repo?Revision=master&FilePath=$f" 2>/dev/null
    done

    /root/fc.sh \
      "$API/Qwen/Qwen3.5-4B/repo?Revision=master&FilePath=model.safetensors-00001-of-00002.safetensors" \
      "$M/qwen3.5-4b/model.safetensors-00001-of-00002.safetensors" 5329398688 && Q1=1

    /root/fc.sh \
      "$API/Qwen/Qwen3.5-4B/repo?Revision=master&FilePath=model.safetensors-00002-of-00002.safetensors" \
      "$M/qwen3.5-4b/model.safetensors-00002-of-00002.safetensors" 3990429408 && Q2=1

    if [ "$W$Q1$Q2" = "111" ]; then
        echo "=== ALL MODELS DONE $(date +%H:%M:%S) ==="
        break
    fi
    echo "=== pass $pass incomplete (whisper=$W qwen1=$Q1 qwen2=$Q2); sleeping 5m ==="
    sleep 300
done
