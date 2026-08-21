"""Brute-force login throttling with a pluggable, shareable backend.

The counters behind a login lockout have to be *shared* to actually bound an
attacker: with per-process counters, N backend replicas hand out N times the
attempt budget, and the gateway load-balances an attacker across all of them
for free. This module keeps the counting logic in one place and lets the
storage move to whatever the deployment already runs.

Backends (select with ``LOGIN_THROTTLE_BACKEND``):

``auto`` (default)
    Redis when ``REDIS_URL`` is set and the client library imports, else
    SQLite. SQLite is the right zero-infrastructure default here because
    every backend replica already shares ``backend/data/app.db`` through a
    bind mount (see docker-compose.yml), so the counters are shared without
    adding a service.
``memory``
    Per-process dict. Fastest, no shared state — correct only for a
    single-replica deployment or tests.
``sqlite``
    Shared through the existing SQLite database. No new dependency.
``redis``
    Shared through Redis. Best when login volume is high enough that the
    SQLite write per failed attempt matters, or when replicas don't share a
    filesystem.

Every tunable is an environment variable; nothing here hardcodes a limit or
an endpoint. See docs/core/CONFIGURATION.md.
"""
from __future__ import annotations

import abc
import json
import logging
import os
import threading
import time

log = logging.getLogger("auth.throttle")

MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "8"))
WINDOW_SEC = float(os.environ.get("LOGIN_WINDOW_SEC", "300"))
LOCKOUT_SEC = float(os.environ.get("LOGIN_LOCKOUT_SEC", "900"))
BACKEND_NAME = os.environ.get("LOGIN_THROTTLE_BACKEND", "auto").strip().lower()
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
REDIS_PREFIX = os.environ.get("LOGIN_THROTTLE_REDIS_PREFIX", "aranmed:login-throttle:")
# Bound the in-process dict / sweep the SQLite table so neither grows forever.
MAX_TRACKED_KEYS = int(os.environ.get("LOGIN_THROTTLE_MAX_KEYS", "10000"))


class ThrottleBackend(abc.ABC):
    """Stores, per key, the recent failure timestamps and a lockout deadline."""

    name = "base"

    @abc.abstractmethod
    def load(self, key: str) -> tuple[list[float], float]:
        """Return ``(attempt_timestamps, locked_until)``; empty/0 if unknown."""

    @abc.abstractmethod
    def save(self, key: str, attempts: list[float], locked_until: float) -> None: ...

    @abc.abstractmethod
    def clear(self, key: str) -> None: ...


