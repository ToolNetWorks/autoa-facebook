"""Cloudflare R2 storage for the album of a workspace.

R2 speaks the S3 protocol, so the ordinary AWS SDK is used. The bucket holds
one folder per workspace and one folder per owner inside it:

    album-ctv/@hiu9h/chung/<file>      pictures of the shared pool
    album-ctv/@hiu9h/hiu9h/<file>      pictures of the collaborator "hiu9h"

Nothing is public: the files are only reachable through the API of the
application, which checks the workspace and the owner of every request.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_BUCKET = "album-ctv"
SHARED_SCOPE = "chung"
SAFE_FOLDER = re.compile(r"[^A-Za-z0-9._@-]+")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def config() -> dict:
    """The R2 configuration of the environment."""
    return {
        "endpoint": _env("R2_ENDPOINT"),
        "access_key": _env("R2_ACCESS_KEY_ID"),
        "secret_key": _env("R2_SECRET_ACCESS_KEY"),
        "bucket": _env("R2_BUCKET", DEFAULT_BUCKET),
        "token": _env("R2_API_TOKEN"),
        "account_id": _env("R2_ACCOUNT_ID"),
        "region": _env("R2_REGION", "auto"),
    }


def enabled() -> bool:
    """True when the album has to be stored in the cloud."""
    settings = config()
    return bool(settings["endpoint"] and settings["access_key"]
                and settings["secret_key"] and settings["bucket"])


def client():
    """The S3 client of R2."""
    import boto3
    from botocore.config import Config

    settings = config()
    return boto3.client(
        "s3",
        endpoint_url=settings["endpoint"],
        aws_access_key_id=settings["access_key"],
        aws_secret_access_key=settings["secret_key"],
        region_name=settings["region"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 3,
                                                         "mode": "standard"}),
    )


def folder(value: str) -> str:
    """A folder name that is safe for every S3 implementation."""
    cleaned = SAFE_FOLDER.sub("-", (value or "").strip()).strip(".-")
    return cleaned or SHARED_SCOPE


def remote_key(pipeline_name: str, owner_label: str, local_key: str) -> str:
    """The R2 key of a picture that is stored locally under ``local_key``.

    The local key starts with the id of the pipeline; that part becomes the
    name of the workspace folder.
    """
    relative = (local_key or "").strip("/")
    parts = relative.split("/", 1)
    tail = parts[1] if len(parts) > 1 else parts[0]
    # A key never carries a relative part, so a crafted value cannot walk out
    # of its folder.
    tail = "/".join(part for part in tail.split("/") if part not in ("", ".", ".."))
    scope = folder(owner_label) if owner_label else SHARED_SCOPE
    return f"{folder(pipeline_name)}/{scope}/{tail}"


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    """Store one object."""
    client().put_object(Bucket=config()["bucket"], Key=key, Body=data,
                        ContentType=content_type or "application/octet-stream")


def get_bytes(key: str) -> bytes | None:
    """Read one object, or None when it does not exist."""
    try:
        response = client().get_object(Bucket=config()["bucket"], Key=key)
    except Exception:
        return None
    return response["Body"].read()


def delete_key(key: str) -> None:
    """Remove one object; a missing object is not an error."""
    if not key:
        return
    try:
        client().delete_object(Bucket=config()["bucket"], Key=key)
    except Exception:
        return


def ensure_bucket() -> bool:
    """Make sure the bucket of the album exists."""
    if not enabled():
        return False
    settings = config()
    try:
        client().head_bucket(Bucket=settings["bucket"])
        return True
    except Exception:
        pass
    try:
        client().create_bucket(Bucket=settings["bucket"])
        return True
    except Exception:
        return False


def ensure_folder(prefix: str) -> None:
    """Create the folder of a workspace so it shows up in the console."""
    if not enabled() or not prefix:
        return
    key = prefix.strip("/") + "/"
    try:
        client().put_object(Bucket=config()["bucket"], Key=key, Body=b"",
                            ContentType="application/x-directory")
    except Exception:
        return


def list_keys(prefix: str = "") -> list[str]:
    """Every key of the bucket that starts with the prefix."""
    if not enabled():
        return []
    keys: list[str] = []
    token = None
    try:
        while True:
            arguments = {"Bucket": config()["bucket"], "Prefix": prefix or ""}
            if token:
                arguments["ContinuationToken"] = token
            response = client().list_objects_v2(**arguments)
            keys.extend(item["Key"] for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                break
    except Exception:
        return keys
    return keys


def usage(prefix: str = "") -> int:
    """How much space the album uses in the bucket."""
    total = 0
    if not enabled():
        return 0
    token = None
    try:
        while True:
            arguments = {"Bucket": config()["bucket"], "Prefix": prefix or ""}
            if token:
                arguments["ContinuationToken"] = token
            response = client().list_objects_v2(**arguments)
            total += sum(int(item.get("Size", 0)) for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                break
    except Exception:
        return total
    return total
