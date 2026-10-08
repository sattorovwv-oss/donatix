from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class InvalidToken(Exception):
    pass


class Sender:
    """Explicit credentials only; fixed Google endpoints; no ambient discovery."""

    def __init__(self, credentials_path="", expected_project="donatix-660fc", transport=None):
        self.transport = transport
        self.lock = threading.Lock()
        self.access_token, self.expires = None, 0
        self.configured = False
        if not credentials_path:
            return
        data = json.loads(Path(credentials_path).read_text())
        if data.get("type") != "service_account" or data.get("project_id") != expected_project:
            raise ValueError("Firebase service account must belong to the configured Android project")
        self.project = data["project_id"]
        self.email, self.key_id = data["client_email"], data["private_key_id"]
        self.key = serialization.load_pem_private_key(data["private_key"].encode(), password=None)
        if not isinstance(self.key, rsa.RSAPrivateKey):
            raise ValueError("An RSA service-account key is required")
        self.configured = True

    @staticmethod
    def encode(data):
        return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=").decode()

    def token(self, client):
        with self.lock:
            if self.access_token and self.expires > time.time() + 60:
                return self.access_token
            now = int(time.time())
            head = self.encode({"alg": "RS256", "typ": "JWT", "kid": self.key_id})
            body = self.encode({"iss": self.email, "scope": "https://www.googleapis.com/auth/firebase.messaging",
                                "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600})
            signing = (head + "." + body).encode()
            sig = base64.urlsafe_b64encode(self.key.sign(signing, padding.PKCS1v15(), hashes.SHA256())).rstrip(b"=").decode()
            reply = client.post("https://oauth2.googleapis.com/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": signing.decode() + "." + sig})
            reply.raise_for_status()
            data = reply.json()
            self.access_token = data["access_token"]
            self.expires = time.time() + int(data["expires_in"])
            return self.access_token

    def __call__(self, token, payload):
        if not self.configured:
            raise RuntimeError("FCM credentials have not been configured")
        with httpx.Client(timeout=10, follow_redirects=False, transport=self.transport) as client:
            reply = client.post(f"https://fcm.googleapis.com/v1/projects/{self.project}/messages:send",
                headers={"Authorization": "Bearer " + self.token(client)}, json={"message": {
                    "token": token, "data": {k: str(v) for k, v in payload.items()},
                    "android": {"priority": "high", "ttl": "3600s", "collapse_key": "dx-" + str(payload["notification_id"])}}})
            if reply.status_code == 401:
                self.expires = 0
            if reply.status_code >= 400:
                try:
                    details = reply.json().get("error", {}).get("details", [])
                except ValueError:
                    details = []
                if any(d.get("errorCode") == "UNREGISTERED" for d in details if isinstance(d, dict)):
                    raise InvalidToken()
                raise RuntimeError("FCM delivery failed, HTTP " + str(reply.status_code))