class MemoryThrottle(ThrottleBackend):
    name = "memory"

    def __init__(self) -> None:
        self._state: dict[str, tuple[list[float], float]] = {}
        self._lock = threading.Lock()

    def load(self, key: str) -> tuple[list[float], float]:
        with self._lock:
            attempts, until = self._state.get(key, ([], 0.0))
            return list(attempts), until

    def save(self, key: str, attempts: list[float], locked_until: float) -> None:
        now = time.time()
        with self._lock:
            self._state[key] = (attempts, locked_until)
            if len(self._state) > MAX_TRACKED_KEYS:
                for k, (a, u) in list(self._state.items()):
                    idle = not a or now - a[-1] > WINDOW_SEC
                    if idle and u < now:
                        self._state.pop(k, None)

    def clear(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


class SqliteThrottle(ThrottleBackend):
    """Shared counters via the existing SQLite database (no new services)."""

    name = "sqlite"

    def load(self, key: str) -> tuple[list[float], float]:
        import db
        try:
            with db.connect() as c:
                row = c.execute(
                    "SELECT attempts, locked_until FROM login_throttle WHERE key=?",
                    (key,),
                ).fetchone()
        except Exception as e:  # noqa: BLE001
            # Never let a throttle-store failure block a legitimate login.
            log.warning("throttle: sqlite load failed (%s); treating as no history", e)
            return [], 0.0
        if row is None:
            return [], 0.0
        attempts = db.loads_json(row["attempts"]) or []
        return [float(t) for t in attempts], float(row["locked_until"] or 0.0)

    def save(self, key: str, attempts: list[float], locked_until: float) -> None:
        import db
        now = time.time()
        try:
            with db.connect() as c:
                c.execute(
                    """INSERT INTO login_throttle (key, attempts, locked_until, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         attempts=excluded.attempts,
                         locked_until=excluded.locked_until,
                         updated_at=excluded.updated_at""",
                    (key, db.dumps_json(attempts), locked_until, now),
                )
                # Opportunistic sweep of rows that are both out of window and
                # no longer locked, so the table can't grow without bound.
                if int(now) % 60 == 0:
                    c.execute(
                        "DELETE FROM login_throttle WHERE updated_at < ? AND locked_until < ?",
                        (now - max(WINDOW_SEC, LOCKOUT_SEC), now),
                    )
        except Exception as e:  # noqa: BLE001
            log.warning("throttle: sqlite save failed (%s); attempt not recorded", e)

    def clear(self, key: str) -> None:
        import db
        try:
            with db.connect() as c:
                c.execute("DELETE FROM login_throttle WHERE key=?", (key,))
        except Exception as e:  # noqa: BLE001
            log.warning("throttle: sqlite clear failed (%s)", e)

    def record_failure_atomic(self, key: str) -> bool:
        """Read-modify-write inside a single write transaction.

        ``BEGIN IMMEDIATE`` takes the write lock up front, so two concurrent
        failed logins cannot both read the same count and have one increment
        lost — the second waits (up to ``DB_BUSY_TIMEOUT_MS``) and then sees
        the first one's result. Returns True when this failure caused, or
        found, a lockout.
        """
        import db
        now = time.time()
        try:
            conn = db.connect()
            try:
                # isolation_level=None means autocommit, so the transaction
                # boundary is explicit here rather than implied by the driver.
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT attempts, locked_until FROM login_throttle WHERE key=?",
                    (key,),
                ).fetchone()
                attempts: list[float] = []
                locked_until = 0.0
                if row is not None:
                    locked_until = float(row["locked_until"] or 0.0)
                    attempts = [float(t) for t in (db.loads_json(row["attempts"]) or [])
                                if now - float(t) < WINDOW_SEC]
                if locked_until > now:
                    conn.execute("COMMIT")
                    return True
                attempts.append(now)
                if len(attempts) >= MAX_ATTEMPTS:
                    locked_until, attempts = now + LOCKOUT_SEC, []
                else:
                    locked_until = 0.0
                conn.execute(
                    """INSERT INTO login_throttle (key, attempts, locked_until, updated_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET
                         attempts=excluded.attempts,
                         locked_until=excluded.locked_until,
                         updated_at=excluded.updated_at""",
                    (key, db.dumps_json(attempts), locked_until, now),
                )
                conn.execute("COMMIT")
                return locked_until > 0
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("throttle: sqlite atomic record_failure failed (%s); "
                        "attempt not recorded", e)
            return False


class RedisThrottle(ThrottleBackend):
    """Shared counters via Redis. Entries expire on their own."""

    name = "redis"

    def __init__(self, url: str) -> None:
        import redis  # noqa: PLC0415  (optional dependency, imported on demand)
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._r.ping()

    def _k(self, key: str) -> str:
        return f"{REDIS_PREFIX}{key}"

    def load(self, key: str) -> tuple[list[float], float]:
        try:
            raw = self._r.get(self._k(key))
        except Exception as e:  # noqa: BLE001
            log.warning("throttle: redis load failed (%s); treating as no history", e)
            return [], 0.0
        if not raw:
            return [], 0.0
        try:
            doc = json.loads(raw)
            return [float(t) for t in doc.get("attempts", [])], float(doc.get("locked_until", 0.0))
        except (ValueError, TypeError):
            return [], 0.0

    def save(self, key: str, attempts: list[float], locked_until: float) -> None:
        payload = json.dumps({"attempts": attempts, "locked_until": locked_until})
        # Keep the entry alive as long as it could still matter.
        ttl = int(max(WINDOW_SEC, locked_until - time.time(), 1))
        try:
            self._r.setex(self._k(key), ttl, payload)
        except Exception as e:  # noqa: BLE001
            log.warning("throttle: redis save failed (%s); attempt not recorded", e)

    def clear(self, key: str) -> None:
        try:
            self._r.delete(self._k(key))
        except Exception as e:  # noqa: BLE001
            log.warning("throttle: redis clear failed (%s)", e)

    # Server-side Lua so the prune → append → lock decision is one atomic
    # step. Without this, two simultaneous failed logins can both read the
    # same count and one increment is lost.
    _LUA_RECORD_FAILURE = """
    local raw = redis.call('GET', KEYS[1])
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local maxatt = tonumber(ARGV[3])
    local lockout = tonumber(ARGV[4])
    local attempts = {}
    local locked_until = 0
    if raw then
      local ok, doc = pcall(cjson.decode, raw)
      if ok and doc then
        locked_until = tonumber(doc['locked_until']) or 0
        for _, t in ipairs(doc['attempts'] or {}) do
          local tv = tonumber(t)
          if tv and (now - tv) < window then attempts[#attempts + 1] = tv end
        end
      end
    end
    if locked_until > now then
      return 1
    end
    attempts[#attempts + 1] = now
    local ttl
    if #attempts >= maxatt then
      locked_until = now + lockout
      attempts = {}
      ttl = lockout
    else
      locked_until = 0
      ttl = window
    end
    local payload = cjson.encode({attempts = attempts, locked_until = locked_until})
    redis.call('SETEX', KEYS[1], math.max(1, math.floor(ttl)), payload)
    if locked_until > 0 then return 1 else return 0 end
    """

    def record_failure_atomic(self, key: str) -> bool:
        """Returns True when this failure caused (or found) a lockout."""
        try:
            script = getattr(self, "_script", None)
            if script is None:
                script = self._r.register_script(self._LUA_RECORD_FAILURE)
                self._script = script
            res = script(keys=[self._k(key)],
                        args=[time.time(), WINDOW_SEC, MAX_ATTEMPTS, LOCKOUT_SEC])
            return bool(int(res or 0))
        except Exception as e:  # noqa: BLE001
            log.warning("throttle: redis atomic record_failure failed (%s) — "
                        "falling back to read-modify-write", e)
            now = time.time()
            attempts, _ = self.load(key)
            attempts = [t for t in attempts if now - t < WINDOW_SEC]
            attempts.append(now)
            if len(attempts) >= MAX_ATTEMPTS:
                self.save(key, [], now + LOCKOUT_SEC)
                return True
            self.save(key, attempts, 0.0)
            return False


