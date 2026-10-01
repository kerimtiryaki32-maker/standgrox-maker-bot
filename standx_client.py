"""Minimal StandX HTTP client used by the dashboard. Keep credentials in .env."""
import base64
import json
import os
import time
import uuid

import requests
from dotenv import load_dotenv
from nacl.signing import SigningKey

load_dotenv()


class StandXClient:
    def __init__(self, token=None, sign_key_hex=None):
        self.base_url = "https://perps.standx.com"
        self.token = (os.getenv("STANDX_TOKEN", "") if token is None else token).strip()
        sign_key_hex = (os.getenv("STANDX_SIGN_KEY_HEX", "") if sign_key_hex is None else sign_key_hex).strip()
        self.session_id = os.getenv("STANDX_SESSION_ID", "").strip() or str(uuid.uuid4())
        if not self.token:
            raise ValueError("STANDX_TOKEN is missing from .env")
        if not sign_key_hex:
            raise ValueError("STANDX_SIGN_KEY_HEX is missing from .env")
        self.signing_key = SigningKey(bytes.fromhex(sign_key_hex))

    def _headers(self):
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}

    def _signed_headers(self, body):
        version, request_id, timestamp = "v1", str(uuid.uuid4()), int(time.time() * 1000)
        message = f"{version},{request_id},{timestamp},{body}".encode("utf-8")
        signature = base64.b64encode(self.signing_key.sign(message).signature).decode("ascii")
        return {**self._headers(), "x-request-sign-version": version,
                "x-request-id": request_id, "x-request-timestamp": str(timestamp),
                "x-request-signature": signature, "x-session-id": self.session_id}

    def _get(self, path, params=None, auth=True):
        response = requests.get(self.base_url + path, params=params,
                                headers=self._headers() if auth else {}, timeout=(2, 4))
        response.raise_for_status()
        return response.json()

    def _post_signed(self, path, payload):
        body = json.dumps(payload, separators=(",", ":"))
        response = requests.post(self.base_url + path, data=body,
                                 headers=self._signed_headers(body), timeout=(3, 8))
        response.raise_for_status()
        return response.json()

    def get_mark_price(self, symbol):
        data = self._get("/api/query_symbol_price", {"symbol": symbol}, auth=False)
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        return float(data["mark_price"])
