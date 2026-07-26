"""SSRF guard (Stage 4) — refuse fetches to internal/metadata addresses.

Any server-side "fetch this URL the user gave us" feature (for example,
job-description-by-URL) is an SSRF vector: a user could point it at
``http://169.254.169.254/`` (cloud metadata), ``http://localhost:5567/`` (the
app's own admin surface), or an RFC-1918 host on the deploy's private network.

``safe_fetch_url`` validates a URL before any request is made:

  * scheme must be http/https
  * the hostname must resolve ONLY to public, routable addresses — every
    resolved IP is checked, so a hostname that resolves to a loopback /
    link-local / private / reserved / metadata address is rejected.

It deliberately does NOT follow redirects for the caller — callers must
disable redirect-following (or re-validate each hop), because a public URL
can 302 to ``http://169.254.169.254/``.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class SSRFBlocked(ValueError):
    """Raised when a URL targets a non-public / disallowed address."""


class HTTPStatusBlocked(SSRFBlocked):
    """Raised when a safe URL returns a non-success HTTP status."""


_ALLOWED_SCHEMES = {"http", "https"}

# The cloud metadata endpoints (link-local 169.254.169.254 is already caught
# by is_link_local, but we name it for clarity and to also catch the GCP
# alias hostname resolution).
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata"}

# Deprecated IPv6 site-local prefix (RFC 3879). ``ipaddress`` reports it as
# is_global=True with every other flag False, so it slips past the flag-based
# checks below — we reject it explicitly. (RFC-6598 CGNAT 100.64.0.0/10 is, by
# contrast, already caught for IPv4 via ``not is_global``.)
_IPV6_SITE_LOCAL = ipaddress.ip_network("fec0::/10")


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    """True only for genuinely routable public addresses.

    For IPv4 we trust ``ip.is_global`` as the single source of truth: it is
    False for every non-routable block — private (RFC-1918), loopback,
    link-local, AND the RFC-6598 CGNAT range ``100.64.0.0/10`` that the
    individual flags miss. For IPv6 we keep the explicit flag checks (its
    ``is_global`` does not cover the deprecated ``fec0::/10`` site-local block)
    and reject ``fec0::/10`` by hand. IPv4-mapped / 6to4 / Teredo v6 hosts that
    embed a private v4 are recursed into so they can't smuggle one through.
    """
    # IPv4-mapped / 6to4 / Teredo can smuggle a private v4 inside a v6 host —
    # validate the embedded v4 first (handles ::ffff:100.64.0.1 etc.).
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and not _ip_is_public(mapped):
        return False

    if isinstance(ip, ipaddress.IPv4Address):
        # is_global is False for private/loopback/link-local/reserved AND the
        # RFC-6598 CGNAT 100.64.0.0/10 range — the robust single check.
        return ip.is_global

    # IPv6: flag checks + explicit deprecated site-local (fec0::/10) reject.
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or ip in _IPV6_SITE_LOCAL
    ):
        return False
    return True


def _validate_host(host: str) -> list[str]:
    """Resolve ``host`` and return its validated public IPs (or raise).

    A bare IP literal is checked in place and returned as the single pinned
    address. A hostname is resolved and EVERY resolved A/AAAA must be public —
    one private answer rejects the whole URL. The returned list is what the
    fetcher connects to directly, so the IP we validate is the IP we hit (no
    second DNS lookup → no DNS-rebinding TOCTOU window).
    """
    if host.lower() in _METADATA_HOSTS:
        raise SSRFBlocked("That host isn't allowed.")

    # A bare IP literal: check it directly (covers IPv6 in brackets too).
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not _ip_is_public(ip):
            raise SSRFBlocked("That URL isn't allowed (non-public address).")
        return [host]

    # A hostname: resolve EVERY address it maps to and require all public.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SSRFBlocked(f"Could not resolve host {host!r}.") from exc
    resolved = {info[4][0] for info in infos}
    if not resolved:
        raise SSRFBlocked(f"Host {host!r} did not resolve.")
    for addr in resolved:
        try:
            if not _ip_is_public(ipaddress.ip_address(addr)):
                raise SSRFBlocked("That URL isn't allowed (resolves to a private address).")
        except ValueError as exc:
            raise SSRFBlocked(f"Unparseable resolved address {addr!r}.") from exc
    return sorted(resolved)


def assert_safe_url(url: str) -> str:
    """Validate ``url`` for SSRF; return its hostname or raise ``SSRFBlocked``.

    Resolves the host and rejects the URL if ANY resolved address is
    non-public. Use before fetching; fetch with redirects disabled.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFBlocked(f"URL scheme must be http/https, got {parsed.scheme!r}.")
    host = parsed.hostname
    if not host:
        raise SSRFBlocked("URL has no host.")
    _validate_host(host)
    return host


