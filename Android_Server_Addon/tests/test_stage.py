from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stage import check_site, replacement


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
