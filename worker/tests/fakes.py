"""Shared test doubles for worker pipeline tests (Tasks 12, 13)."""
from __future__ import annotations


class FakeCursor:
    """Minimal psycopg-cursor stand-in with canned result rows.

    `script` is a list of row-lists consumed one execute() at a time, so a test
    can stage multiple queries. fetchone() returns the first row (or None).
    """

    def __init__(self, script):
        self._script = list(script)
        self._current = []
        self.executed = []  # list of (sql, params) for assertions
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._current = self._script.pop(0) if self._script else []
        self.rowcount = len(self._current)
        return self

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):     # jobqueue helpers commit; no-op for the fake
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePool:
    """Stand-in for psycopg_pool.ConnectionPool: pool.connection() -> conn."""

    def __init__(self, rows_script):
        # rows_script: list of row-lists, one per expected query.
        self.cursor = FakeCursor(rows_script)
        self._conn = FakeConnection(self.cursor)

    def connection(self):
        return self._conn


class FakeOcrEngine:
    """Stand-in for a PaddleOCR instance.

    `.ocr(img, cls=...)` returns PaddleOCR's structure:
      [ [ [box, (text, confidence)], ... ] ]   # one page, list of lines
    `results` maps a call ordinal to the lines for that call so name-band and
    number-band reads can differ.
    """

    def __init__(self, results):
        self._results = list(results)
        self._i = 0

    def ocr(self, img, cls=False):
        page = self._results[self._i] if self._i < len(self._results) else []
        self._i += 1
        return [page]


class _FakeMessages:
    def __init__(self, canned_text, raise_if_called):
        self._canned_text = canned_text
        self._raise = raise_if_called
        self.calls = []

    def create(self, **kwargs):
        if self._raise:
            raise AssertionError("Anthropic client must NOT be called on cache hit")
        self.calls.append(kwargs)
        # Anthropic response: .content is a list of blocks with .text
        block = type("Block", (), {"type": "text", "text": self._canned_text})()
        return type("Msg", (), {"content": [block]})()


class FakeAnthropic:
    """Stand-in for anthropic.Anthropic. Set raise_if_called=True to assert the
    client is never hit (cache-hit path)."""

    def __init__(self, canned_text="", raise_if_called=False):
        self.messages = _FakeMessages(canned_text, raise_if_called)
