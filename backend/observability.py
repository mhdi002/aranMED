"""Request correlation, log formatting and metrics.

* Every request gets an id: the caller's ``X-Request-ID`` when it is a sane
  token, otherwise a fresh one. It is echoed in the response header, added to
  every log record written while serving the request (``%(request_id)s``)
  and used as the error id of unhandled exceptions, so a user's error report,
  the gateway log and the API log line up.
* ``LOG_FORMAT=json`` writes one JSON object per log line for log shippers.
* ``/api/metrics`` serves Prometheus metrics (request count and latency per
  route template and status) when ``prometheus_client`` is installed and
  ``METRICS_TOKEN`` is set; scrapers send ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import contextvars
import hmac
import json
import logging
import os
import re
import time
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import Response


request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._\-]{6,64}$")


def current_request_id() -> str:
    return request_id_var.get()


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Root logging with the request id on every record."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    if os.getenv("LOG_FORMAT", "").strip().lower() == "json":
        fmt: logging.Formatter = JsonFormatter()
    else:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s [%(request_id)s]: %(message)s")
    for h in root.handlers:
        h.setFormatter(fmt)
        if not any(isinstance(f, _RequestIdFilter) for f in h.filters):
            h.addFilter(_RequestIdFilter())


# ---------------------------------------------------------------- metrics
try:  # optional dependency
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
    _REQUESTS = Counter("aranmed_http_requests_total", "HTTP requests",
                        ["method", "route", "status"])
    _LATENCY = Histogram("aranmed_http_request_seconds", "HTTP request latency",
                         ["method", "route"],
                         buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))
except Exception:  # noqa: BLE001
    _REQUESTS = _LATENCY = None


def _route_template(request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


def install(app) -> None:
    """Add the request-id/metrics middleware and the metrics endpoint."""

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            if _REQUESTS is not None:
                route = _route_template(request)
                _REQUESTS.labels(request.method, route, str(status)).inc()
                _LATENCY.labels(request.method, route).observe(time.perf_counter() - start)
            request_id_var.reset(token)

    @app.get("/api/metrics", include_in_schema=False)
    def metrics(request: Request) -> Response:
        expected = os.getenv("METRICS_TOKEN", "")
        if not expected or _REQUESTS is None:
            raise HTTPException(404, "metrics are not enabled")
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, expected):
            raise HTTPException(401, "metrics token required")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def error_id(fallback_len: int = 12) -> str:
    rid = request_id_var.get()
    return rid if rid != "-" else uuid.uuid4().hex[:fallback_len]


