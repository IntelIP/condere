"""Read and rotate one Google Secret Manager secret from Cloud Run (metadata-server identity).

The identity needs `secretmanager.versions.access`, `.add` and `.destroy` on that secret only.
"""

import base64
import json
import re
import time

import httpx

METADATA_TOKEN = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
API = "https://secretmanager.googleapis.com/v1"
SECRET = re.compile(r"projects/[a-z0-9-]+/secrets/[A-Za-z0-9_-]+")


class SecretRecord:
    """A JSON document kept as the latest enabled version of a secret."""

    def __init__(self, secret: str, *, transport=None):
        if not SECRET.fullmatch(secret):
            raise ValueError("Name the secret as projects/<project>/secrets/<secret>.")
        self.secret = secret
        self.client = httpx.Client(timeout=30, follow_redirects=False, transport=transport)
        self._token, self._expires = None, 0.0

    def _headers(self) -> dict:
        if self._token is None or time.monotonic() > self._expires:
            response = self.client.get(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
            response.raise_for_status()
            token = response.json()
            self._token = token["access_token"]
            self._expires = time.monotonic() + token["expires_in"] - 60
        return {"Authorization": f"Bearer {self._token}"}

    def load(self) -> dict | None:
        response = self.client.get(
            f"{API}/{self.secret}/versions/latest:access", headers=self._headers()
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return json.loads(base64.b64decode(response.json()["payload"]["data"]))

    def save(self, record: dict) -> None:
        """Add the record as the new latest version, then destroy the versions it replaces."""
        data = base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode()
        added = self.client.post(
            f"{API}/{self.secret}:addVersion",
            headers=self._headers(),
            json={"payload": {"data": data}},
        )
        added.raise_for_status()
        current = added.json()["name"]
        listed = self.client.get(
            f"{API}/{self.secret}/versions",
            headers=self._headers(),
            params={"filter": "state:ENABLED"},
        )
        listed.raise_for_status()
        for version in listed.json().get("versions", []):
            if version["name"] != current:
                # A rotated refresh token is spent; keep no readable copy of it.
                self.client.post(
                    f"{API}/{version['name']}:destroy", headers=self._headers(), json={}
                ).raise_for_status()

    def close(self) -> None:
        self.client.close()
