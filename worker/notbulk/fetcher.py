"""SSRF-hardened URL fetcher (design/AC 15). Runs ONLY in the worker.

Every control from the M2 Global Constraints is implemented here:
  - exact-hostname allowlist (lowercased, exact match — no suffix/substring),
  - https only, no userinfo, single DNS resolution,
  - ALL A/AAAA records must be public; ANY private/loopback/link-local/CGNAT/
    metadata/ULA/IPv6-link-local/IPv4-mapped-private record -> reject,
  - connection pinned to the one verified public IP (URL rewritten to the IP,
    Host header + TLS SNI restored to the real hostname),
  - redirects disabled; any 3xx -> reject,
  - streamed with a byte cap; timeout; image Content-Type only,
  - Imgur/Reddit enumeration re-runs the FULL gate on every discovered URL.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

import httpx

_USER_AGENT = "notbulk/0.3 (card scanner; contact via github)"
_IMGUR_API = "https://api.imgur.com/3/album/{album_id}"


class FetchRejected(Exception):
    """Permanent rejection — the fetch handler must NOT retry."""


def _is_public(ip_str: str) -> bool:
    """True only for a genuinely routable public address."""
    ip = ipaddress.ip_address(ip_str)
    # Unwrap IPv4-mapped IPv6 (::ffff:a.b.c.d) to judge the embedded v4 address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return False
    # CGNAT 100.64.0.0/10 is not flagged by is_private on all Python versions.
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return False
    return True


def resolve_public(host: str) -> str:
    """Resolve host, require EVERY A/AAAA record to be public, return one IP."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchRejected(f"dns resolution failed for {host}") from exc
    ips = []
    for info in infos:
        ip = info[4][0]
        if not _is_public(ip):
            raise FetchRejected(f"non-public address for {host}: {ip}")
        ips.append(ip)
    if not ips:
        raise FetchRejected(f"no addresses for {host}")
    return ips[0]


def _check_allowlist(url: str, cfg: dict) -> tuple[str, str]:
    """Return (host, path) after validating scheme/host/userinfo. Raises before
    any DNS resolution on a bad URL."""
    allowed = {h.lower() for h in cfg["fetcher"]["allowed_hosts"]}
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise FetchRejected("scheme must be https")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise FetchRejected("userinfo not allowed in url")
    host = (parsed.hostname or "").lower()
    if host not in allowed:            # EXACT match only
        raise FetchRejected(f"host not on allowlist: {host!r}")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return host, path


def _build_client(cfg: dict) -> httpx.Client:
    """Build the httpx client used for all fetches: redirects disabled,
    verify=True, timeout from cfg.

    IP pinning itself is NOT done here — callers rewrite the request URL to
    https://{ip}{path}, set the Host header to the real hostname, and pass
    extensions={'sni_hostname': host} per-request so the TLS handshake uses the
    correct SNI and the cert is verified against the real host.
    """
    return httpx.Client(
        follow_redirects=False,
        timeout=cfg["fetcher"]["timeout_seconds"],
        headers={"User-Agent": _USER_AGENT},
        verify=True,
    )


def fetch_image(url: str, cfg: dict) -> bytes:
    host, path = _check_allowlist(url, cfg)     # rejects bad shapes pre-DNS
    ip = resolve_public(host)                   # single resolution, all-public
    max_bytes = int(cfg["fetcher"]["max_bytes"])

    # Pin to the verified IP; restore Host header + TLS SNI to the real host.
    literal = f"[{ip}]" if ":" in ip else ip
    pinned_url = f"https://{literal}{path}"
    client = _build_client(cfg)
    try:
        with client.stream(
            "GET", pinned_url,
            headers={"Host": host},
            extensions={"sni_hostname": host},
        ) as resp:
            if 300 <= resp.status_code < 400:
                raise FetchRejected(f"redirect not allowed ({resp.status_code})")
            if resp.status_code != 200:
                raise FetchRejected(f"unexpected status {resp.status_code}")
            ctype = resp.headers.get("Content-Type", "")
            if not ctype.lower().startswith("image/"):
                raise FetchRejected(f"content-type not image/*: {ctype!r}")
            chunks = bytearray()
            for chunk in resp.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise FetchRejected("response too large")
            return bytes(chunks)
    finally:
        client.close()


def _regate(urls: list[str], cfg: dict) -> list[str]:
    """Keep only URLs that pass the full allowlist check (re-gate enumeration
    results). Resolution/fetch happens later per-URL in the fetch handler."""
    kept = []
    for u in urls:
        try:
            _check_allowlist(u, cfg)
        except FetchRejected:
            continue
        kept.append(u)
    return kept


def _album_id_from(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    # imgur.com/a/<id> or imgur.com/gallery/<id>
    return parts[-1] if parts else ""


def enumerate_imgur(url: str, cfg: dict) -> list[str]:
    """List direct image URLs from an Imgur album/gallery via the API.

    Requires IMGUR_CLIENT_ID (M2 dev works with direct i.imgur.com links; the
    album API is optional). Each discovered link is re-gated through the
    allowlist, so only i.imgur.com results survive.
    """
    client_id = os.environ.get("IMGUR_CLIENT_ID")
    if not client_id:
        raise FetchRejected("imgur api unavailable (IMGUR_CLIENT_ID unset)")
    album_id = _album_id_from(url)
    api_host = "api.imgur.com"
    ip = resolve_public(api_host)
    client = _build_client(cfg)
    literal = f"[{ip}]" if ":" in ip else ip
    try:
        resp = client.get(
            f"https://{literal}/3/album/{album_id}",
            headers={"Host": api_host, "Authorization": f"Client-ID {client_id}"},
            extensions={"sni_hostname": api_host},
        )
    finally:
        client.close()
    data = resp.json().get("data", {})
    images = data.get("images", []) if isinstance(data, dict) else []
    links = [img.get("link") for img in images if img.get("link")]
    return _regate(links, cfg)


def enumerate_reddit(url: str, cfg: dict) -> list[str]:
    """List direct image URLs from a Reddit post JSON. Re-gated; only i.redd.it
    (and i.imgur.com) results survive."""
    json_url = url.rstrip("/") + ".json"
    host, path = _check_allowlist(json_url, cfg)
    ip = resolve_public(host)
    client = _build_client(cfg)
    literal = f"[{ip}]" if ":" in ip else ip
    try:
        resp = client.get(
            f"https://{literal}{path}",
            headers={"Host": host},
            extensions={"sni_hostname": host},
        )
    finally:
        client.close()
    payload = resp.json()
    links: list[str] = []
    # Reddit listing: payload[0].data.children[].data.url
    try:
        children = payload[0]["data"]["children"]
    except (KeyError, IndexError, TypeError):
        children = []
    for child in children:
        link = child.get("data", {}).get("url")
        if link:
            links.append(link)
    return _regate(links, cfg)
