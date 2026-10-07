#!/usr/bin/env python3
"""Copy protected CI inputs without shell interpolation or secret logging."""
import argparse
import json
import os
import plistlib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('platform', choices=['android','ios'])
parser.add_argument('--release', action='store_true')
args = parser.parse_args()

def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(0o600)

if args.platform == 'android':
    raw = os.environ.get('GOOGLE_SERVICES_JSON')
    if raw:
        json.loads(raw)
        write_private(root / 'android/app/google-services.json', raw.encode())
    elif args.release:
        raise SystemExit('Set protected GOOGLE_SERVICES_JSON to the owner Firebase Android configuration.')
    if args.release:
        fields = {'storeFile':os.environ['CM_KEYSTORE_PATH'], 'storePassword':os.environ['CM_KEYSTORE_PASSWORD'],
                  'keyAlias':os.environ['CM_KEY_ALIAS'], 'keyPassword':os.environ['CM_KEY_PASSWORD']}
        def escape(value):
            def character(c):
                if ord(c) > 127:
                    raw=c.encode('utf-16-be')
                    return ''.join('\\u'+raw[i:i+2].hex() for i in range(0,len(raw),2))
                return '\\'+c if c in '\\ =:#!' else '\\n' if c=='\n' else '\\r' if c=='\r' else '\\t' if c=='\t' else c
            return ''.join(character(c) for c in value)
        write_private(root / 'android/key.properties', ('\n'.join(k+'='+escape(v) for k,v in fields.items())+'\n').encode())
else:
    raw = os.environ.get('GOOGLE_SERVICE_INFO_PLIST')
    if raw:
        plistlib.loads(raw.encode())
        write_private(root / 'ios/Runner/GoogleService-Info.plist', raw.encode())
    elif args.release:
        raise SystemExit('Set protected GOOGLE_SERVICE_INFO_PLIST to the owner Firebase iOS configuration.')
