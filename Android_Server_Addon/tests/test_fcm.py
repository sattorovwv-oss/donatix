import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from donatix_android_extension.fcm import InvalidToken, Sender


def key_file(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "fake-service-account.json"
    path.write_text(json.dumps({"type": "service_account", "project_id": "test-project",
        "private_key_id": "test-key-id", "client_email": "fake@test-project.iam.gserviceaccount.com",
        "private_key": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode()}))
    return path


def test_sender_uses_explicit_key_fixed_endpoints_and_data_only_payload(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            assert request.url.path == "/token"
            return httpx.Response(200, json={"access_token": "test-access-token", "expires_in": 3600})
        assert str(request.url) == "https://fcm.googleapis.com/v1/projects/test-project/messages:send"
        payload = json.loads(request.content)["message"]
        assert payload["data"]["binding"] == "session-binding"
        assert "notification" not in payload
        return httpx.Response(200, json={"name": "test-message"})
    sender = Sender(key_file(tmp_path), "test-project", transport=httpx.MockTransport(handler))
    sender("fake-registration-token", {"notification_id": 4, "binding": "session-binding"})
    sender("fake-registration-token", {"notification_id": 5, "binding": "session-binding"})
    assert len(calls) == 3  # The access token is reused, not requested per notification.


def test_sender_rejects_wrong_project_and_revoked_registration(tmp_path):
    path = key_file(tmp_path)
    with pytest.raises(ValueError):
        Sender(path, "different-project")
    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "test-access-token", "expires_in": 3600})
        return httpx.Response(404, json={"error": {"details": [{"errorCode": "UNREGISTERED"}]}})
    sender = Sender(path, "test-project", transport=httpx.MockTransport(handler))
    with pytest.raises(InvalidToken):
        sender("fake-registration-token", {"notification_id": 1})
