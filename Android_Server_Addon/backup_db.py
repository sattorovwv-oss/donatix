#!/usr/bin/env python3
"""Consistent read-only SQLite backup. Never restores the site's database."""
import argparse
import os
import sqlite3
import time
import uuid
from pathlib import Path


def backup(source, destination, timeout=40):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if destination.exists() or source == destination:
        raise ValueError("Choose a new backup filename outside the source database")
    if destination.parent.exists() and (destination.parent.is_symlink() or destination.parent.stat().st_uid != os.geteuid()):
        raise ValueError("The private backup directory must belong to the current user")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    temporary = destination.parent / (".android-backup-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    deadline = time.monotonic() + timeout

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise TimeoutError("Backup deadline reached; the original database was not modified")

    try:
        src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)
        dst = sqlite3.connect(temporary)
        try:
            tables = {x[0] for x in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"users", "orders", "payments", "transactions"} <= tables:
                raise ValueError("This is not the expected Donatix database")
            src.backup(dst, pages=256, progress=progress, sleep=0.01)
            if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("The backup integrity check failed")
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dst.close()
            src.close()
        # Atomic create without overwriting a concurrent backup or following symlinks.
        os.link(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    backup(args.source, args.destination)
    print("Consistent backup created and checked. The source database was not modified.")


if __name__ == "__main__":
    main()
