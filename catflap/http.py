"""Minimal JSON-over-HTTPS client (stdlib only) with certificate pinning.

Gateways usually present self-signed certificates. Instead of "verify=False",
catflap pins the certificate's SHA-256 fingerprint (trust on first use, shown
to the user to confirm) or uses a CA file. ``insecure`` exists but is opt-in.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit


class HttpError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


class TlsPinError(RuntimeError):
    pass


def adguard_message(host: str) -> str:
    """Why a gateway connection fails while AdGuard's HTTPS filtering re-signs it."""
    import sys
    how = ("In AdGuard, turn off HTTPS filtering for CatFlap (catflap.exe), or add "
           f"{host} to its HTTPS filtering exclusions."
           if sys.platform == "win32" else
           "Exclude CatFlap from HTTPS filtering: CatFlap's AdGuard menu → Check HTTPS "
           "filtering…, or run: catflap adguard exclude 'python3*'")
    return (f"AdGuard's HTTPS filtering is intercepting the connection to {host}, so CatFlap "
            f"can't check that it's really your gateway and won't send your API key. {how}")


def _adguard_der(host: str, port: int, timeout: float) -> bytes | None:
    """The certificate we're shown for host:port, if AdGuard issued it."""
    from .adguard import intercepted_by_adguard
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as s, \
                ctx.wrap_socket(s, server_hostname=host) as ts:
            der = ts.getpeercert(binary_form=True)
    except OSError:
        return None
    return der if intercepted_by_adguard(der) else None


def _norm_fp(fp: str) -> str:
    return fp.replace(":", "").strip().lower()


def fetch_fingerprint(base_url: str, timeout: float = 10) -> str:
    """Return the SHA-256 fingerprint of the server certificate (for TOFU)."""
    u = urlsplit(base_url)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((u.hostname, u.port or 443), timeout=timeout) as s:
        with ctx.wrap_socket(s, server_hostname=u.hostname) as ts:
            der = ts.getpeercert(binary_form=True)
    from .adguard import intercepted_by_adguard
    if intercepted_by_adguard(der):
        # Never pin AdGuard's stand-in certificate as the gateway's.
        raise TlsPinError(adguard_message(u.hostname or ""))
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


@dataclass
class JsonClient:
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    fingerprint: str | None = None
    ca_file: str | None = None
    insecure: bool = False
    timeout: float = 15
    cookies: dict[str, str] = field(default_factory=dict)

    def _context(self) -> ssl.SSLContext:
        if self.fingerprint or self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context(cafile=self.ca_file)

    def request(self, method: str, path: str, body: Any = None,
                params: dict[str, Any] | None = None,
                extra_headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
        u = urlsplit(self.base_url)
        full = u.path.rstrip("/") + path
        if params:
            full += "?" + urlencode(params)
        if u.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                u.hostname, u.port or 443, timeout=self.timeout, context=self._context())
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=self.timeout)
        try:
            try:
                conn.connect()
            except ssl.SSLCertVerificationError:
                if _adguard_der(u.hostname or "", u.port or 443, self.timeout):
                    raise TlsPinError(adguard_message(u.hostname or "")) from None
                raise
            if self.fingerprint and isinstance(conn, http.client.HTTPSConnection):
                der = conn.sock.getpeercert(binary_form=True)  # type: ignore[union-attr]
                got = hashlib.sha256(der).hexdigest()
                if got != _norm_fp(self.fingerprint):
                    from .adguard import intercepted_by_adguard
                    if intercepted_by_adguard(der):
                        raise TlsPinError(adguard_message(u.hostname or ""))
                    raise TlsPinError(
                        "gateway TLS certificate changed (fingerprint mismatch). If you replaced "
                        "the certificate, re-run 'catflap gateway add --tofu' to re-pin it.")
            hdrs = {"Accept": "application/json", **self.headers, **(extra_headers or {})}
            if self.cookies:
                hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                hdrs["Content-Type"] = "application/json"
            conn.request(method, full, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            rh = {k.lower(): v for k, v in resp.getheaders()}
            for c in resp.headers.get_all("Set-Cookie") or []:
                k, _, rest = c.partition("=")
                self.cookies[k.strip()] = rest.split(";", 1)[0]
        finally:
            conn.close()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode(errors="replace")
        if resp.status >= 400:
            msg = parsed.get("message") if isinstance(parsed, dict) else None
            if isinstance(parsed, dict) and not msg:
                msg = (parsed.get("meta") or {}).get("msg")
            raise HttpError(resp.status, str(msg or resp.reason), parsed)
        return resp.status, rh, parsed

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)[2]

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, body=body, **kw)[2]
