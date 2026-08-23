"""Probe the RESIDENT Whisper via triton's HTTP endpoint, one window at a time.

Deliberately does not import the model: a second copy OOMs an 11GB card that
already holds vLLM plus Whisper. This drives the loaded one over the wire, so
it reproduces exactly what compat_http_server's window loop does.
"""
import json
import os
import subprocess
import sys
import urllib.request

import numpy as np

SRC = os.environ.get("PROBE_SRC", "/tmp/s538.m4a")
URL = os.environ.get("PROBE_URL", "http://triton:8002/v2/models/whisper/infer")
SR = 16000

raw = subprocess.run(
    ["ffmpeg", "-v", "error", "-i", SRC, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
    capture_output=True,
).stdout
wav = np.frombuffer(raw, dtype=np.float32)
print(f"source   : {SRC}")
print(f"duration : {wav.size / SR:.1f}s  ({wav.size} samples)")


def infer(piece):
    body = json.dumps({
        "inputs": [{"name": "WAV", "shape": [int(piece.size)],
                    "datatype": "FP32", "data": piece.tolist()}]
    }).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        out = json.loads(r.read())
    for o in out.get("outputs", []):
        if o.get("name") == "TRANSCRIPT":
            d = o.get("data", [""])[0]
            return d.decode() if isinstance(d, (bytes, bytearray)) else str(d)
    return ""


for chunk_s in (float(x) for x in os.environ.get("PROBE_CHUNKS", "30,20,15").split(",")):
    n = int(round(chunk_s * SR))
    print(f"\n=== window size {chunk_s:g}s ===")
    joined = []
    start = 0
    idx = 0
    while start < wav.size:
        end = min(start + n, wav.size)
        piece = wav[start:end]
        if piece.size < int(0.5 * SR):
            print(f"  w{idx} [{start/SR:.0f}-{end/SR:.0f}s] SKIPPED (tail < 0.5s)")
            break
        t = infer(piece).strip()
        joined.append(t)
        print(f"  w{idx} [{start/SR:.0f}-{end/SR:.0f}s] {len(t):4d} chars: {t[:200]}")
        if end >= wav.size:
            break
        start += n
        idx += 1
    total = " ".join(x for x in joined if x)
    print(f"  JOINED: {len(total)} chars")
