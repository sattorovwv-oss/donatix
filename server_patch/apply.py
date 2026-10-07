#!/usr/bin/env python3
"""Revision-checked installation and rollback of the mobile extension."""
import argparse
import hashlib
import json
import os
import shutil
import secrets
import tempfile
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        handle.write(source.read_bytes())
        temp = Path(handle.name)
    try:
        reference = target if target.exists() else target.parent
        stat = reference.stat()
        os.chmod(temp, stat.st_mode & 0o777 if target.exists() else 0o644)
        if hasattr(os, 'chown') and (temp.stat().st_uid, temp.stat().st_gid) != (stat.st_uid, stat.st_gid):
            os.chown(temp, stat.st_uid, stat.st_gid)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def pop_backup(backup):
    previous = backup / 'previous'
    saved = backup.parent / ('.mobile-extension-restore-' + secrets.token_hex(6))
    if previous.exists():
        previous.rename(saved)
    shutil.rmtree(backup)
    if saved.exists():
        saved.rename(backup)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project', type=Path, help='Folder containing donatix/app.py')
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    package = args.project.resolve() / 'donatix'
    if not (package / 'app.py').is_file():
        raise SystemExit('Wrong directory: donatix/app.py was not found.')
    backup = package / '.mobile-extension-backup'
    manifest = backup / 'manifest.json'
    expected = json.loads((source / 'base_hashes.json').read_text())
    if args.rollback:
        if not manifest.is_file():
            raise SystemExit('No mobile extension backup exists.')
        records = json.loads(manifest.read_text())
        for name, entry in records.items():
            if digest(package / name) != entry['installed']:
                raise SystemExit('Rollback stopped: ' + name + ' changed since installation.')
            if entry['before'] and digest(backup / name) != entry['before']:
                raise SystemExit('Rollback stopped: backup checksum differs for ' + name)
        if args.check:
            print('Rollback is possible; no files changed.')
            return
        for name, entry in records.items():
            if entry['before'] is None:
                (package / name).unlink(missing_ok=True)
            else:
                atomic_copy(backup / name, package / name)
        pop_backup(backup)
        print('Previous code restored. Restart the existing service. Database and .env were not changed.')
        return
    records = {}
    for name, hashes in expected.items():
        old, new = digest(package / name), digest(source / 'donatix' / name)
        if new is None:
            raise SystemExit('Incomplete extension: missing ' + name)
        if old == new:
            continue
        if old not in hashes:
            raise SystemExit('Server revision differs for ' + name + '. Review change.diff; nothing was overwritten.')
        records[name] = {'before':old, 'installed':new}
    if not records:
        print('This mobile extension is already installed.')
        return
    if backup.exists():
        if not manifest.is_file():
            raise SystemExit('Incomplete earlier backup. Review it before updating; nothing was overwritten.')
        for name, entry in json.loads(manifest.read_text()).items():
            if digest(package / name) != entry['installed']:
                raise SystemExit('Earlier installed module changed: ' + name + '. Review change.diff; nothing was overwritten.')
    if args.check:
        print('Revision check passed; would install: ' + ', '.join(records))
        return
    staging = Path(tempfile.mkdtemp(prefix='.mobile-extension-stage-', dir=package))
    try:
        for name, entry in records.items():
            if entry['before']:
                (staging / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(package / name, staging / name)
        (staging / 'manifest.json').write_text(json.dumps(records, indent=2))
        if backup.exists():
            backup.rename(staging / 'previous')
        staging.rename(backup)
    except BaseException:
        if (staging / 'previous').exists():
            (staging / 'previous').rename(backup)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    installed = []
    try:
        for name in records:
            atomic_copy(source / 'donatix' / name, package / name)
            installed.append(name)
    except BaseException:
        for name in reversed(installed):
            if records[name]['before']:
                atomic_copy(backup / name, package / name)
            else:
                (package / name).unlink(missing_ok=True)
        pop_backup(backup)
        raise
    print('Mobile extension installed with rollback backup. Database and .env were not changed.')
    print('Restart Donatix and verify /api/v1/mobile/config and /api/v1/mobile-session.')


if __name__ == '__main__':
    main()
