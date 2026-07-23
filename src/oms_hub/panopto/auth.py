import secrets as secure_random
import threading
import time
from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from oms_hub.security.secret_store import SecretStore

CLIENT_SECRET_KEY = "panopto-client-secret"
REFRESH_TOKEN_KEY = "panopto-refresh-token"
AUTHORIZATION_STATE_KEY = "panopto-oauth-state"
AUTHORIZATION_SCOPE = "openid api offline_access"
AUTHORIZATION_STATE_LIFETIME_SECONDS = 10 * 60


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
        self._lock = threading.RLock()

    def connected(self) -> bool:
        return bool(self.secrets.get(REFRESH_TOKEN_KEY))

    def authorization_url(self, redirect_uri: str) -> str:
        self._configured_client_secret()
        with self._lock:
            state = secure_random.token_urlsafe(32)
            self.secrets.set(
                AUTHORIZATION_STATE_KEY,
                f"{int(time.time())}:{state}",
            )
            query = urlencode(
                {
                    "client_id": self.client_id,
                    "response_type": "code",
                    "redirect_uri": redirect_uri,
                    "scope": AUTHORIZATION_SCOPE,
                    "state": state,
                }
            )
        return f"{self.tenant_url}/Panopto/oauth2/connect/authorize?{query}"

    def complete_authorization(
        self,
        code: str,
        state: str,
        redirect_uri: str,
    ) -> None:
        with self._lock:
            state_record = self.secrets.get(AUTHORIZATION_STATE_KEY)
            self.secrets.delete(AUTHORIZATION_STATE_KEY)
            issued_text, separator, expected_state = (state_record or "").partition(":")
            try:
                state_age = time.time() - int(issued_text)
            except ValueError:
                state_age = float("inf")
            state_is_current = (
                separator == ":"
                and 0 <= state_age <= AUTHORIZATION_STATE_LIFETIME_SECONDS
                and secure_random.compare_digest(expected_state, state)
            )
            if not state_is_current:
                raise PanoptoAuthenticationError(
                    "Panopto connection could not be verified; restart Connect Panopto"
                )
            secret = self._configured_client_secret()
            payload = self._request_token(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                auth=httpx.BasicAuth(self.client_id, secret),
            )
            self._accept_token_response(payload, require_refresh=True)

    def access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires_at - 60:
                return self._token
            secret = self._configured_client_secret()
            refresh_token = self.secrets.get(REFRESH_TOKEN_KEY)
            if not refresh_token:
                raise PanoptoAuthenticationError(
                    "Connect Panopto in the dashboard before validating or scanning"
                )
            payload = self._request_token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                    "client_secret": secret,
                }
            )
            self._accept_token_response(payload, require_refresh=False)
            if self._token is None:  # pragma: no cover - guarded by response validation
                raise PanoptoAuthenticationError(
                    "Panopto authentication returned an invalid response"
                )
            return self._token

    def disconnect(self) -> None:
        with self._lock:
            self.secrets.delete(REFRESH_TOKEN_KEY)
            self._token = None
            self._expires_at = 0.0
            self.secrets.delete(AUTHORIZATION_STATE_KEY)

    def _configured_client_secret(self) -> str:
        secret = self.secrets.get(CLIENT_SECRET_KEY)
        if not self.client_id or not secret:
            raise PanoptoAuthenticationError(
                "Panopto web application client ID and secret are not configured"
            )
        return secret

    def _request_token(
        self,
        data: Mapping[str, str],
        *,
        auth: httpx.Auth | None = None,
    ) -> object:
        try:
            token_url = f"{self.tenant_url}/Panopto/oauth2/connect/token"
            if auth is None:
                response = self.http.post(token_url, data=data)
            else:
                response = self.http.post(token_url, data=data, auth=auth)
        except httpx.RequestError as error:
            raise PanoptoAuthenticationError(
                "Panopto authentication service is unavailable"
            ) from error
        if response.status_code in {400, 401, 403}:
            raise PanoptoAuthenticationError(
                "Panopto connection was rejected; verify the web application client "
                "ID, secret, and redirect URL"
            )
        try:
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, TypeError, ValueError) as error:
            raise PanoptoAuthenticationError(
                "Panopto authentication returned an invalid response"
            ) from error

    def _accept_token_response(
        self,
        payload: object,
        *,
        require_refresh: bool,
    ) -> None:
        if not isinstance(payload, dict):
            raise PanoptoAuthenticationError(
                "Panopto authentication returned an invalid response"
            )
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not isinstance(access_token, str) or not access_token:
            raise PanoptoAuthenticationError(
                "Panopto authentication returned an invalid response"
            )
        if require_refresh and (
            not isinstance(refresh_token, str) or not refresh_token
        ):
            raise PanoptoAuthenticationError(
                "Panopto did not grant offline access; verify the client type and reconnect"
            )
        if isinstance(refresh_token, str) and refresh_token:
            self.secrets.set(REFRESH_TOKEN_KEY, refresh_token)
        self._token = access_token
        expires_in = payload.get("expires_in", 300)
        try:
            lifetime = max(int(expires_in), 60)
        except (TypeError, ValueError):
            lifetime = 300
        self._expires_at = time.monotonic() + lifetime
