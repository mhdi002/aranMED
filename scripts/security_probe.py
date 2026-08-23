#!/usr/bin/env python3
"""Adversarial probe against a running deployment.

Exercises the controls the platform claims: SQL injection, authentication and
token handling, authorisation boundaries, and input abuse. Every target is
env-driven (SEC_BASE, SEC_ENV_FILE) so this runs against any environment
without editing the script.

Exit code is the number of FAILed checks, so CI can gate on it.
"""
import os
import re
import sys
import time
import uuid

import httpx

BASE = os.environ.get("SEC_BASE", "http://gateway:8080")
ENV_FILE = os.environ.get("SEC_ENV_FILE", "/tmp/.env")

rows: list[tuple[str, str, str]] = []


def ok(name: str, detail: str) -> None:
    rows.append(("PASS", name, detail))


def bad(name: str, detail: str) -> None:
    rows.append(("FAIL", name, detail))


def env(key: str, default: str = "") -> str:
    try:
        m = re.search(rf"^{key}=(.*)$", open(ENV_FILE).read(), re.M)
        return m.group(1).strip() if m else default
    except OSError:
        return default


ADMIN_U = env("ASR_AGENT_ADMIN_USER", "admin")
ADMIN_P = env("ASR_AGENT_ADMIN_PASSWORD")

# Classic auth-bypass and destructive payloads. Against a vulnerable login these
# return a token; against a parameterised one they return 401 and the schema
# survives (checked separately below).
SQLI = [
    "' OR '1'='1",
    "' OR 1=1 --",
    "admin'--",
    "admin'/*",
    "' UNION SELECT NULL,NULL,NULL --",
    "'; DROP TABLE users; --",
    "' OR ''='",
    '" OR ""="',
    "1' AND SLEEP(3)--",
    "') OR ('1'='1",
]

