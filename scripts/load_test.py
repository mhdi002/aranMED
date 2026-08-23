#!/usr/bin/env python3
"""Concurrency probe: hammer the agent and report latency/error distribution.

Everything is env-driven so the same script runs against any deployment:
  LOAD_BASE, LOAD_N, LOAD_CONCURRENCY, LOAD_ENV_FILE
"""
import asyncio, json, os, re, statistics, sys, time
import httpx

BASE = os.environ.get("LOAD_BASE", "http://localhost:8090")
N = int(os.environ.get("LOAD_N", "100"))
CONC = int(os.environ.get("LOAD_CONCURRENCY", "25"))
ENV_FILE = os.environ.get("LOAD_ENV_FILE", "/opt/aranmed/.env")

QUESTIONS = [
    "What are the ultrasound features of acute cholecystitis?",
    "List the available report templates.",
    "Explain hepatic steatosis grading on ultrasound.",
    "What does a dilated CBD suggest?",
    "List the EHR records you have access to.",
]

def env(key, default=""):
    try:
        m = re.search(rf"^{key}=(.*)$", open(ENV_FILE).read(), re.M)
        return m.group(1).strip() if m else default
    except OSError:
        return default

async def login(c):
    r = await c.post(f"{BASE}/api/auth/login", data={
        "username": env("ASR_AGENT_ADMIN_USER", "admin"),
        "password": env("ASR_AGENT_ADMIN_PASSWORD"),
    })
    return r.json().get("access_token", "") if r.status_code == 200 else ""

async def main():
    lat, codes, errs = [], {}, []
    async with httpx.AsyncClient(timeout=600) as c:
        tok = await login(c)
        print(f"auth: {'token acquired' if tok else 'ANONYMOUS'}")
        hdr = {"Authorization": f"Bearer {tok}"} if tok else {}
        sem = asyncio.Semaphore(CONC)

        async def one(i):
            q = QUESTIONS[i % len(QUESTIONS)]
            async with sem:
                t0 = time.perf_counter()
                try:
                    r = await c.post(f"{BASE}/api/chat", headers=hdr,
                                     data={"text": q, "session_id": f"load{i}"})
                    dt = time.perf_counter() - t0
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                    lat.append(dt)
                    if r.status_code != 200:
                        errs.append(f"{r.status_code}: {r.text[:120]}")
                except Exception as e:
                    dt = time.perf_counter() - t0
                    lat.append(dt); codes["EXC"] = codes.get("EXC", 0) + 1
                    errs.append(f"EXC: {type(e).__name__}: {e}")

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(N)))
        wall = time.perf_counter() - t0

    lat.sort()
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0
    print(f"\nrequests   : {N} at concurrency {CONC}")
    print(f"wall clock : {wall:.1f}s  ({N / wall:.2f} req/s)")
    print(f"status     : {codes}")
    print(f"latency    : min {min(lat):.2f}s  p50 {pct(.5):.2f}s  "
          f"p95 {pct(.95):.2f}s  max {max(lat):.2f}s  mean {statistics.mean(lat):.2f}s")
    ok = codes.get(200, 0)
    print(f"success    : {ok}/{N} ({100*ok/N:.1f}%)")
    for e in errs[:8]:
        print("  err:", e)
    sys.exit(0 if ok == N else 1)

asyncio.run(main())
