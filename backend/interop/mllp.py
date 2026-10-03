"""MLLP (Minimal Lower Layer Protocol) transport for HL7 v2.

Frame: ``<VT> message <FS><CR>`` (0x0B … 0x1C 0x0D). The server accepts any
number of messages per connection and answers each with the ACK/response
produced by :mod:`interop.hl7_handlers`. It runs its own asyncio loop in a
background thread, so it works both inside the API process and in the
standalone ``clinical_listeners`` service.

Settings: ``HL7_MLLP_ENABLED``, ``HL7_MLLP_BIND``, ``HL7_MLLP_PORT``,
``HL7_MLLP_ALLOWED_PEERS`` (comma-separated source IPs; empty = any),
``HL7_MLLP_MAX_BYTES``. Optional TLS via ``HL7_MLLP_TLS_CERT``/``_KEY``.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import threading
from typing import Optional

from clinicaldb import settings

log = logging.getLogger("interop.mllp")

VT, FS, CR = b"\x0b", b"\x1c", b"\x0d"


def frame(message: str) -> bytes:
    return VT + message.encode("utf-8") + FS + CR


class MllpServer:
    def __init__(self, *, bind: Optional[str] = None, port: Optional[int] = None) -> None:
        self.bind = bind or settings.env("HL7_MLLP_BIND", "0.0.0.0")
        self.port = port if port is not None else settings.env_int("HL7_MLLP_PORT", 2575)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self.max_bytes = settings.env_int("HL7_MLLP_MAX_BYTES", 16 * 1024 * 1024)
        allowed = settings.env("HL7_MLLP_ALLOWED_PEERS", "")
        self.allowed = {a.strip() for a in allowed.split(",") if a.strip()}

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from interop import hl7_handlers
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else "?"
        if self.allowed and peer_ip not in self.allowed:
            log.warning("MLLP connection from %s refused (not in HL7_MLLP_ALLOWED_PEERS)", peer_ip)
            writer.close()
            return
        buf = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > self.max_bytes:
                    log.warning("MLLP message from %s exceeds limit; closing", peer_ip)
                    break
                while True:
                    start = buf.find(VT)
                    end = buf.find(FS + CR, start + 1) if start >= 0 else -1
                    if start < 0 or end < 0:
                        break
                    raw = buf[start + 1:end].decode("utf-8", errors="replace")
                    buf = buf[end + 2:]
                    resp = await asyncio.get_running_loop().run_in_executor(
                        None, lambda r=raw: hl7_handlers.process(r, peer=f"mllp:{peer_ip}"))
                    writer.write(frame(resp))
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    def _ssl(self) -> Optional[ssl.SSLContext]:
        cert, key = settings.env("HL7_MLLP_TLS_CERT", ""), settings.env("HL7_MLLP_TLS_KEY", "")
        if not (cert and key):
            return None
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert, key)
        return ctx

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def start():
            self._server = await asyncio.start_server(self._handle, self.bind, self.port,
                                                      ssl=self._ssl())
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()
        self._loop.run_until_complete(start())
        log.info("HL7 MLLP listening on %s:%d", self.bind, self.port)
        self._loop.run_forever()

    def start(self) -> "MllpServer":
        self._thread = threading.Thread(target=self._run, name="hl7-mllp", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("MLLP server failed to start")
        return self

    def stop(self) -> None:
        if self._loop and self._server:
            async def close():
                self._server.close()
                await self._server.wait_closed()
            fut = asyncio.run_coroutine_threadsafe(close(), self._loop)
            try:
                fut.result(5)
            except Exception:  # noqa: BLE001
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)
        self._server = None


def send(host: str, port: int, message: str, *, timeout: float = 30.0,
         use_tls: bool = False) -> str:
    """Send one message over MLLP and return the decoded response."""
    from clinicaldb import messages
    sock = socket.create_connection((host, port), timeout=timeout)
    if use_tls:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    try:
        sock.sendall(frame(message))
        buf = b""
        while FS + CR not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    start = buf.find(VT)
    end = buf.find(FS + CR)
    resp = buf[start + 1:end].decode("utf-8", errors="replace") if start >= 0 and end > start else ""
    status = "ok" if "MSA|AA" in resp else "error"
    messages.log(direction="out", protocol="hl7v2", message_type=message.split("|")[8] if message.count("|") > 8 else None,
                 peer=f"mllp:{host}:{port}", status=status, payload=message, response=resp)
    return resp


_server: Optional[MllpServer] = None


def start_from_config() -> Optional[MllpServer]:
    global _server
    if not settings.env_bool("HL7_MLLP_ENABLED", False) or _server is not None:
        return _server
    _server = MllpServer().start()
    return _server


def stop() -> None:
    global _server
    if _server:
        _server.stop()
        _server = None


def status() -> dict:
    return {"enabled": settings.env_bool("HL7_MLLP_ENABLED", False),
            "running": _server is not None,
            "port": _server.port if _server else settings.env_int("HL7_MLLP_PORT", 2575)}
