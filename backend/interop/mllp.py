"""MLLP (Minimal Lower Layer Protocol) transport for HL7 v2.

Frame: ``<VT> message <FS><CR>`` (0x0B … 0x1C 0x0D). The server accepts any
number of messages per connection and answers each with the ACK/response
produced by :mod:`interop.hl7_handlers`. It runs its own asyncio loop in a
background thread, so it works both inside the API process and in the
standalone ``clinical_listeners`` service.

Settings: ``HL7_MLLP_ENABLED``, ``HL7_MLLP_BIND`` (default 127.0.0.1),
``HL7_MLLP_PORT``, ``HL7_MLLP_ALLOWED_PEERS`` (comma-separated source IPs),
``HL7_MLLP_MAX_BYTES``, ``HL7_MLLP_IDLE_SEC`` (idle connection timeout).
Optional TLS via ``HL7_MLLP_TLS_CERT``/``_KEY``.

HL7 v2 carries no credentials, so a listener on a non-loopback address must
be restricted: by a peer allowlist, by TLS, or by an explicit
``HL7_MLLP_ALLOW_ANY_PEER=true`` (e.g. inside a container whose published
port is already restricted). Otherwise it refuses to start.
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



# MSH-18 character set names (HL7 v2 table 0211) -> Python codecs.
_HL7_CHARSETS = {"UNICODE UTF-8": "utf-8", "UNICODE": "utf-8", "UTF-8": "utf-8",
                 "ASCII": "ascii", "8859/1": "iso8859-1", "8859/2": "iso8859-2",
                 "8859/5": "iso8859-5", "8859/6": "iso8859-6", "8859/7": "iso8859-7",
                 "8859/8": "iso8859-8", "8859/9": "iso8859-9", "8859/15": "iso8859-15",
                 "WINDOWS-1256": "cp1256", "CP1256": "cp1256"}


def decode_hl7(data: bytes) -> str:
    """Decode an HL7 v2 message using its declared MSH-18 character set,
    falling back to UTF-8 and then the legacy Persian/Arabic code page."""
    declared = None
    try:
        head = data.split(b"\r", 1)[0].decode("ascii", errors="replace")
        if head.startswith("MSH") and len(head) > 4:
            fields = head.split(head[3])
            if len(fields) > 17 and fields[17].strip():
                declared = _HL7_CHARSETS.get(fields[17].strip().split(head[4] if len(head) > 4 else "^")[0].upper())
    except Exception:  # noqa: BLE001
        declared = None
    for enc in [e for e in (declared, "utf-8", "cp1256") if e]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")

def frame(message: str) -> bytes:
    return VT + message.encode("utf-8") + FS + CR


class MllpServer:
    def __init__(self, *, bind: Optional[str] = None, port: Optional[int] = None) -> None:
        self.bind = bind or settings.env("HL7_MLLP_BIND", "127.0.0.1")
        self.port = port if port is not None else settings.env_int("HL7_MLLP_PORT", 2575)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self.max_bytes = settings.env_int("HL7_MLLP_MAX_BYTES", 16 * 1024 * 1024)
        allowed = settings.env("HL7_MLLP_ALLOWED_PEERS", "")
        self.allowed = {a.strip() for a in allowed.split(",") if a.strip()}
        self.idle_sec = float(settings.env("HL7_MLLP_IDLE_SEC", "60") or 60)
        self._check_exposure()

    def _check_exposure(self) -> None:
        loopback = self.bind in ("127.0.0.1", "::1", "localhost")
        any_ok = settings.env("HL7_MLLP_ALLOW_ANY_PEER", "").strip().lower() in ("1", "true", "yes")
        if loopback or self.allowed or any_ok or self._ssl() is not None:
            return
        raise RuntimeError(
            f"refusing to expose HL7 MLLP on {self.bind}:{self.port} without access control: "
            "set HL7_MLLP_ALLOWED_PEERS, enable TLS (HL7_MLLP_TLS_CERT/_KEY), or set "
            "HL7_MLLP_ALLOW_ANY_PEER=true if the network already restricts who can connect")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from interop import hl7_handlers
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else "?"
        if self.allowed and peer_ip not in self.allowed:
            log.warning("MLLP connection from %s refused (not in HL7_MLLP_ALLOWED_PEERS)", peer_ip)
            writer.close()
            return
        buf = bytearray()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=self.idle_sec)
                except asyncio.TimeoutError:
                    log.info("MLLP connection from %s idle for %.0fs; closing", peer_ip, self.idle_sec)
                    break
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
                    raw = decode_hl7(bytes(buf[start + 1:end]))
                    del buf[:end + 2]
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
    resp = decode_hl7(bytes(buf[start + 1:end])) if start >= 0 and end > start else ""
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
