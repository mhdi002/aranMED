#!/usr/bin/env python3
"""Concurrency / load test for the AranMed stack.

Drives N concurrent virtual users against the gateway (or any base URL) and
reports throughput, latency percentiles and error taxonomy per endpoint.

Nothing is hardcoded: the target comes from scripts/_endpoints.py (env), and
the scenario mix, concurrency, duration and thresholds are all CLI/env driven.

Design notes
------------
* Uses asyncio + httpx with a bounded connection pool, so a 1000-user run does
  not simply exhaust local sockets and measure the client instead of the server.
* Separates *scenarios* by cost class. Hammering GPU endpoints with 1000
  concurrent users measures queueing, not capacity, so heavy AI scenarios are
  opt-in (--include-ai) and rate-limited independently.
* Reports p50/p90/p95/p99 and a full status-code histogram; a run "passes" only
  if the error rate and p95 stay within budget.

Usage
-----
    python scripts/loadtest.py --users 1000 --duration 60
    python scripts/loadtest.py --users 1000 --duration 60 --base http://127.0.0.1:8090
    python scripts/loadtest.py --users 50 --duration 30 --include-ai
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from _endpoints import BACKEND_URL, GATEWAY_URL  # noqa: E402


@dataclass
class Scenario:
    """One request shape a virtual user can perform."""

    name: str
    method: str
    path: str
    weight: int = 1
    json_body: dict | None = None
    heavy: bool = False          # touches GPU/LLM — excluded unless --include-ai
    expect: tuple[int, ...] = (200,)
    group: str = "api"           # "api" (backend) | "ui" (frontend) — see --group


# Read-mostly scenarios: safe to drive at very high concurrency. These are the
# endpoints a real 1000-user population hits constantly (page loads, health
# polling, template pickers).
#
# UI scenarios only exist behind the gateway (or the frontend itself); pointing
# the test straight at the backend would score them as 404s and mask the real
# API numbers, hence the group filter.
LIGHT_SCENARIOS = [
    Scenario("live",      "GET", "/api/live",      weight=2, group="api"),
    Scenario("health",    "GET", "/api/health",    weight=3, group="api"),
    Scenario("templates", "GET", "/api/templates", weight=3, group="api"),
    Scenario("ui_login",  "GET", "/login",         weight=2, group="ui"),
    Scenario("ui_root",   "GET", "/",              weight=1, group="ui",
             expect=(200, 302, 307)),
]

# Cost-class: each of these occupies a model for seconds-to-minutes.
HEAVY_SCENARIOS = [
    Scenario(
        "report", "POST", "/api/report", weight=1, heavy=True,
        json_body={
            "transcript": "This is a chest sonography. No pleural effusion. "
                          "No pneumothorax. Diaphragmatic movement is normal.",
            "template_id": "chest",
        },
    ),
]


@dataclass
class Stats:
    latencies_ms: list[float] = field(default_factory=list)
    statuses: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)

    def record(self, ms: float, status: int | None, err: str | None) -> None:
        if err:
            self.errors[err] += 1
            return
        self.latencies_ms.append(ms)
        self.statuses[status] += 1

    @property
    def count(self) -> int:
        return len(self.latencies_ms) + sum(self.errors.values())

    def pct(self, p: float) -> float:
        if not self.latencies_ms:
            return float("nan")
        data = sorted(self.latencies_ms)
        k = max(0, min(len(data) - 1, int(round((p / 100.0) * len(data) + 0.5)) - 1))
        return data[k]

    def summary(self, expect: tuple[int, ...]) -> dict:
        ok = sum(n for s, n in self.statuses.items() if s in expect)
        total = self.count
        return {
            "requests": total,
            "ok": ok,
            "error_rate": round(1 - (ok / total), 4) if total else 0.0,
            "rps": None,  # filled in by caller (needs wall time)
            "p50_ms": round(self.pct(50), 1) if self.latencies_ms else None,
            "p90_ms": round(self.pct(90), 1) if self.latencies_ms else None,
            "p95_ms": round(self.pct(95), 1) if self.latencies_ms else None,
            "p99_ms": round(self.pct(99), 1) if self.latencies_ms else None,
            "max_ms": round(max(self.latencies_ms), 1) if self.latencies_ms else None,
            "mean_ms": round(statistics.fmean(self.latencies_ms), 1) if self.latencies_ms else None,
            "statuses": dict(sorted(self.statuses.items(), key=lambda kv: str(kv[0]))),
            "transport_errors": dict(self.errors),
            # Raw samples so a multi-process run can recompute true percentiles
            # (averaging per-process percentiles would be statistically wrong).
            "_lat": [round(x, 2) for x in self.latencies_ms],
        }


def pick(scenarios: list[Scenario], rng: random.Random) -> Scenario:
    return rng.choices(scenarios, weights=[s.weight for s in scenarios], k=1)[0]


async def virtual_user(
    client, scenarios: list[Scenario], stats: dict[str, Stats],
    stop_at: float, rng: random.Random, think_time: float, sem,
) -> None:
    while time.perf_counter() < stop_at:
        sc = pick(scenarios, rng)
        t0 = time.perf_counter()
        status, err = None, None
        try:
            async with sem:
                r = await client.request(
                    sc.method, sc.path,
                    json=sc.json_body if sc.json_body else None,
                )
            status = r.status_code
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__
        stats[sc.name].record((time.perf_counter() - t0) * 1000, status, err)
        if think_time:
            await asyncio.sleep(rng.uniform(0, think_time))


async def run(args) -> dict:
    import httpx

    scenarios = list(LIGHT_SCENARIOS)
    if args.include_ai:
        scenarios += HEAVY_SCENARIOS
    if args.group != "all":
        scenarios = [s for s in scenarios if s.group == args.group]
    if not scenarios:
        raise SystemExit(f"no scenarios match --group {args.group}")

    stats: dict[str, Stats] = defaultdict(Stats)
    # Bound in-flight requests so we measure the server, not client socket churn.
    sem = asyncio.Semaphore(args.max_inflight or args.users)
    limits = httpx.Limits(
        max_connections=args.max_inflight or args.users,
        max_keepalive_connections=args.keepalive,
    )
    timeout = httpx.Timeout(args.timeout, connect=args.connect_timeout)

    print(f"target      : {args.base}")
    print(f"users       : {args.users} (max in-flight {args.max_inflight or args.users})")
    print(f"duration    : {args.duration}s   ramp: {args.ramp}s   think: {args.think}s")
    print(f"scenarios   : {', '.join(s.name for s in scenarios)}")
    print("running…\n")

    async with httpx.AsyncClient(
        base_url=args.base, limits=limits, timeout=timeout,
        follow_redirects=False, headers={"User-Agent": "aranmed-loadtest"},
    ) as client:
        wall0 = time.perf_counter()
        stop_at = wall0 + args.duration
        tasks = []
        for i in range(args.users):
            # Ramp arrivals so we exercise steady state, not a synchronised spike.
            delay = (i / max(1, args.users)) * args.ramp
            rng = random.Random(args.seed + i)

            async def spawn(d=delay, r=rng):
                await asyncio.sleep(d)
                await virtual_user(client, scenarios, stats, stop_at, r, args.think, sem)

            tasks.append(asyncio.create_task(spawn()))
        await asyncio.gather(*tasks, return_exceptions=True)
        wall = time.perf_counter() - wall0

    by_scenario, total_req, total_ok = {}, 0, 0
    for sc in scenarios:
        s = stats[sc.name]
        summ = s.summary(sc.expect)
        summ["rps"] = round(summ["requests"] / wall, 1) if wall else None
        summ["expect"] = list(sc.expect)
        by_scenario[sc.name] = summ
        total_req += summ["requests"]
        total_ok += summ["ok"]

    error_rate = round(1 - (total_ok / total_req), 4) if total_req else 1.0
    all_lat = [ms for sc in scenarios for ms in stats[sc.name].latencies_ms]
    agg = Stats(latencies_ms=all_lat)
    p95 = round(agg.pct(95), 1) if all_lat else None

    result = {
        "target": args.base,
        "users": args.users,
        "duration_s": round(wall, 1),
        "include_ai": args.include_ai,
        "total_requests": total_req,
        "throughput_rps": round(total_req / wall, 1) if wall else 0,
        "error_rate": error_rate,
        "p50_ms": round(agg.pct(50), 1) if all_lat else None,
        "p95_ms": p95,
        "p99_ms": round(agg.pct(99), 1) if all_lat else None,
        "by_scenario": by_scenario,
        "thresholds": {"max_error_rate": args.max_error_rate, "max_p95_ms": args.max_p95_ms},
    }
    result["passed"] = bool(
        total_req > 0
        and error_rate <= args.max_error_rate
        and (p95 is not None and p95 <= args.max_p95_ms)
    )
    return result


def report(result: dict) -> None:
    print(f"{'scenario':<14}{'reqs':>8}{'rps':>9}{'err':>8}{'p50':>9}{'p95':>9}{'p99':>9}")
    print("-" * 66)
    for name, s in result["by_scenario"].items():
        print(f"{name:<14}{s['requests']:>8}{s['rps']:>9}"
              f"{s['error_rate']:>8.1%}{str(s['p50_ms']):>9}"
              f"{str(s['p95_ms']):>9}{str(s['p99_ms']):>9}")
    print("-" * 66)
    print(f"{'TOTAL':<14}{result['total_requests']:>8}{result['throughput_rps']:>9}"
          f"{result['error_rate']:>8.1%}{str(result['p50_ms']):>9}"
          f"{str(result['p95_ms']):>9}{str(result['p99_ms']):>9}")

    bad = {n: s for n, s in result["by_scenario"].items() if s["transport_errors"]}
    if bad:
        print("\ntransport errors:")
        for n, s in bad.items():
            print(f"  {n}: {s['transport_errors']}")

    th = result["thresholds"]
    print(f"\nthresholds: error_rate <= {th['max_error_rate']:.1%}, p95 <= {th['max_p95_ms']}ms")
    print(f"RESULT: {'PASS' if result['passed'] else 'FAIL'}")


def _worker_entry(payload: str) -> str:
    """Child-process entrypoint: run a slice of the users, return JSON stats."""
    import argparse as _a

    args = _a.Namespace(**json.loads(payload))
    return json.dumps(asyncio.run(run(args)))


def merge_results(parts: list[dict], args) -> dict:
    """Combine per-process results. Latency percentiles are recomputed from the
    union of raw samples (averaging percentiles across processes would be
    statistically wrong)."""
    by_scenario: dict[str, dict] = {}
    total_req = total_ok = 0
    wall = max((p["duration_s"] for p in parts), default=0.0)

    for p in parts:
        for name, s in p["by_scenario"].items():
            tgt = by_scenario.setdefault(name, {
                "requests": 0, "ok": 0, "statuses": {}, "transport_errors": {},
                "_lat": [], "expect": s.get("expect", [200]),
            })
            tgt["requests"] += s["requests"]
            tgt["ok"] += s["ok"]
            tgt["_lat"].extend(s.get("_lat", []))
            for k, v in (s.get("statuses") or {}).items():
                tgt["statuses"][str(k)] = tgt["statuses"].get(str(k), 0) + v
            for k, v in (s.get("transport_errors") or {}).items():
                tgt["transport_errors"][k] = tgt["transport_errors"].get(k, 0) + v

    all_lat: list[float] = []
    for name, s in by_scenario.items():
        lat = s.pop("_lat")
        all_lat.extend(lat)
        st = Stats(latencies_ms=lat)
        s["rps"] = round(s["requests"] / wall, 1) if wall else None
        s["error_rate"] = round(1 - (s["ok"] / s["requests"]), 4) if s["requests"] else 0.0
        for p_ in (50, 90, 95, 99):
            s[f"p{p_}_ms"] = round(st.pct(p_), 1) if lat else None
        s["max_ms"] = round(max(lat), 1) if lat else None
        total_req += s["requests"]
        total_ok += s["ok"]

    agg = Stats(latencies_ms=all_lat)
    error_rate = round(1 - (total_ok / total_req), 4) if total_req else 1.0
    p95 = round(agg.pct(95), 1) if all_lat else None
    out = {
        "target": args.base,
        "users": args.users,
        "processes": args.processes,
        "duration_s": round(wall, 1),
        "include_ai": args.include_ai,
        "total_requests": total_req,
        "throughput_rps": round(total_req / wall, 1) if wall else 0,
        "error_rate": error_rate,
        "p50_ms": round(agg.pct(50), 1) if all_lat else None,
        "p95_ms": p95,
        "p99_ms": round(agg.pct(99), 1) if all_lat else None,
        "by_scenario": by_scenario,
        "thresholds": {"max_error_rate": args.max_error_rate, "max_p95_ms": args.max_p95_ms},
    }
    out["passed"] = bool(
        total_req > 0 and error_rate <= args.max_error_rate
        and (p95 is not None and p95 <= args.max_p95_ms)
    )
    return out


def main() -> int:
    default_base = os.getenv("LOADTEST_BASE") or GATEWAY_URL or BACKEND_URL
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=default_base, help=f"Base URL (default: {default_base})")
    ap.add_argument("--users", type=int, default=int(os.getenv("LOADTEST_USERS", "1000")))
    ap.add_argument("--duration", type=float, default=float(os.getenv("LOADTEST_DURATION", "60")))
    ap.add_argument("--ramp", type=float, default=float(os.getenv("LOADTEST_RAMP", "10")), help="Seconds to ramp all users in")
    ap.add_argument("--think", type=float, default=float(os.getenv("LOADTEST_THINK", "0.5")), help="Max think time between requests")
    ap.add_argument("--max-inflight", type=int, default=int(os.getenv("LOADTEST_MAX_INFLIGHT", "0")) or None)
    ap.add_argument("--keepalive", type=int, default=int(os.getenv("LOADTEST_KEEPALIVE", "200")))
    ap.add_argument("--timeout", type=float, default=float(os.getenv("LOADTEST_TIMEOUT", "30")))
    ap.add_argument("--connect-timeout", type=float, default=float(os.getenv("LOADTEST_CONNECT_TIMEOUT", "10")))
    ap.add_argument("--include-ai", action="store_true", help="Also drive GPU/LLM endpoints")
    ap.add_argument("--group", choices=("all", "api", "ui"), default=os.getenv("LOADTEST_GROUP", "all"),
                    help="Scenario group: 'api' when targeting the backend directly, "
                         "'ui' for the frontend, 'all' behind the gateway (default)")
    ap.add_argument("--max-error-rate", type=float, default=float(os.getenv("LOADTEST_MAX_ERROR_RATE", "0.01")))
    ap.add_argument("--max-p95-ms", type=float, default=float(os.getenv("LOADTEST_MAX_P95_MS", "2000")))
    ap.add_argument("--processes", type=int, default=int(os.getenv("LOADTEST_PROCESSES", "1")),
                    help="Load-generator processes. One asyncio process cannot saturate "
                         "a fast server at high concurrency — use 4-8 for 1000+ users.")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "loadtest.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    args.base = args.base.rstrip("/")

    if args.processes > 1:
        from concurrent.futures import ProcessPoolExecutor

        per = max(1, args.users // args.processes)
        payloads = []
        for i in range(args.processes):
            slice_args = vars(args).copy()
            slice_args["users"] = per
            slice_args["seed"] = args.seed + i * 100000
            slice_args["out"] = None          # children never write files
            slice_args["json"] = False
            payloads.append(json.dumps(slice_args, default=str))
        print(f"spawning {args.processes} load-generator processes "
              f"× {per} users = {per * args.processes} total\n")
        with ProcessPoolExecutor(max_workers=args.processes) as ex:
            parts = [json.loads(r) for r in ex.map(_worker_entry, payloads)]
        result = merge_results(parts, args)
    else:
        result = asyncio.run(run(args))
        for s in result["by_scenario"].values():
            s.pop("_lat", None)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        report(result)

    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        if not args.json:
            print(f"\nwrote {args.out}")
    except Exception:  # noqa: BLE001
        pass
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
