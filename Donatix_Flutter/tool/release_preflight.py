#!/usr/bin/env python3
"""Check owner release inputs; never print keys or Firebase config contents."""
import argparse
import json
import os
import plistlib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = 'tj.donatix.app'


def check(platform, check_server=False):
    errors = []
    if platform == 'android':
        if not (ROOT / 'android/key.properties').is_file():
            errors.append('android/key.properties with owner upload signing key is missing')
        try:
            config = json.loads((ROOT / 'android/app/google-services.json').read_text())
            if not config.get('project_info', {}).get('project_id'):
                errors.append('Firebase project_id is missing')
            if not any(c.get('client_info', {}).get('android_client_info', {}).get('package_name') == BUNDLE_ID for c in config.get('client', [])):
                errors.append('Firebase configuration belongs to a different Android package')
        except (ValueError, OSError):
            errors.append('Owner android/app/google-services.json is missing or invalid')
    else:
        try:
            config = plistlib.loads((ROOT / 'ios/Runner/GoogleService-Info.plist').read_bytes())
            if config.get('BUNDLE_ID') != BUNDLE_ID:
                errors.append('Firebase configuration belongs to a different iOS bundle')
            for field in ('PROJECT_ID', 'GOOGLE_APP_ID', 'GCM_SENDER_ID', 'API_KEY'):
                if not config.get(field):
                    errors.append('iOS Firebase ' + field + ' is missing')
        except (ValueError, OSError, plistlib.InvalidFileException):
            errors.append('Owner ios/Runner/GoogleService-Info.plist is missing or invalid')
    if check_server:
        origin = os.environ.get('DONATIX_URL', 'https://donatix.tj').rstrip('/')
        url = urlsplit(origin)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path:
            errors.append('DONATIX_URL must be an HTTPS origin')
        else:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            try:
                request = urllib.request.Request(origin + '/api/v1/mobile/config', headers={'Accept':'application/json'})
                with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                    server = json.loads(response.read(1024 * 1024))
                if server.get('version', 0) < 4 or platform not in server.get('platforms', []):
                    errors.append('Deploy the version 4 mobile server extension first')
                if platform == 'ios' and server.get('google_enabled') and not server.get('apple_enabled'):
                    errors.append('Configure Sign in with Apple before releasing an iOS app with Google login')
            except (OSError, ValueError, TypeError):
                errors.append('Could not verify the live mobile server over HTTPS')
    return errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', choices=['android','ios'], default='android')
    parser.add_argument('--check-server', action='store_true')
    args = parser.parse_args()
    errors = check(args.platform, args.check_server)
    if errors:
        raise SystemExit('Release input check failed:\n- ' + '\n- '.join(errors))
    print('Release inputs present. Signing, device and payment acceptance still required.')
