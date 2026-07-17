"""Sanitized Discord webhook notifier (design S10).

A single entry point `notify(cfg, level, title, fields)`:
  * No-op when the config toggle `discord.enabled` is false OR when the
    `DISCORD_WEBHOOK_URL` env var is unset (warns ONCE via a module flag so a
    disabled/unprovisioned deployment does not spam the log).
  * Otherwise POSTs a minimal Discord embed to the webhook URL via httpx, with a
    timeout of `cfg['discord']['timeout_seconds']`.
  * Every field VALUE is coerced to str, stripped, and truncated to 1000 chars —
    callers pass an error CLASS name + ids, never a raw traceback with
    interpolated user content (filenames, OCR text).
  * ALL exceptions (httpx/network/HTTP) are swallowed and logged at WARNING
    level: a dead or slow webhook must NEVER crash or stall the worker.
  * The webhook URL is NEVER logged, printed, or returned (secret hygiene).
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("notbulk.discord")

# Discord embed sidebar colors (decimal int): red for errors, green for info.
_COLOR = {"error": 0xE03131, "info": 0x2F9E44}
_DEFAULT_COLOR = 0x868E96  # gray fallback for an unknown level

_MAX_FIELD_VALUE = 1000

# Set once when DISCORD_WEBHOOK_URL is missing so the warning fires a single time
# per process rather than on every notify call.
_warned_no_webhook = False


def _sanitize(value) -> str:
    """Coerce to str, strip, truncate to _MAX_FIELD_VALUE chars."""
    return str(value).strip()[:_MAX_FIELD_VALUE]


def notify(cfg: dict, level: str, title: str, fields: dict[str, str]) -> None:
    global _warned_no_webhook

    if not cfg.get("discord", {}).get("enabled"):
        return

    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        if not _warned_no_webhook:
            log.warning("discord.enabled but DISCORD_WEBHOOK_URL unset; "
                        "notifications disabled (this warns once)")
            _warned_no_webhook = True
        return

    embed = {
        "title": title,
        "color": _COLOR.get(level, _DEFAULT_COLOR),
        "fields": [
            {"name": str(name), "value": _sanitize(value), "inline": True}
            for name, value in fields.items()
        ],
    }
    timeout = cfg.get("discord", {}).get("timeout_seconds", 5)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json={"embeds": [embed]})
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a dead webhook must never crash the worker
        # NEVER include `url` in this message (it carries the secret token).
        log.warning("discord notify failed: %s", exc.__class__.__name__)
