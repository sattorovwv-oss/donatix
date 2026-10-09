"""Installer contract checks; no systemctl or production server is used."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("orders_update", ROOT / "update_orders_deletion.py")
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


def test_factory_patch_is_scoped_and_preserves_both_deployed_google_variants():
    manifest = update.read_manifest()
    current = (ROOT / "donatix_android_extension/factory.py").read_bytes()
    old = current.decode()
    for edit in reversed(manifest["factory_edits"]):
        old = old.replace(edit["after"], edit["before"], 1)
    assert update.patched_factory(old.encode(), manifest) == current
    assert update.patched_factory(current, manifest) == current
    with pytest.raises(ValueError):
        update.patched_factory(old.encode() + b"\n# unreviewed edit\n", manifest)
    # The second approved variant has the customer's Google link/reset fixes.
    google_old = old.replace(
        'relevant = scope["type"] == "http" and scope.get("path") in ("/auth/google/callback", "/login/code")',
        'relevant = scope["type"] == "http" and scope.get("path") in ("/auth/google", "/auth/google/callback", "/login/code")')
    google_old = google_old.replace('        async def response(message):',
        '        # The live site also offers Google linking and password recovery. These\n'
        '        # browser operations must never complete a pending mobile login.\n'
        '        browser_operation = (ticket and scope.get("path") == "/auth/google" and\n'
        '                             any(Request(scope).query_params.get(mode, "0") not in ("", "0")\n'
        '                                 for mode in ("link", "reset")))\n\n'
        '        async def response(message):', 1)
    google_old = google_old.replace('\n\n        # The live site', '\n        # The live site', 1)
    google_old = google_old.replace(
        '                if message["status"] == 303 and location in (b"/panel", b"/admin") and session.get("user_id") and session.get("sid"):',
        '                if browser_operation:\n'
        '                    message = dict(message)\n'
        '                    message["headers"] = list(headers) + [(b"set-cookie",\n'
        '                        (COOKIE + "=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax").encode())]\n'
        '                elif (scope.get("path") in ("/auth/google/callback", "/login/code") and\n'
        '                      message["status"] == 303 and location.startswith(b"/") and\n'
        '                      not location.startswith(b"//") and location not in (b"/login", b"/login/code") and\n'
        '                      session.get("user_id") and session.get("sid") and not session.get("pending_2fa")):')
    result = update.patched_factory(google_old.encode(), manifest).decode()
    assert result[:result.index("class Prepare")] == google_old[:google_old.index("class Prepare")].replace(
        'from .store import Store, site_readonly\n',
        'from .store import Store, site_readonly\nfrom .erasure import Erasure, mount as mount_erasure\nfrom .history import history\n')


def test_installer_payload_integrity_check_stops_on_changed_upload(tmp_path, monkeypatch):
    manifest = json.loads((ROOT / "orders_deletion_manifest.json").read_text())
    (tmp_path / "orders_deletion_manifest.json").write_text(json.dumps(manifest))
    for name in manifest["payload"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    monkeypatch.setattr(update, "ROOT", tmp_path)
    (tmp_path / "donatix_android_extension/history.py").write_text("changed")
    with pytest.raises(ValueError, match="history.py"):
        update.read_manifest()


def test_atomic_code_replacement_and_rollback_never_restore_customer_database(tmp_path, monkeypatch):
    addon = tmp_path / "addon"
    addon.mkdir()
    (addon / "factory.py").write_bytes(b"original factory")
    site = tmp_path / "site.sqlite3"
    with sqlite3.connect(site) as c:
        c.execute("CREATE TABLE customer_payments(id INTEGER)")
        c.execute("INSERT INTO customer_payments VALUES(1)")
    monkeypatch.setattr(update, "ADDON", addon)
    monkeypatch.setattr(update.time, "sleep", lambda _: None)
    monkeypatch.setattr(update, "property_value", lambda _: "unchanged launch")
    monkeypatch.setattr(update, "restart", lambda: None)
    calls = []
    def failed_health(*args):
        calls.append(1)
        with sqlite3.connect(site) as c:
            c.execute("INSERT INTO customer_payments VALUES(2)")
        raise ValueError("simulated failed startup")
    monkeypatch.setattr(update, "health", failed_health)
    clock = iter([0, 100])
    monkeypatch.setattr(update.time, "monotonic", lambda: next(clock))
    # Redirect only code-backup paths away from the machine's /var/backups.
    real_path = update.Path
    monkeypatch.setattr(update, "Path", lambda value: tmp_path / "backups" if value == "/var/backups/donatix-android" else real_path(value))
    with pytest.raises(ValueError, match="deadline"):
        update.apply({"factory.py": b"new factory", "history.py": b"new history", "erasure.py": b"new erasure"},
                     {"command": "unchanged launch", "local": "local", "url": "public"}, {"config": {}, "paths": set()})
    assert (addon / "factory.py").read_bytes() == b"original factory"
    assert not (addon / "history.py").exists() and not (addon / "erasure.py").exists()
    with sqlite3.connect(site) as c:
        assert c.execute("SELECT id FROM customer_payments").fetchall() == [(1,), (2,)]


def test_current_site_schema_and_code_verified_before_any_install(tmp_path, monkeypatch):
    from donatix import db
    import donatix
    current = Path(donatix.__file__).parent.parent
    manifest = update.read_manifest()
    staged = tmp_path / "site"
    for name, expected in manifest["site_code"].items():
        data = (current / name).read_bytes()
        # This contract uses the received current server snapshot.
        assert update.digest(data) == expected
        target = staged / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    database = tmp_path / "site.db"
    db.init(database)
    monkeypatch.setattr(update, "SITE", staged)
    update.validate_site(manifest, database)
    app = staged / "donatix/app.py"
    app.write_bytes(app.read_bytes() + b"\n# unknown subsequent deployment\n")
    with pytest.raises(ValueError, match="Site code changed"):
        update.validate_site(manifest, database)
    with sqlite3.connect(database) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        c.execute("DROP TABLE guest_keys")
    app.write_bytes((current / "donatix/app.py").read_bytes())
    with pytest.raises(ValueError, match="Unexpected database schema: guest_keys"):
        update.validate_site(manifest, database)


def test_known_previous_feature_modules_upgrade_but_unknown_edits_stop(tmp_path, monkeypatch):
    import os
    if os.geteuid() != 0:
        pytest.skip("Root-owned directory contract")
    manifest = update.read_manifest()
    addon = tmp_path / "opt" / "v1" / "extension"
    addon.mkdir(parents=True)
    for name in manifest["unchanged_modules"]:
        (addon/name).write_bytes((ROOT/"donatix_android_extension"/name).read_bytes())
    for name in ("factory.py", "history.py", "erasure.py"):
        (addon/name).write_bytes((ROOT/"donatix_android_extension"/name).read_bytes())
    previous = b"known previous feature module"
    (addon/"erasure.py").write_bytes(previous)
    manifest["previous_feature_modules"]["erasure.py"] = [update.digest(previous)]
    monkeypatch.setattr(update, "ADDON", addon)
    desired = update.validate_addon(manifest)
    assert desired["erasure.py"] == (ROOT/"donatix_android_extension/erasure.py").read_bytes()
    (addon/"erasure.py").write_bytes(previous + b"unknown edit")
    with pytest.raises(ValueError, match="unrelated version"):
        update.validate_addon(manifest)
