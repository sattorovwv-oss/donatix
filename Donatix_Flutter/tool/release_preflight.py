#!/usr/bin/env python3
"""Validate build inputs; check a deployed API only when explicitly requested.

Compiling a signed app does not require a running server. Store publication and
end-to-end acceptance do. Never print keys or Firebase configuration contents.
"""
import argparse
import json
import os
import plistlib
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = 'tj.donatix.app'


def validate_origin(origin):
    try:
        url = urlsplit(origin)
        url.port
    except ValueError:
        return ['DONATIX_URL must be an HTTPS origin']
    if (url.scheme != 'https' or not url.hostname or url.username or
            url.password or url.query or url.fragment or url.path):
        return ['DONATIX_URL must be an HTTPS origin']
    return []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verify_server(platform, origin):
    endpoint = origin + '/api/v1/mobile/config'
    try:
        request = urllib.request.Request(endpoint, headers={'Accept': 'application/json'})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            server = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return ['Mobile API was not found (HTTP 404): ' + endpoint +
                    '. Install the server_patch extension on the existing server.']
        return ['Mobile API returned HTTP ' + str(error.code) + ': ' + endpoint]
    except (OSError, ValueError, TypeError):
        return ['Could not read a valid mobile server response over HTTPS: ' + endpoint]
    if not isinstance(server, dict) or server.get('ok') is not True:
        return ['Mobile API did not return a successful configuration: ' + endpoint]
    version = server.get('version')
    platforms = server.get('platforms')
    errors = []
    if (type(version) is not int or version < 4 or
            not isinstance(platforms, list) or platform not in platforms):
        errors.append('Deploy the version 4 mobile server extension for ' + platform + ' first')
    if platform == 'ios' and server.get('google_enabled') and not server.get('apple_enabled'):
        errors.append('Configure Sign in with Apple before releasing an iOS app with Google login')
    return errors


def check(platform, check_server=False, server_only=False):
    origin = os.environ.get('DONATIX_URL', 'https://donatix.tj').rstrip('/')
    origin_errors = validate_origin(origin)
    errors = list(origin_errors)
    if not server_only and platform == 'android':
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
    elif not server_only:
        try:
            config = plistlib.loads((ROOT / 'ios/Runner/GoogleService-Info.plist').read_bytes())
            if config.get('BUNDLE_ID') != BUNDLE_ID:
                errors.append('Firebase configuration belongs to a different iOS bundle')
            for field in ('PROJECT_ID', 'GOOGLE_APP_ID', 'GCM_SENDER_ID', 'API_KEY'):
                if not config.get(field):
                    errors.append('iOS Firebase ' + field + ' is missing')
        except (ValueError, OSError, plistlib.InvalidFileException):
            errors.append('Owner ios/Runner/GoogleService-Info.plist is missing or invalid')
    if (check_server or server_only) and not origin_errors:
        errors.extend(verify_server(platform, origin))
    return errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', choices=['android','ios'], default='android')
    parser.add_argument('--check-server', action='store_true')
    parser.add_argument('--server-only', action='store_true',
                        help='Check the deployed API without requiring local signing/Firebase files')
    args = parser.parse_args()
    errors = check(args.platform, args.check_server, args.server_only)
    if errors:
        raise SystemExit('Release input check failed:\n- ' + '\n- '.join(errors))
    if args.server_only:
        print('Mobile server API check passed for ' + args.platform + '. End-to-end acceptance still required.')
    elif args.check_server:
        print('Local release inputs and mobile server API checked. Device and payment acceptance still required.')
    else:
        print('Local release inputs present. Live server was not checked. Before store publication,')
        print('run release_preflight.py --platform ' + args.platform + ' --server-only and complete device/payment acceptance.')
