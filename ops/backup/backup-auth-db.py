#!/usr/bin/env python3
"""Create a consistent local SQLite backup and retain the newest 14 copies."""

from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
import os
import sqlite3


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

for old in sorted(backup_dir.glob("auth-*.db"), reverse=True)[14:]:
    if old.is_file() and old.parent == backup_dir:
        old.unlink()

print(f"Created consistent auth DB backup: {destination.name}")
