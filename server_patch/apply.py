#!/usr/bin/env python3
"""Revision-checked installation and rollback of the mobile extension."""
import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def atomic_copy(source, target):
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        handle.write(source.read_bytes())
        temp = Path(handle.name)
    try:
        reference = target if target.exists() else target.parent / 'app.py'
        stat = reference.stat()
        os.chmod(temp, stat.st_mode & 0o777 if target.exists() else 0o644)
        if hasattr(os, 'chown') and (temp.stat().st_uid, temp.stat().st_gid) != (stat.st_uid, stat.st_gid):
            os.chown(temp, stat.st_uid, stat.st_gid)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


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
        shutil.rmtree(backup)
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
        raise SystemExit('An earlier backup exists. Keep it and review the update manually.')
    if args.check:
        print('Revision check passed; would install: ' + ', '.join(records))
        return
    backup.mkdir(mode=0o700)
    for name, entry in records.items():
        if entry['before']:
            shutil.copy2(package / name, backup / name)
    manifest.write_text(json.dumps(records, indent=2))
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
        shutil.rmtree(backup)
        raise
    print('Mobile extension installed with rollback backup. Database and .env were not changed.')
    print('Restart Donatix and verify /api/v1/mobile/config and /api/v1/mobile-session.')


if __name__ == '__main__':
    main()
