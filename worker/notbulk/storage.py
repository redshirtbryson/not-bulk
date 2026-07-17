"""boto3 S3/MinIO client. Endpoint + credentials come from config.yaml's
`storage` block (compose-local MinIO creds are plaintext, NOT secret material —
see the M2 plan Global Constraints). Path-style addressing is forced so the
same code targets MinIO now and R2 at M5.

Key formats mirror the Node Storage helper EXACTLY (Interface Contract):
  photos: {user_id}/{batch_id}/{photo_id}.webp
  crops:  {user_id}/{batch_id}/crops/{card_id}.webp
"""
from __future__ import annotations

import boto3
from botocore.config import Config as BotoConfig


class Storage:
    def __init__(self, cfg: dict):
        s = cfg["storage"]
        self._bucket = s["bucket"]
        self._client = boto3.client(
            "s3",
            endpoint_url=s["endpoint"],
            aws_access_key_id=s["access_key"],
            aws_secret_access_key=s["secret_key"],
            config=BotoConfig(s3={"addressing_style": "path"}, signature_version="s3v4"),
            region_name="us-east-1",  # MinIO ignores region; boto3 requires one
        )

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=body, ContentType=content_type
        )

    @staticmethod
    def photo_key(user_id: str, batch_id: str, photo_id: str) -> str:
        return f"{user_id}/{batch_id}/{photo_id}.webp"

    @staticmethod
    def crop_key(user_id: str, batch_id: str, card_id: str) -> str:
        return f"{user_id}/{batch_id}/crops/{card_id}.webp"
