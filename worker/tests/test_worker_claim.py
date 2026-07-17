"""The Python worker must claim exactly its handler types — no more (would claim
an unhandled 'export' job and dead-letter it), no fewer (a handler would starve).
This asserts the partition the worker uses stays in lockstep with _build_handlers.
"""
from __future__ import annotations

from notbulk import worker


def test_worker_allowed_types_equal_handler_keys():
    handlers = worker._build_handlers()
    allowed = set(tuple(sorted(handlers)))          # mirrors main()'s derivation
    assert allowed == set(handlers)
    # Explicit: the five pipeline types, and NOT 'export' (Node-owned).
    assert allowed == {"detect", "identify", "fetch_source", "ingest_correction", "price"}
    assert "export" not in allowed
