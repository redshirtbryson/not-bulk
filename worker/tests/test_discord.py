"""Unit tests for the sanitized Discord notifier.

No network: httpx is monkeypatched with a capture-only stub. The webhook URL is
supplied via the DISCORD_WEBHOOK_URL env var (monkeypatched), never printed.
"""
from __future__ import annotations

import logging

import pytest

from notbulk import discord


class _CaptureClient:
    """httpx.Client stand-in: records the single POST and returns a 204-like resp."""

    posted: list[tuple[str, dict, float | None]] = []

    def __init__(self, *, timeout=None):
        self._timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None):
        _CaptureClient.posted.append((url, json, self._timeout))
        return type("Resp", (), {"status_code": 204, "raise_for_status": lambda self=None: None})()


class _RaisingClient:
    """httpx.Client stand-in whose post() raises (dead webhook / network down)."""

    def __init__(self, *, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None):
        raise RuntimeError("connection refused")


ENABLED_CFG = {"discord": {"enabled": True, "timeout_seconds": 5}}
DISABLED_CFG = {"discord": {"enabled": False, "timeout_seconds": 5}}
WEBHOOK = "https://discord.com/api/webhooks/123/abcSECRETtoken"


@pytest.fixture(autouse=True)
def _reset_capture_and_warn_flag(monkeypatch):
    _CaptureClient.posted = []
    # Reset the module-level "warned once" flag so each test starts clean.
    monkeypatch.setattr(discord, "_warned_no_webhook", False, raising=False)
    yield


def test_enabled_and_env_set_posts_embed(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _CaptureClient)
    discord.notify(ENABLED_CFG, "error", "pipeline job failed",
                   {"type": "identify", "job_id": "j1", "error_class": "RuntimeError"})
    assert len(_CaptureClient.posted) == 1
    url, body, timeout = _CaptureClient.posted[0]
    assert url == WEBHOOK
    assert timeout == 5
    embed = body["embeds"][0]
    assert embed["title"] == "pipeline job failed"
    assert embed["color"] == discord._COLOR["error"]
    names = {f["name"]: f["value"] for f in embed["fields"]}
    assert names == {"type": "identify", "job_id": "j1", "error_class": "RuntimeError"}


def test_field_values_are_sanitized_and_truncated(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _CaptureClient)
    long = "x" * 5000
    discord.notify(ENABLED_CFG, "info", "batch complete",
                   {"batch_id": "  b1  ", "note": long, "count": 7})
    embed = _CaptureClient.posted[0][1]["embeds"][0]
    vals = {f["name"]: f["value"] for f in embed["fields"]}
    assert vals["batch_id"] == "b1"          # str + strip
    assert vals["count"] == "7"              # coerced to str
    assert len(vals["note"]) == 1000         # truncated to 1000 chars


def test_disabled_config_never_posts(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _CaptureClient)
    discord.notify(DISABLED_CFG, "error", "t", {"a": "b"})
    assert _CaptureClient.posted == []


def test_env_unset_never_posts_and_warns_once(monkeypatch, caplog):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(discord.httpx, "Client", _CaptureClient)
    with caplog.at_level(logging.WARNING):
        discord.notify(ENABLED_CFG, "error", "t", {"a": "b"})
        discord.notify(ENABLED_CFG, "error", "t", {"a": "b"})
    assert _CaptureClient.posted == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1                # warned ONCE, not per call


def test_post_exception_is_swallowed(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _RaisingClient)
    # Must NOT raise — a dead webhook can never crash the worker.
    discord.notify(ENABLED_CFG, "error", "t", {"a": "b"})


def test_webhook_url_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _RaisingClient)
    with caplog.at_level(logging.WARNING):
        discord.notify(ENABLED_CFG, "error", "t", {"a": "b"})
    assert WEBHOOK not in caplog.text
    assert "abcSECRETtoken" not in caplog.text
