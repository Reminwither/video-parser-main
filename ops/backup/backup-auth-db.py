#!/usr/bin/env python3
"""Create a checked SQLite backup locally and in a private COS bucket."""

from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
import hashlib
import os
import sqlite3


def copy_to_cos(path: Path) -> None:
    bucket = os.environ.get("VIDEO_PARSER_BACKUP_COS_BUCKET")
    if not bucket:
        return

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url="https://cos.ap-shanghai.myqcloud.com",
        region_name="ap-shanghai",
        aws_access_key_id=os.environ["TENCENT_ASR_SECRET_ID"],
        aws_secret_access_key=os.environ["TENCENT_ASR_SECRET_KEY"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    key = f"auth/{path.name}"
    client.upload_file(str(path), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    metadata = client.head_object(Bucket=bucket, Key=key)
    if metadata["ContentLength"] != path.stat().st_size or metadata.get("ServerSideEncryption") != "AES256":
        raise RuntimeError("COS backup size or encryption verification failed")

    # Exercise the actual restore path, rather than trusting a successful upload response.
    restored = path.with_name(f".{path.stem}-restore.tmp")
    try:
        client.download_file(bucket, key, str(restored))
        if hashlib.sha256(restored.read_bytes()).digest() != hashlib.sha256(path.read_bytes()).digest():
            raise RuntimeError("COS restore checksum mismatch")
        with closing(sqlite3.connect(f"file:{restored}?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("COS restore integrity check failed")
    finally:
        restored.unlink(missing_ok=True)
    print(f"COS backup uploaded and restored successfully: {key}")


source = Path("/opt/video-parser/data/auth.db")
backup_dir = Path("/var/backups/video-parser")
backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(backup_dir, 0o700)

stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
destination = backup_dir / f"auth-{stamp}.db"
temporary = backup_dir / f".auth-{stamp}.tmp"

try:
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(temporary)) as copy:
            original.backup(copy)
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)
finally:
    temporary.unlink(missing_ok=True)

copy_to_cos(destination)

for old in sorted(backup_dir.glob("auth-*.db"), reverse=True)[14:]:
    if old.is_file() and old.parent == backup_dir:
        old.unlink()

print(f"Created consistent auth DB backup: {destination.name}")
