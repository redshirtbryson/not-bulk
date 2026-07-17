"""AC 15 SSRF suite — all offline. socket.getaddrinfo is monkeypatched to
return chosen IPs; httpx is driven by a MockTransport so no real network I/O
happens. Every allowlist-bypass shape and every private-IP family is rejected.
"""
from __future__ import annotations

import httpx
import pytest

from notbulk import fetcher
from notbulk.fetcher import FetchRejected

CFG = {
    "fetcher": {
        "allowed_hosts": ["i.imgur.com", "imgur.com", "api.imgur.com",
                          "i.redd.it", "www.reddit.com"],
        "max_bytes": 15728640,
        "timeout_seconds": 20,
    }
}


def _addrinfo(ip):
    """Build a getaddrinfo-shaped result for one IP (v4 or v6)."""
    import socket
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 0, 0, 0) if fam == socket.AF_INET6 else (ip, 0)
    return [(fam, socket.SOCK_STREAM, 6, "", sockaddr)]


# ---- resolve_public rejects every private/special family -----------------

@pytest.mark.parametrize("ip", [
    "10.0.0.5",          # RFC1918
    "192.168.1.9",       # RFC1918
    "172.16.0.1",        # RFC1918
    "127.0.0.1",         # loopback
    "169.254.169.254",   # link-local + cloud metadata
    "100.64.1.1",        # CGNAT 100.64/10
    "::1",               # IPv6 loopback
    "fc00::1",           # IPv6 ULA
    "fe80::1",           # IPv6 link-local
    "::ffff:10.0.0.1",   # IPv4-mapped private
])
def test_resolve_public_rejects_private(monkeypatch, ip):
    monkeypatch.setattr(fetcher.socket, "getaddrinfo", lambda *a, **k: _addrinfo(ip))
    with pytest.raises(FetchRejected):
        fetcher.resolve_public("i.imgur.com")


def test_resolve_public_rejects_mixed_public_and_private(monkeypatch):
    import socket
    def fake(*a, **k):
        return _addrinfo("1.1.1.1") + _addrinfo("10.0.0.1")
    monkeypatch.setattr(fetcher.socket, "getaddrinfo", fake)
    with pytest.raises(FetchRejected):
        fetcher.resolve_public("i.imgur.com")


def test_resolve_public_accepts_public(monkeypatch):
    monkeypatch.setattr(fetcher.socket, "getaddrinfo", lambda *a, **k: _addrinfo("1.1.1.1"))
    assert fetcher.resolve_public("i.imgur.com") == "1.1.1.1"


# ---- allowlist bypass shapes rejected BEFORE any resolution --------------

def test_reject_suffix_lookalike_before_resolve(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher.socket, "getaddrinfo",
                        lambda *a, **k: calls.append(a) or _addrinfo("1.1.1.1"))
    with pytest.raises(FetchRejected):
        fetcher.fetch_image("https://imgur.com.evil.io/x.png", CFG)


def test_reject_query_trick_before_resolve(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher.socket, "getaddrinfo",
                        lambda *a, **k: calls.append(a) or _addrinfo("1.1.1.1"))
    with pytest.raises(FetchRejected):
        fetcher.fetch_image("https://evil.com/?x=imgur.com", CFG)
    assert calls == []      # host was never resolved


def test_reject_userinfo_trick_before_resolve(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher.socket, "getaddrinfo",
                        lambda *a, **k: calls.append(a) or _addrinfo("1.1.1.1"))
    with pytest.raises(FetchRejected):
        fetcher.fetch_image("https://imgur.com@evil.com/x", CFG)
    assert calls == []      # userinfo is stripped; real host evil.com is not on the list


def test_reject_non_https(monkeypatch):
    with pytest.raises(FetchRejected):
        fetcher.fetch_image("http://i.imgur.com/x.png", CFG)


# ---- transport behavior via httpx.MockTransport --------------------------

def _fetch_with_transport(monkeypatch, url, transport):
    monkeypatch.setattr(fetcher.socket, "getaddrinfo", lambda *a, **k: _addrinfo("1.1.1.1"))
    monkeypatch.setattr(fetcher, "_build_client",
                        lambda cfg: httpx.Client(transport=transport,
                                                 follow_redirects=False))
    return fetcher.fetch_image(url, CFG)


def test_redirect_rejected(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://evil.com/x"})
    transport = httpx.MockTransport(handler)
    with pytest.raises(FetchRejected, match="redirect"):
        _fetch_with_transport(monkeypatch, "https://i.imgur.com/x.png", transport)


def test_non_image_content_type_rejected(monkeypatch):
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>")
    transport = httpx.MockTransport(handler)
    with pytest.raises(FetchRejected, match="content-type"):
        _fetch_with_transport(monkeypatch, "https://i.imgur.com/x.png", transport)


def test_oversize_stream_aborted(monkeypatch):
    big = b"\xff\xd8\xff" + b"\x00" * (CFG["fetcher"]["max_bytes"] + 10)
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=big)
    transport = httpx.MockTransport(handler)
    with pytest.raises(FetchRejected, match="too large"):
        _fetch_with_transport(monkeypatch, "https://i.imgur.com/x.png", transport)


def test_valid_image_returns_bytes(monkeypatch):
    body = b"\xff\xd8\xff" + b"jpegdata"
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=body)
    transport = httpx.MockTransport(handler)
    out = _fetch_with_transport(monkeypatch, "https://i.imgur.com/x.png", transport)
    assert out == body


# ---- enumeration re-gates each discovered URL ----------------------------

def test_enumerate_imgur_regates_external_entries(monkeypatch):
    monkeypatch.setattr(fetcher.socket, "getaddrinfo", lambda *a, **k: _addrinfo("1.1.1.1"))
    album_json = {"data": {"images": [
        {"link": "https://i.imgur.com/a1.png"},
        {"link": "https://evil.com/a2.png"},      # must be dropped on re-check
    ]}}
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "application/json"},
                              json=album_json)
    monkeypatch.setenv("IMGUR_CLIENT_ID", "test-client")
    monkeypatch.setattr(fetcher, "_build_client",
                        lambda cfg: httpx.Client(
                            transport=httpx.MockTransport(handler), follow_redirects=False))
    urls = fetcher.enumerate_imgur("https://imgur.com/a/abc123", CFG)
    assert urls == ["https://i.imgur.com/a1.png"]   # external entry re-gated out


def test_enumerate_imgur_without_client_id_raises(monkeypatch):
    monkeypatch.delenv("IMGUR_CLIENT_ID", raising=False)
    with pytest.raises(FetchRejected, match="imgur api unavailable"):
        fetcher.enumerate_imgur("https://imgur.com/a/abc123", CFG)