def _read_capped(resp, max_bytes: int) -> bytes:
    """Read a bounded response and reject bodies larger than ``max_bytes``."""
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SSRFBlocked("Response body is too large.")
    return data


def _https_connection_to_pinned_ip(pin: str, port: int, *, timeout: float, context, server_name: str):
    """Build an HTTPSConnection that connects to ``pin`` but uses host SNI."""
    import http.client

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        def __init__(self, host, port=None, **kwargs):
            self._onepager_server_name = kwargs.pop("onepager_server_name")
            super().__init__(host, port=port, **kwargs)

        def connect(self):  # noqa: D401 - mirrors http.client internals
            sock = self._create_connection((self.host, self.port), self.timeout, self.source_address)
            self.sock = self._context.wrap_socket(sock, server_hostname=self._onepager_server_name)

    return _PinnedHTTPSConnection(
        pin,
        port,
        timeout=timeout,
        context=context,
        onepager_server_name=server_name,
    )


def safe_fetch_url(url: str, *, timeout: float = 8.0, max_bytes: int = 64 * 1024) -> bytes:
    """SSRF-guarded GET. Returns up to ``max_bytes`` of the body.

    Hardened against DNS-rebinding / redirect TOCTOU:

      * the host is resolved ONCE and every resolved IP is validated as public;
      * the request connects to a VALIDATED/PINNED IP (not a second DNS lookup),
        so the IP we checked is the IP we hit — a rebind between check and fetch
        can't redirect us to an internal address;
      * the original Host header is preserved (and SNI for TLS), so virtual
        hosting / certificate validation still work;
      * redirects are NOT followed — a 3xx is returned as-is and the caller gets
        a clear failure rather than us chasing a Location into the internal
        network;

    Raises ``SSRFBlocked`` on a disallowed target and ``OSError`` on transport
    failure — callers surface these as a clear 4xx.
    """
    import http.client
    import ssl

    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFBlocked(f"URL scheme must be http/https, got {parsed.scheme!r}.")
    host = parsed.hostname
    if not host:
        raise SSRFBlocked("URL has no host.")
    try:
        port = parsed.port
    except ValueError as exc:  # urlparse raises for non-numeric/out-of-range ports
        # Fail fast, BEFORE any DNS resolution below.
        raise SSRFBlocked("URL has an invalid port.") from exc

    # Resolve + validate, then pin the first validated IP for the connection.
    pinned_ips = _validate_host(host)
    pin = pinned_ips[0]

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    headers = {"User-Agent": "onepager/1.0", "Host": host, "Accept": "*/*"}

    if parsed.scheme == "https":
        ctx = ssl.create_default_context()
        # Connect to the validated IP; keep ``host`` as server_hostname so SNI
        # + certificate validation use the real name, not the raw IP.
        conn = _https_connection_to_pinned_ip(
            pin, port or 443, timeout=timeout, context=ctx, server_name=host
        )
    else:
        conn = http.client.HTTPConnection(pin, port or 80, timeout=timeout)

    try:
        # Explicitly do NOT follow redirects: http.client never does on its own.
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        if resp.status < 200 or resp.status >= 300:
            raise HTTPStatusBlocked(f"URL returned HTTP {resp.status}.")
        return _read_capped(resp, max_bytes)
    finally:
        conn.close()
