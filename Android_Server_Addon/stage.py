#!/usr/bin/env python3
"""Read-only site/service inspection. Writes a draft drop-in; never installs it."""
import argparse
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def check_site(site):
    hashes = json.loads((ROOT / "site_hashes.json").read_text())
    changed = [name for name, digest in hashes.items()
               if not (site / name).is_file() or hashlib.sha256((site / name).read_bytes()).hexdigest() != digest]
    if changed:
        raise ValueError("Site version differs from the verified original ZIP. No deployment: " + ", ".join(changed))


def replacement(command):
    args = shlex.split(command)
    if len(args) >= 4 and args[1:4] == ["-m", "donatix", "serve"]:
        allowed = {"--host", "--port"}
        if any(args[i] not in allowed for i in range(4, len(args), 2)) or (len(args) - 4) % 2:
            raise ValueError("Unsupported serve arguments; preserve and review the original command manually")
        args[2] = "donatix_android_extension"
    elif any(x in args for x in ("donatix.app:factory", "donatix.app:create_app")) and "--factory" in args:
        args = ["donatix_android_extension.factory:create_app" if x in ("donatix.app:factory", "donatix.app:create_app") else x for x in args]
    else:
        raise ValueError("Unknown service launch command; no automatic replacement")
    if not all(re.fullmatch(r"[A-Za-z0-9_/.:=,+-]+", a) for a in args):
        raise ValueError("Unsupported quoting in the launch command; manual review is required")
    return " ".join(args)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--site", type=Path, default=Path("/home/donatix/app"))
    p.add_argument("--service", default="donatix")
    p.add_argument("--installed-addon", default="/opt/donatix-android-addon/v1")
    p.add_argument("--state", default="/var/lib/donatix-android/android.sqlite3")
    p.add_argument("--credentials", default="/etc/donatix/firebase-service-account.json")
    p.add_argument("--project", default="donatix-660fc")
    p.add_argument("--output", type=Path, default=Path("/tmp/donatix-android-stage"))
    args = p.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.service):
        raise ValueError("Invalid service name")
    check_site(args.site.resolve())
    values = {}
    for prop in ("ExecStart", "WorkingDirectory", "User", "ActiveState"):
        values[prop] = subprocess.check_output(["systemctl", "show", args.service, "--value", "-p", prop], text=True).strip()
    if values["WorkingDirectory"] != str(args.site.resolve()) or values["ActiveState"] != "active" or not values["User"]:
        raise ValueError("Unexpected service working directory, state or user")
    match = re.search(r"argv\[\]=(.*?)\s*;\s*ignore_errors=", values["ExecStart"])
    if not match:
        raise ValueError("Could not inspect the launch command safely")
    command = replacement(match.group(1))
    for value in (args.installed_addon, args.state, args.credentials, args.project, str(args.site.resolve())):
        if not re.fullmatch(r"[A-Za-z0-9_/.-]+", value):
            raise ValueError("Unsupported path or project identifier")
    content = ('[Service]\n'
        f'Environment="DONATIX_ANDROID_DB={args.state}"\n'
        f'Environment="DONATIX_FIREBASE_CREDENTIALS={args.credentials}"\n'
        f'Environment="DONATIX_ANDROID_FIREBASE_PROJECT={args.project}"\n'
        'ExecStart=\n'
        f'ExecStart=/usr/bin/env PYTHONPATH={args.installed_addon}:{args.site.resolve()} {command}\n')
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    (args.output / "40-donatix-android.conf").write_text(content)
    (args.output / "plan.json").write_text(json.dumps({"site": str(args.site.resolve()), "service": args.service,
        "user": values["User"], "installed_addon": args.installed_addon, "state": args.state,
        "credentials": args.credentials, "project": args.project}, indent=2) + "\n")
    print("Draft prepared. No site files, site database or systemd configuration were modified.")
    print("Review:", args.output / "40-donatix-android.conf")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
