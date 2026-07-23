import time

import httpx

from oms_hub.security.secret_store import SecretStore


class PanoptoAuthenticationError(RuntimeError):
    pass


class PanoptoTokenProvider:
    def __init__(
        self,
        tenant_url: str,
        client_id: str,
        secrets: SecretStore,
        http: httpx.Client | None = None,
    ):
        self.tenant_url = tenant_url.rstrip("/")
        self.client_id = client_id
        self.secrets = secrets
        self.http = http or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._expires_at = 0.0

    def access_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at - 60:
            return self._token
        secret = self.secrets.get("panopto-client-secret")
        if not self.client_id or not secret:
            raise PanoptoAuthenticationError("Panopto client credentials are not configured")
        try:
            response = self.http.post(
                f"{self.tenant_url}/Panopto/oauth2/connect/token",
                auth=(self.client_id, secret),
                data={"grant_type": "client_credentials", "scope": "api"},
            )
        except httpx.RequestError as error:
            raise PanoptoAuthenticationError(
                "Panopto authentication service is unavailable"
            ) from error
        if response.status_code in {400, 401, 403}:
            raise PanoptoAuthenticationError("Panopto client credentials were rejected")
        try:
            response.raise_for_status()
            payload = response.json()
            access_token = payload["access_token"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise PanoptoAuthenticationError(
                "Panopto authentication returned an invalid response"
            ) from error
        if not isinstance(access_token, str) or not access_token:
            raise PanoptoAuthenticationError(
                "Panopto authentication returned an invalid response"
            )
        self._token = access_token
        expires_in = payload.get("expires_in", 300)
        try:
            lifetime = max(int(expires_in), 60)
        except (TypeError, ValueError):
            lifetime = 300
        self._expires_at = time.monotonic() + lifetime
        return access_token
