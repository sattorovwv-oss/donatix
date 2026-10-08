from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stage import check_site, replacement
from backup_db import backup
import sqlite3


def test_cli_preserves_bind_address_and_port():
    assert replacement('/home/donatix/.venv/bin/python -m donatix serve --host 127.0.0.1 --port 8000') == \
        '/home/donatix/.venv/bin/python -m donatix_android_extension serve --host 127.0.0.1 --port 8000'


def test_uvicorn_preserves_flags():
    cmd = '/home/donatix/.venv/bin/uvicorn donatix.app:factory --factory --host 127.0.0.1 --port 8000 --workers 4'
    assert replacement(cmd) == cmd.replace('donatix.app:factory', 'donatix_android_extension.factory:create_app')


def test_unknown_commands_and_changed_site_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        replacement('/bin/sh launch.sh')
    with pytest.raises(ValueError):
        replacement('/usr/bin/python -m donatix serve --reload')
    with pytest.raises(ValueError):
        check_site(tmp_path)


def test_backup_includes_wal_and_preserves_source(tmp_path):
    source = tmp_path / 'site.db'
    connection = sqlite3.connect(source)
    connection.execute('PRAGMA journal_mode=WAL')
    for name in ('users', 'orders', 'payments', 'transactions'):
        connection.execute(f'CREATE TABLE {name}(id INTEGER)')
    connection.execute('INSERT INTO orders VALUES(42)')
    connection.commit()
    target = tmp_path / 'private-backup/site.sqlite3'
    backup(source, target)
    with sqlite3.connect(target) as copy:
        assert copy.execute('SELECT id FROM orders').fetchall() == [(42,)]
    assert connection.execute('SELECT id FROM orders').fetchall() == [(42,)]
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        backup(source, target)
    connection.close()