with httpx.Client(timeout=60, follow_redirects=False) as c:
    # --- 1. SQL injection on the login form ---------------------------------
    leaked = []
    for payload in SQLI:
        r = c.post(f"{BASE}/api/auth/login",
                   data={"username": payload, "password": payload})
        if r.status_code == 200 and (r.json() or {}).get("access_token"):
            leaked.append(payload)
    if leaked:
        bad("sqli.login", f"AUTH BYPASSED by {len(leaked)}: {leaked[:3]}")
    else:
        ok("sqli.login", f"{len(SQLI)} payloads all rejected")

    # --- 2. Time-based blind SQL injection ----------------------------------
    t0 = time.perf_counter()
    c.post(f"{BASE}/api/auth/login",
           data={"username": "x' AND SLEEP(5)--", "password": "x"})
    dt = time.perf_counter() - t0
    if dt > 4.0:
        bad("sqli.blind_time", f"delay honoured: {dt:.1f}s -> injectable")
    else:
        ok("sqli.blind_time", f"no delay ({dt:.2f}s)")

    # --- 3. Authenticate for real -------------------------------------------
    r = c.post(f"{BASE}/api/auth/login",
               data={"username": ADMIN_U, "password": ADMIN_P})
    TOK = (r.json() or {}).get("access_token", "") if r.status_code == 200 else ""
    if TOK:
        ok("auth.login", "valid credentials accepted")
    elif r.status_code == 429:
        # A previous run's brute-force check (16) leaves the limiter armed.
        # That is the control working, but it makes every authenticated check
        # below meaningless, so say so instead of reporting phantom failures.
        print("ABORT: login is rate-limited from an earlier run (HTTP 429).")
        print("       Wait for the throttle window to expire and re-run.")
        sys.exit(2)
    else:
        bad("auth.login", f"could not authenticate ({r.status_code})")
    # An empty bearer value is not a legal header, so httpx refuses to send it
    # at all -- which crashes the probe instead of reporting the failure.
    AUTH = {"Authorization": f"Bearer {TOK}"} if TOK else {}

    # --- 4. Schema survived the DROP TABLE attempt --------------------------
    r = c.get(f"{BASE}/api/ehr", headers=AUTH)
    if r.status_code in (200, 403):
        ok("sqli.integrity", f"users/ehr tables intact ({r.status_code})")
    else:
        bad("sqli.integrity", f"unexpected {r.status_code}: {r.text[:120]}")

    # --- 5. Injection through an authenticated data path --------------------
    hits = []
    for payload in ["' OR '1'='1", "'; DROP TABLE ehr_records; --",
                    "1 UNION SELECT 1,2,3"]:
        if c.get(f"{BASE}/api/ehr/{payload}", headers=AUTH).status_code == 200:
            hits.append(payload)
    if hits:
        bad("sqli.path_param", f"injected id returned 200: {hits}")
    else:
        ok("sqli.path_param", "injected record ids return no data")

    # --- 6. Anonymous access to protected resources -------------------------
    unprotected = [p for p in ("/api/ehr", "/api/audit", "/api/auth/sessions")
                   if c.get(f"{BASE}{p}").status_code == 200]
    if unprotected:
        bad("authz.anonymous", f"served without a token: {unprotected}")
    else:
        ok("authz.anonymous", "protected paths reject anonymous callers")

    # --- 7. Forged and tampered tokens --------------------------------------
    forged = {
        "garbage": "not-a-token",
        "alg-none": ("eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
                     "eyJzdWIiOiJhZG1pbiIsInJvbGUiOiJhZG1pbiJ9."),
        "empty-sig": (TOK.rsplit(".", 1)[0] + ".") if TOK.count(".") == 2 else "a.b.",
        "bitflip": (TOK[:-3] + ("aaa" if not TOK.endswith("aaa") else "bbb"))
                   if TOK else "x.y.z",
    }
    accepted = [
        k for k, v in forged.items()
        if c.get(f"{BASE}/api/ehr",
                 headers={"Authorization": f"Bearer {v}"}).status_code == 200
    ]
    if accepted:
        bad("auth.token_forgery", f"ACCEPTED forged tokens: {accepted}")
    else:
        ok("auth.token_forgery", f"all {len(forged)} forged tokens rejected")

    # --- 8. Logout actually revokes -----------------------------------------
    r2 = c.post(f"{BASE}/api/auth/login",
                data={"username": ADMIN_U, "password": ADMIN_P})
    t2 = (r2.json() or {}).get("access_token", "")
    if t2:
        h2 = {"Authorization": f"Bearer {t2}"}
        before = c.get(f"{BASE}/api/ehr", headers=h2).status_code
        c.post(f"{BASE}/api/auth/logout", headers=h2)
        after = c.get(f"{BASE}/api/ehr", headers=h2).status_code
        if before == 200 and after == 200:
            bad("auth.revocation", "token still works after logout")
        else:
            ok("auth.revocation", f"{before} before logout -> {after} after")
    else:
        bad("auth.revocation", "could not obtain a token to revoke")

    # --- 9. Password policy --------------------------------------------------
    weak = []
    for pw in ("123456", "password", "abc"):
        r = c.post(f"{BASE}/api/auth/register",
                   data={"username": f"probe_{uuid.uuid4().hex[:8]}",
                         "password": pw, "role": "student"})
        if r.status_code in (200, 201):
            weak.append(pw)
    if weak:
        bad("auth.password_policy", f"accepted weak passwords: {weak}")
    else:
        ok("auth.password_policy", "weak passwords rejected")

    # --- 10. Privilege escalation via a self-declared role ------------------
    user = f"probe_{uuid.uuid4().hex[:8]}"
    strong = "Zx9!qR7#mK2$vL5w"
    reg = c.post(f"{BASE}/api/auth/register",
                 data={"username": user, "password": strong, "role": "admin"})
    if reg.status_code in (200, 201):
        rr = c.post(f"{BASE}/api/auth/login",
                    data={"username": user, "password": strong})
        ut = (rr.json() or {}).get("access_token", "")
        code = c.get(f"{BASE}/api/audit",
                     headers={"Authorization": f"Bearer {ut}"}).status_code
        detail = f"self-declared admin -> /api/audit {code}"
        bad("authz.privilege_escalation", detail) if code == 200 \
            else ok("authz.privilege_escalation", detail)
    else:
        ok("authz.privilege_escalation",
           f"self-service registration closed ({reg.status_code})")

    # --- 11. Oversized body is refused, not absorbed ------------------------
    r = c.post(f"{BASE}/api/chat", data={"text": "A" * 5_000_000},
               headers=AUTH, timeout=120)
    if r.status_code in (413, 400, 422):
        ok("abuse.oversized_body", f"rejected ({r.status_code})")
    elif r.status_code == 200:
        bad("abuse.oversized_body", "5MB prompt accepted -- no input cap")
    else:
        ok("abuse.oversized_body", f"not accepted ({r.status_code})")

    # --- 12. Path traversal on template retrieval ---------------------------
    traversed = []
    for payload in ("../../../../etc/passwd", "..%2f..%2f..%2fetc%2fpasswd",
                    "....//....//etc/passwd"):
        r = c.get(f"{BASE}/api/templates/{payload}", headers=AUTH)
        if r.status_code == 200 and "root:" in r.text:
            traversed.append(payload)
    if traversed:
        bad("abuse.path_traversal", f"read host files: {traversed}")
    else:
        ok("abuse.path_traversal", "traversal payloads read no host files")

    # --- 13. Security headers ------------------------------------------------
    r = c.get(f"{BASE}/api/health")
    present = {k.lower() for k in r.headers}
    missing = [h for h in ("x-content-type-options", "x-frame-options",
                           "content-security-policy", "referrer-policy")
               if h not in present]
    if missing:
        bad("headers.security", f"missing: {', '.join(missing)}")
    else:
        ok("headers.security", "content-type/frame/CSP/referrer all set")

    # --- 14. Server banner does not disclose versions -----------------------
    srv = r.headers.get("server", "")
    if re.search(r"\d+\.\d+", srv):
        bad("headers.banner", f"version disclosed: {srv!r}")
    else:
        ok("headers.banner", f"no version in Server: {srv!r}")

    # --- 15. Errors do not leak stack traces --------------------------------
    r = c.get(f"{BASE}/api/ehr/missing-{uuid.uuid4().hex}", headers=AUTH)
    body = r.text.lower()
    if "traceback" in body or "/app/backend" in body:
        bad("errors.leakage", "stack trace / host path in error body")
    else:
        ok("errors.leakage", f"opaque error body ({r.status_code})")

    # --- 16. Brute-force throttling on login --------------------------------
    codes = [c.post(f"{BASE}/api/auth/login",
                    data={"username": ADMIN_U, "password": "wrong"}).status_code
             for _ in range(25)]
    if 429 in codes:
        ok("abuse.rate_limit", f"throttled after {codes.index(429)} bad logins")
    else:
        bad("abuse.rate_limit", "25 failed logins, never throttled")

print("=== AranMed security probe ===")
print(f"    target: {BASE}\n")
print(f"{'STATUS':6} {'CHECK':28} DETAIL")
print(f"{'------':6} {'-' * 28} ------")
for status, name, detail in rows:
    print(f"{status:6} {name:28} {detail}")
fails = sum(1 for s, _, _ in rows if s == "FAIL")
print(f"\npassed={len(rows) - fails} failed={fails}")
sys.exit(fails)
