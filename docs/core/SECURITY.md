# AranMed Security Reference

What is enforced, what is configurable, and what is deliberately left to the
operator. Every control below is env-driven — see `.env.example` for the full
key list and `docs/core/CONFIGURATION.md` for the no-hardcoding rule they follow.

---

## 1. SQL injection

Structurally prevented, not filtered. Every database call in `backend/` goes
through `sqlite3` parameter binding (`c.execute("… WHERE id=?", (value,))`).
No query is built by string interpolation, formatting, or f-string, so
attacker-controlled text is never parsed as SQL.

Verify it stays that way:

```bash
grep -rnE "execute\(" backend --include=*.py | grep -iE "%|\.format\(|f\"|f'"
```

That search must return nothing. A hit means someone introduced an
interpolated query and it needs to become a bound parameter.

Escaping helpers were deliberately *not* added — an escaping layer implies
interpolation is acceptable somewhere, which is the weaker guarantee.

## 2. Authentication

| Control | Implementation |
| --- | --- |
| Password storage | `scrypt` (n=2^14, r=8, p=1), per-password random salt, constant-time compare (`hmac.compare_digest`). |
| Session tokens | HS256 JWS (`base64url(header).base64url(payload).base64url(sig)`), signature verified before the payload is read, `exp` enforced. |
| User enumeration | A login for a non-existent username still runs one full scrypt round against a fixed dummy hash, so "no such user" and "wrong password" take the same time and return the identical message. |
| Brute force | Per `username|IP` throttle: `LOGIN_MAX_ATTEMPTS` (default 8) failures inside `LOGIN_WINDOW_SEC` (300s) trigger a `LOGIN_LOCKOUT_SEC` (900s) lockout, answered `429` with `Retry-After`. |

### The signing secret is required before scaling

`ASR_AGENT_SECRET` signs every token. If it is unset each worker generates its
own random secret, so a token minted by one replica is rejected by the next —
users get seemingly random 401s the moment `backend` runs more than one
replica, and every restart logs everyone out. The backend logs a loud warning
at startup when it has to generate one.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # put in .env
```

### Throttle counters must be shared to actually bound an attacker

Per-process counters hand an attacker N times the attempt budget on an
N-replica deployment, because the gateway spreads their guesses across all of
them. `backend/throttle.py` therefore defaults to a shared store:

| `LOGIN_THROTTLE_BACKEND` | Storage | Use when |
| --- | --- | --- |
| `auto` *(default)* | Redis if `REDIS_URL` is set, else SQLite | Almost always. |
| `sqlite` | The existing `app.db`, shared by every replica through the `backend/data` bind mount | Default — shared with zero new infrastructure. |
| `redis` | Redis (`REDIS_URL`) | Login volume high enough that a SQLite write per failed attempt matters, or replicas don't share a filesystem. |
| `memory` | Per-process dict | Single replica, or tests. Logs a warning. |

A throttle-store failure never blocks a legitimate login — it is logged and
treated as "no history".

## 3. First-run admin account

There is **no well-known default password**. On first start, in precedence order:

1. `ASR_AGENT_ADMIN_PASSWORD` — what a real deployment should set.
2. `ASR_AGENT_ALLOW_INSECURE_ADMIN=1` — explicit opt-in to `admin`/`admin`
   for throwaway local work. Logs a warning naming the risk.
3. Otherwise a random password is generated, written to
   `ADMIN_CREDENTIALS_FILE` (default `backend/data/initial-admin-password.txt`,
   mode `0600`), and logged once.

The reasoning: a seeded-but-unknown password is recoverable, while a seeded
*guessable* password on a reachable deployment is not recoverable at all. So
the guessable one now has to be asked for by name.

Sign in, change the password, delete the credentials file.

## 4. Transport security

TLS termination at the gateway is opt-in:

```bash
./deploy.sh --tls          # or: docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
```

Enabling it makes `deploy/nginx/10-aranmed-tls.sh` rewrite the single gateway
template into a TLS listener (TLS 1.2/1.3, HTTP/2, HSTS) and prepend a server
block that 301s plain HTTP to HTTPS. `/healthz` stays answerable over plain
HTTP so the container healthcheck doesn't chase a redirect. If TLS is enabled
but the certificate is missing or unreadable the gateway **fails to start**
rather than quietly serving plaintext.

Bring your own certificate — put `tls.crt`/`tls.key` in `deploy/nginx/certs/`
(gitignored) or point `GATEWAY_TLS_CERT_DIR` at an existing lineage. Filenames,
protocols, ciphers, session cache, and HSTS max-age are all env-configurable.

Local test certificate:

```bash
mkdir -p deploy/nginx/certs && openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout deploy/nginx/certs/tls.key -out deploy/nginx/certs/tls.crt \
  -days 365 -subj "/CN=localhost"
```

This terminates TLS **at the gateway**. Gateway→backend traffic stays on the
internal compose network in plaintext, which is the normal arrangement behind
a terminator. If that network isn't trusted, terminate upstream instead.

## 5. Request-level limits (gateway)

`deploy/nginx/nginx.conf.template` rate- and connection-limits the expensive AI
endpoints per client IP (`LB_AI_RATE`, `LB_AI_BURST`, `LB_MAX_CONN_PER_IP`)
while leaving cheap reads unthrottled, and caps upload size
(`LB_MAX_BODY_SIZE`). This is throughput protection, distinct from the login
throttle above.

## 6. Data scoping

EHR records are owned by a user row and every read/write is scoped by
`owner_user_id` — the REST routes via the bearer token, and the chat/tool path
via `ToolContext.owner_user_id` threaded from the same token. One session
cannot list or fetch another clinician's patients. Unauthenticated chat falls
back to a configured system-owner account
(`EHR_TOOL_SYSTEM_OWNER_USERNAME`), never to another user's namespace.

## 7. Known limits

Bounded and deliberate, listed so nobody assumes otherwise:

- **Gateway→upstream traffic is plaintext** on the compose network (see §4).
- **`memory` throttle backend is per-process.** Only correct at one replica;
  the default is not `memory`.
- **Token revocation is expiry-only.** There is no deny-list, so a stolen
  token stays valid until `ASR_AGENT_TOKEN_TTL` elapses. Shorten the TTL if
  that window matters; rotating `ASR_AGENT_SECRET` invalidates everything at once.
- **No MFA, no password-complexity policy** beyond a minimum length.
- **The credentials file is plaintext on disk** by design — it is meant to be
  read once and deleted.