def _build_backend() -> ThrottleBackend:
    choice = BACKEND_NAME
    if choice == "redis" or (choice == "auto" and REDIS_URL):
        if not REDIS_URL:
            log.warning("throttle: LOGIN_THROTTLE_BACKEND=redis but REDIS_URL is unset")
        else:
            try:
                backend = RedisThrottle(REDIS_URL)
                log.info("throttle: using redis backend")
                return backend
            except Exception as e:  # noqa: BLE001
                if choice == "redis":
                    log.error("throttle: redis backend unavailable (%s) — "
                              "falling back to sqlite", e)
                else:
                    log.warning("throttle: redis unavailable (%s) — using sqlite", e)
    if choice == "memory":
        log.warning("throttle: using per-process memory backend — the lockout "
                    "budget is multiplied by the number of replicas")
        return MemoryThrottle()
    log.info("throttle: using sqlite backend (shared across replicas)")
    return SqliteThrottle()


_backend: ThrottleBackend | None = None
_backend_lock = threading.Lock()


def backend() -> ThrottleBackend:
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = _build_backend()
    return _backend


def set_backend(b: ThrottleBackend | None) -> None:
    """Swap the backend (tests)."""
    global _backend
    with _backend_lock:
        _backend = b


# ---------------------------------------------------------------------------
# Public API used by auth.py / the login route
# ---------------------------------------------------------------------------
def make_key(username: str, client_ip: str = "") -> str:
    return f"{(username or '').strip().lower()}|{client_ip}"


def seconds_locked(key: str) -> float:
    """Seconds remaining on a lockout for *key*, or 0.0 if not locked."""
    _, until = backend().load(key)
    remaining = until - time.time()
    return remaining if remaining > 0 else 0.0


def record_failure(key: str) -> None:
    """Count a failed attempt; lock the key out once it exceeds the window.

    Backends that can do the whole read-modify-write atomically implement
    :meth:`ThrottleBackend.record_failure_atomic` and are used directly —
    otherwise two simultaneous failures can read the same count and one
    increment is lost, letting an attacker running parallel requests get
    slightly more attempts than the policy allows.
    """
    b = backend()
    atomic = getattr(b, "record_failure_atomic", None)
    if atomic is not None:
        locked = atomic(key)
        if locked:
            log.warning("auth: login locked out for %ss (key=%s)", LOCKOUT_SEC, key)
        return

    now = time.time()
    attempts, _ = b.load(key)
    attempts = [t for t in attempts if now - t < WINDOW_SEC]
    attempts.append(now)
    if len(attempts) >= MAX_ATTEMPTS:
        b.save(key, [], now + LOCKOUT_SEC)
        log.warning("auth: login locked out for %ss (key=%s)", LOCKOUT_SEC, key)
    else:
        b.save(key, attempts, 0.0)


def record_success(key: str) -> None:
    backend().clear(key)
