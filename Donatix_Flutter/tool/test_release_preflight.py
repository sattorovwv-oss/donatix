"""Isolated build/readiness regressions; no real Firebase files or network."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('release_preflight', Path(__file__).with_name('release_preflight.py'))
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class ReleaseChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.addCleanup(patch.stopall)
        patch.object(preflight, 'ROOT', self.root).start()
        patch.dict(os.environ, {'DONATIX_URL': 'https://donatix.example'}).start()
        self.opener = patch.object(preflight.urllib.request, 'build_opener').start()

    def android_inputs(self, package='tj.donatix.app'):
        app = self.root / 'android/app'
        app.mkdir(parents=True)
        (app.parent / 'key.properties').write_text('keyAlias=unit-test\n')
        (app / 'google-services.json').write_text(json.dumps({
            'project_info': {'project_id': 'unit-test'},
            'client': [{'client_info': {'android_client_info': {'package_name': package}}}],
        }))

    def server_response(self, **changes):
        payload = {'ok': True, 'version': 4, 'platforms': ['android', 'ios'],
                   'google_enabled': False, 'apple_enabled': False}
        payload.update(changes)
        self.opener.return_value.open.return_value = io.BytesIO(json.dumps(payload).encode())

    def test_signed_build_never_calls_live_server(self):
        self.android_inputs()
        self.opener.side_effect = AssertionError('Build must not contact a live server')
        self.assertEqual(preflight.check('android'), [])
        self.opener.assert_not_called()

    def test_missing_signing_input_still_blocks_build(self):
        self.android_inputs()
        (self.root / 'android/key.properties').unlink()
        self.assertTrue(any('signing key' in error for error in preflight.check('android')))
        self.opener.assert_not_called()

    def test_wrong_firebase_package_still_blocks_build(self):
        self.android_inputs('other.application')
        self.assertTrue(any('different Android package' in error for error in preflight.check('android')))

    def test_invalid_firebase_json_still_blocks_build(self):
        self.android_inputs()
        (self.root / 'android/app/google-services.json').write_text('invalid')
        self.assertTrue(any('missing or invalid' in error for error in preflight.check('android')))

    def test_ios_requires_owner_configuration_without_contacting_server(self):
        errors = preflight.check('ios')
        self.assertTrue(any('missing or invalid' in error for error in errors))
        self.opener.assert_not_called()

    def test_untrusted_origins_block_build_and_readiness(self):
        self.android_inputs()
        for origin in ('http://donatix.example', 'https://name:pass@donatix.example',
                       'https://donatix.example/api', 'https://donatix.example?key=value',
                       'https://donatix.example#part', 'https://donatix.example:bad', ''):
            with self.subTest(origin=origin), patch.dict(os.environ, {'DONATIX_URL': origin}):
                self.assertIn('DONATIX_URL must be an HTTPS origin', preflight.check('android'))
                self.assertIn('DONATIX_URL must be an HTTPS origin', preflight.check('ios', server_only=True))
        self.opener.assert_not_called()

    def test_readiness_needs_no_local_signing_or_firebase(self):
        self.server_response()
        self.assertEqual(preflight.check('android', server_only=True), [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.opener.return_value.open.assert_called_once()

    def test_missing_existing_api_does_not_recommend_site_modifications(self):
        self.opener.return_value.open.side_effect = urllib.error.HTTPError('https://donatix.example', 404, '', {}, None)
        errors = preflight.check('android', server_only=True)
        self.assertIn('HTTP 404', errors[0])
        self.assertNotIn('server_patch', errors[0])

    def test_android_accepts_existing_api_without_mobile_extension(self):
        required = {
            '/api/v1/me': 'get', '/api/v1/balance': 'get',
            '/api/v1/categories': 'get', '/api/v1/products': 'get',
            '/api/v1/products/{product_id}': 'get', '/api/v1/orders': 'post',
            '/api/v1/orders/{order_id}': 'get', '/api/v1/payments/methods': 'get',
            '/api/v1/payments': 'post', '/api/v1/payments/{payment_id}/receipt': 'post',
        }
        self.opener.return_value.open.side_effect = [
            urllib.error.HTTPError('https://donatix.example', 404, '', {}, None),
            io.BytesIO(json.dumps({'paths': {p: {m: {}} for p, m in required.items()}}).encode()),
            io.BytesIO(b'<form action="/login"><input name="csrf"></form>'),
        ]
        self.assertEqual(preflight.check('android', server_only=True), [])

    def test_ios_still_requires_its_mobile_server_support(self):
        self.opener.return_value.open.side_effect = urllib.error.HTTPError('https://donatix.example', 404, '', {}, None)
        self.assertIn('server_patch', preflight.check('ios', server_only=True)[0])

    def test_readiness_reports_server_failure(self):
        self.opener.return_value.open.side_effect = urllib.error.HTTPError('https://donatix.example', 503, '', {}, None)
        self.assertIn('HTTP 503', preflight.check('android', server_only=True)[0])

    def test_readiness_does_not_accept_redirects(self):
        self.opener.return_value.open.side_effect = urllib.error.HTTPError('https://donatix.example', 302, '', {}, None)
        self.assertIn('HTTP 302', preflight.check('android', server_only=True)[0])
        self.assertIsNone(preflight.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_invalid_responses_do_not_pass_readiness(self):
        for raw in (b'<html>sign in</html>', b'[]', b'{"ok": false}', b'null'):
            with self.subTest(raw=raw):
                self.opener.return_value.open.return_value = io.BytesIO(raw)
                self.assertTrue(preflight.check('android', server_only=True))

    def test_old_or_wrong_platform_server_does_not_pass_readiness(self):
        for changes in ({'version': 3}, {'version': '4'}, {'version': True},
                        {'platforms': ['ios']}, {'platforms': 'android'}):
            with self.subTest(changes=changes):
                self.server_response(**changes)
                self.assertTrue(preflight.check('android', server_only=True))

    def test_ios_google_login_requires_apple_in_readiness(self):
        self.server_response(google_enabled=True)
        self.assertTrue(any('Sign in with Apple' in error for error in preflight.check('ios', server_only=True)))
        self.server_response(google_enabled=True, apple_enabled=True)
        self.assertEqual(preflight.check('ios', server_only=True), [])

    def test_combined_legacy_check_still_checks_both(self):
        self.android_inputs()
        self.server_response()
        self.assertEqual(preflight.check('android', check_server=True), [])
        self.opener.return_value.open.assert_called_once()


if __name__ == '__main__':
    unittest.main()
