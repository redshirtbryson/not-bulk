"""Storage key-format tests + a FakeS3 call-recording test. The real boto3
client is never constructed here; one optional integration test (STORAGE_
INTEGRATION=1) mirrors the Node round-trip against local MinIO."""
from __future__ import annotations

import os

import pytest

from notbulk.storage import Storage

CFG = {
    "storage": {
        "endpoint": "http://127.0.0.1:9000",
        "bucket": "notbulk",
        "access_key": "minioadmin",
        "secret_key": "minioadmin",
        "signed_url_ttl_seconds": 900,
    }
}


class FakeS3:
    """Records get_object/put_object calls; returns canned bytes for get."""

    def __init__(self, body: bytes = b"canned"):
        self._body = body
        self.puts = []
        self.gets = []

    def get_object(self, *, Bucket, Key):
        self.gets.append((Bucket, Key))
        return {"Body": _FakeStreamingBody(self._body)}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.puts.append((Bucket, Key, Body, ContentType))


class _FakeStreamingBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def _storage_with_fake(fake):
    s = Storage.__new__(Storage)      # skip __init__ (no real boto3)
    s._client = fake
    s._bucket = CFG["storage"]["bucket"]
    return s


def test_photo_key_format_mirrors_node():
    s = _storage_with_fake(FakeS3())
    assert s.photo_key("u1", "b2", "p3") == "u1/b2/p3.webp"


def test_crop_key_format_mirrors_node():
    s = _storage_with_fake(FakeS3())
    assert s.crop_key("u1", "b2", "c3") == "u1/b2/crops/c3.webp"


def test_put_forwards_bucket_key_body_contenttype():
    fake = FakeS3()
    s = _storage_with_fake(fake)
    s.put("u1/b2/p3.webp", b"bytes", "image/webp")
    assert fake.puts == [("notbulk", "u1/b2/p3.webp", b"bytes", "image/webp")]


def test_get_reads_streaming_body_to_bytes():
    fake = FakeS3(b"hello")
    s = _storage_with_fake(fake)
    assert s.get("u1/b2/p3.webp") == b"hello"
    assert fake.gets == [("notbulk", "u1/b2/p3.webp")]


@pytest.mark.skipif(
    os.environ.get("STORAGE_INTEGRATION") != "1",
    reason="STORAGE_INTEGRATION!=1 (needs local MinIO on 127.0.0.1:9000)",
)
def test_round_trip_against_local_minio():
    s = Storage(CFG)
    key = "test/integration/roundtrip.webp"
    s.put(key, b"\x00\x01\x02roundtrip", "image/webp")
    assert s.get(key) == b"\x00\x01\x02roundtrip"
