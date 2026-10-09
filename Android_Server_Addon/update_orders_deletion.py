#!/usr/bin/env python3
"""Upgrade two Android features, preserving the existing site and OAuth code.

Default: read-only preflight. --apply: verified code backup, atomic module
replacement, service restart and health checks, automatic code-only rollback.
The website's source, .env, service configuration and database schema are never
written here. Never restore a database over new customer operations.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SITE = Path("/home/donatix/app")
ADDON = Path("/opt/donatix-android-addon/v1/donatix_android_extension")
SERVICE = "donatix"
NAMES = ("history.py", "erasure.py", "factory.py")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_manifest():
    data = json.loads((ROOT / "orders_deletion_manifest.json").read_text())
    if data.get("format") != 1:
        raise ValueError("Unsupported update manifest")
    for name, expected in data["payload"].items():
        path = ROOT / name
        permitted = {expected}
        if name == "donatix_android_extension/factory.py":
            permitted.update(variant["after"] for variant in data["factories"])
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) not in permitted:
            raise ValueError("Update upload differs from the verified package: " + name)
    return data


def patched_factory(raw, manifest):
    before = digest(raw)
    variants = manifest["factories"]
    if before in {v["after"] for v in variants}:
        return raw
    expected = next((v["after"] for v in variants if v["before"] == before), None)
    if expected is None:
        raise ValueError("Installed Android factory changed; refusing to overwrite existing fixes")
    text = raw.decode("utf-8")
    for edit in manifest["factory_edits"]:
        if text.count(edit["before"]) != 1:
            raise ValueError("Feature patch does not match the installed addon")
        text = text.replace(edit["before"], edit["after"], 1)
    result = text.encode("utf-8")
    if digest(result) != expected:
        raise ValueError("Patched factory does not match its verified variant")
    ast.parse(text)
    return result


def property_value(prop):
    return subprocess.check_output(["systemctl", "show", SERVICE, "--value", "-p", prop],
                                   text=True, timeout=10).strip()


def service_values():
    values = {p: property_value(p) for p in ("WorkingDirectory", "User", "ActiveState", "ExecStart", "Environment")}
    if values["WorkingDirectory"] != str(SITE) or values["User"] != "donatix" or values["ActiveState"] != "active":
        raise ValueError("Unexpected service directory, user or state; nothing was installed")
    command = values["ExecStart"]
    if "donatix_android_extension" not in command or f"PYTHONPATH={ADDON.parent}:{SITE}" not in command:
        raise ValueError("The existing Android addon is not the service's active launcher")
    match = re.search(r"--port\s+(\d+)", command)
    if match is None or not 1 <= int(match[1]) <= 65535:
        raise ValueError("Could not identify the existing service port")
    environment = dict(item.split("=", 1) for item in shlex.split(values["Environment"]) if "=" in item)
    state = Path(environment.get("DONATIX_ANDROID_DB", ""))
    if not state.is_absolute() or not state.is_file() or state.is_symlink():
        raise ValueError("Existing private Android database was not found")
    # Read only public config/path information. Never print credentials or the
    # complete systemd Environment/.env. Config.from_env does not create an app.
    env = os.environ.copy()
    env.update(environment)
    env["PYTHONPATH"] = str(SITE)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    info = subprocess.run(["runuser", "-u", "donatix", "--", str(SITE / ".venv/bin/python"), "-B", "-c",
        "import json; from pathlib import Path; from donatix.config import Config; c=Config.from_env(); "
        "print(json.dumps({'db':str(Path(c.db_path).resolve()),'url':c.base_url}))"],
        cwd=SITE, env=env, text=True, capture_output=True, timeout=15)
    if info.returncode:
        raise ValueError("Could not read the existing service configuration; no code installed")
    result = json.loads(info.stdout)
    database = Path(result["db"])
    if not database.is_file() or database == state.resolve():
        raise ValueError("Unexpected site database")
    if result["url"].rstrip("/") != "https://donatix.tj":
        raise ValueError("Unexpected site URL")
    return {"state": state, "database": database, "local": "http://127.0.0.1:" + match[1],
            "environment": environment, "url": result["url"].rstrip("/"), "command": command}


def validate_site(manifest, database):
    for name, expected in manifest["site_code"].items():
        path = SITE / name
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != expected:
            raise ValueError("Site code changed since verification; no update installed: " + name)
    # The deletion implementation is tested against this customer's live schema.
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT json_valid('{}')")
        for table, required in manifest["schema"].items():
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not set(required) <= columns:
                raise ValueError("Unexpected database schema: " + table)


def validate_addon(manifest):
    for parent in (ADDON, ADDON.parent, ADDON.parent.parent):
        if parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
            raise ValueError("Addon directories must be root-owned and not writable by other users")
    for name, expected in manifest["unchanged_modules"].items():
        path = ADDON / name
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != expected:
            raise ValueError("Existing addon module changed; will not overwrite it: " + name)
    raw = (ADDON / "factory.py").read_bytes()
    if (ADDON / "factory.py").is_symlink():
        raise ValueError("Refusing an addon symlink")
    desired = {"factory.py": patched_factory(raw, manifest)}
    for name in ("history.py", "erasure.py"):
        desired[name] = (ROOT / "donatix_android_extension" / name).read_bytes()
        ast.parse(desired[name].decode())
        path = ADDON / name
        known = {digest(desired[name]), *manifest.get("previous_feature_modules", {}).get(name, [])}
        if path.is_symlink() or (path.exists() and digest(path.read_bytes()) not in known):
            raise ValueError("An unrelated version of the feature module already exists: " + name)
    return desired


def fetch(url, path):
    request = Request(url + path, headers={"Cache-Control": "no-cache", "User-Agent": "Donatix-Feature-Update/9"})
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, response.read(2 * 1024 * 1024)
    except HTTPError as error:
        return error.code, error.read(2 * 1024 * 1024)


def health(url, previous=None):
    status, body = fetch(url, "/api/v1/android/config")
    data = json.loads(body)
    if status != 200 or data.get("ok") is not True or data.get("android_extension_version") != 1:
        raise ValueError("Android config unavailable")
    for path, expected in (("/", 200), ("/login", 200), ("/api/v1/me", 401)):
        if fetch(url, path)[0] != expected:
            raise ValueError("Existing route health check failed: " + path)
    status, body = fetch(url, "/api/openapi.json")
    if status != 200:
        raise ValueError("Existing API schema unavailable")
    paths = set(json.loads(body)["paths"])
    if previous:
        if not previous["paths"] <= paths:
            raise ValueError("An existing API route disappeared")
        for key in ("google_enabled", "fcm_enabled"):
            if data.get(key) != previous["config"].get(key):
                raise ValueError("Existing Google or FCM settings changed")
        if data.get("order_history") is not True or data.get("account_deletion") is not True:
            raise ValueError("New features are not active")
        for path, expected in (("/android/account-deletion", 200),
                               ("/api/v1/android/orders", 401),
                               ("/api/v1/android/account/deletion", 401)):
            if fetch(url, path)[0] != expected:
                raise ValueError("New feature route check failed: " + path)
    return {"config": data, "paths": paths}


def atomic_write(path, raw, mode=0o644):
    fd, name = tempfile.mkstemp(prefix=".orders-delete-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def restart():
    subprocess.run(["systemctl", "restart", SERVICE], check=True, timeout=35, capture_output=True)


def apply(desired, service, baseline):
    changed = [name for name in NAMES if not (ADDON / name).exists() or (ADDON / name).read_bytes() != desired[name]]
    if not changed:
        health(service["local"], baseline)
        print("Already installed. No restart or files changed.")
        return
    backup = Path("/var/backups/donatix-android") / ("orders-delete-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    if backup.parent.exists() and (backup.parent.is_symlink() or backup.parent.stat().st_uid != 0):
        raise ValueError("Unexpected code backup directory")
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    original = {}
    for name in changed:
        path = ADDON / name
        original[name] = path.read_bytes() if path.exists() else None
        if original[name] is not None:
            atomic_write(backup / name, original[name], 0o600)
    metadata = {"installed": {n: digest(desired[n]) for n in changed},
                "original": {n: digest(raw) if raw is not None else None for n, raw in original.items()}}
    atomic_write(backup / "rollback.json", json.dumps(metadata, indent=2).encode(), 0o600)
    try:
        # New imports are installed before replacing the factory. Existing
        # workers keep their current code until the one service restart.
        for name in changed:
            atomic_write(ADDON / name, desired[name])
        if property_value("ExecStart") != service["command"]:
            raise ValueError("Service launcher changed during installation")
        restart()
        deadline = time.monotonic() + 45
        while True:
            try:
                health(service["local"], baseline)
                health(service["url"], baseline)
                if property_value("ActiveState") != "active":
                    raise ValueError("Service is not active")
                break
            except (ValueError, OSError):
                if time.monotonic() >= deadline:
                    raise ValueError("Health check deadline reached")
                time.sleep(1)
    except BaseException:
        for name, raw in original.items():
            if raw is None:
                (ADDON / name).unlink(missing_ok=True)
            else:
                atomic_write(ADDON / name, raw)
        restart()
        print("Activation failed: previous addon code restored. No database was restored.")
        raise
    print("Order history and account deletion activated: OK")
    print("Existing site, API, Google and FCM health checks: OK")
    print("Website sources, .env, systemd configuration and site schema were not changed.")
    print("Code rollback backup:", backup)


def rollback(folder):
    folder = folder.resolve()
    if folder.parent != Path("/var/backups/donatix-android") or folder.stat().st_uid != 0:
        raise ValueError("Unexpected rollback directory")
    data = json.loads((folder / "rollback.json").read_text())
    if not set(data["installed"]) <= set(NAMES):
        raise ValueError("Unexpected rollback file names")
    for name, installed in data["installed"].items():
        path = ADDON / name
        if path.is_symlink() or digest(path.read_bytes()) != installed:
            raise ValueError("Addon changed after installation; rollback stopped: " + name)
        previous = data["original"][name]
        if previous is not None and digest((folder / name).read_bytes()) != previous:
            raise ValueError("Code backup integrity check failed")
    for name in reversed(NAMES):
        if name not in data["installed"]:
            continue
        if data["original"][name] is None:
            (ADDON / name).unlink()
        else:
            atomic_write(ADDON / name, (folder / name).read_bytes())
    restart()
    print("Previous addon code restored. Customer operations/database were not rolled back.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--rollback", type=Path)
    args = p.parse_args()
    if os.geteuid() != 0:
        raise ValueError("Run the installer as root")
    # Serialize installations without stopping the running site for preflight.
    with open("/run/donatix-orders-deletion-update.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            rollback(args.rollback)
            return
        manifest = read_manifest()
        desired = validate_addon(manifest)
        service = service_values()
        validate_site(manifest, service["database"])
        baseline = health(service["local"])
        print("Preflight OK: known site/addon code, schema and active service verified.")
        if args.apply:
            apply(desired, service, baseline)
        else:
            print("Read-only check completed. Use --apply to install the two features and restart donatix.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Avoid displaying subprocess output which could contain environment secrets.
        raise SystemExit("STOP: " + (str(error) if isinstance(error, ValueError) else type(error).__name__))
