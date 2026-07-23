from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from oms_hub.panopto.auth import PanoptoAuthenticationError, PanoptoTokenProvider
from oms_hub.panopto.client import (
    CaptionNotReady,
    PanoptoClient,
    PanoptoPermissionError,
    PanoptoResponseError,
)


class MemorySecrets:
    def __init__(
        self,
        secret: str | None = "secret",
        refresh_token: str | None = "refresh-token",
    ):
        self.values = {}
        if secret is not None:
            self.values["panopto-client-secret"] = secret
        if refresh_token is not None:
            self.values["panopto-refresh-token"] = refresh_token

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def session_payload(caption_url: str | None = "https://captions.example/file.txt"):
    return {
        "Id": "8796399e-393c-4256-b6e4-b48f0150d156",
        "Name": "6H. MSK Shoulder",
        "CreatedDate": "2026-07-23T12:00:00Z",
        "Duration": 3600,
        "Folder": {"Name": "MSK"},
        "ContentLanguage": "English_USA",
        "Urls": {"CaptionDownloadUrl": caption_url},
    }


@respx.mock
def test_refresh_token_authentication_and_caption_download():
    token = respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "token", "expires_in": 3600}))
    session = respx.get(
        "https://lmunet.hosted.panopto.com/Panopto/api/v1/sessions/"
        "8796399e-393c-4256-b6e4-b48f0150d156"
    ).mock(return_value=httpx.Response(200, json=session_payload()))
    captions = respx.get("https://captions.example/file.txt").mock(
        return_value=httpx.Response(200, text="shoulder transcript")
    )
    tokens = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com", "client", MemorySecrets()
    )
    client = PanoptoClient("https://lmunet.hosted.panopto.com", tokens)

    value = client.get_session("8796399e-393c-4256-b6e4-b48f0150d156")
    assert client.download_captions(value.caption_download_url or "", 1024) == (
        b"shoulder transcript"
    )
    assert token.call_count == session.call_count == captions.call_count == 1


def test_authorization_url_uses_user_scopes_redirect_and_unpredictable_state():
    provider = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com",
        "client",
        MemorySecrets(refresh_token=None),
    )

    authorization_url = provider.authorization_url(
        "http://127.0.0.1:8765/panopto/oauth/callback"
    )
    parsed = urlparse(authorization_url)
    query = parse_qs(parsed.query)

    assert parsed.path == "/Panopto/oauth2/connect/authorize"
    assert query["client_id"] == ["client"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [
        "http://127.0.0.1:8765/panopto/oauth/callback"
    ]
    assert set(query["scope"][0].split()) == {"openid", "api", "offline_access"}
    assert len(query["state"][0]) >= 32


@respx.mock
def test_authorization_code_exchange_validates_state_and_saves_refresh_token():
    route = respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
            },
        )
    )
    secrets = MemorySecrets(refresh_token=None)
    provider = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com",
        "client",
        secrets,
    )
    redirect_uri = "http://127.0.0.1:8765/panopto/oauth/callback"
    state = parse_qs(urlparse(provider.authorization_url(redirect_uri)).query)["state"][0]

    provider.complete_authorization("authorization-code", state, redirect_uri)

    assert secrets.get("panopto-refresh-token") == "new-refresh-token"
    assert provider.access_token() == "access-token"
    request = route.calls[0].request
    body = parse_qs(request.content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["authorization-code"]
    assert body["redirect_uri"] == [redirect_uri]
    assert "client_id" not in body
    assert "client_secret" not in body
    assert request.headers["authorization"] == "Basic Y2xpZW50OnNlY3JldA=="


@respx.mock
def test_authorization_state_survives_provider_restart():
    route = respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
            },
        )
    )
    secrets = MemorySecrets(refresh_token=None)
    redirect_uri = "http://127.0.0.1:8765/panopto/oauth/callback"
    first_process = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com",
        "client",
        secrets,
    )
    state = parse_qs(
        urlparse(first_process.authorization_url(redirect_uri)).query
    )["state"][0]
    restarted_process = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com",
        "client",
        secrets,
    )

    restarted_process.complete_authorization(
        "authorization-code",
        state,
        redirect_uri,
    )

    assert route.call_count == 1
    assert secrets.get("panopto-oauth-state") is None
    assert secrets.get("panopto-refresh-token") == "new-refresh-token"


@respx.mock
def test_authorization_code_exchange_rejects_missing_or_mismatched_state():
    route = respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "not-used"}))
    provider = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com",
        "client",
        MemorySecrets(refresh_token=None),
    )
    redirect_uri = "http://127.0.0.1:8765/panopto/oauth/callback"
    provider.authorization_url(redirect_uri)

    with pytest.raises(PanoptoAuthenticationError, match="restart"):
        provider.complete_authorization("authorization-code", "wrong-state", redirect_uri)

    assert route.call_count == 0


@respx.mock
def test_missing_caption_url_is_waiting():
    respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "token", "expires_in": 3600}))
    respx.get(
        "https://lmunet.hosted.panopto.com/Panopto/api/v1/sessions/"
        "8796399e-393c-4256-b6e4-b48f0150d156"
    ).mock(return_value=httpx.Response(200, json=session_payload(None)))
    tokens = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com", "client", MemorySecrets()
    )
    client = PanoptoClient("https://lmunet.hosted.panopto.com", tokens)

    value = client.get_session("8796399e-393c-4256-b6e4-b48f0150d156")

    with pytest.raises(CaptionNotReady):
        client.download_captions(value.caption_download_url or "", 1024)


@respx.mock
def test_refreshed_token_is_cached_rotated_and_rejected_responses_are_sanitized():
    token_route = respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "token",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )
    )
    secrets = MemorySecrets("do-not-leak")
    provider = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com", "client", secrets
    )

    assert provider.access_token() == provider.access_token() == "token"
    assert token_route.call_count == 1
    assert secrets.get("panopto-refresh-token") == "rotated-refresh"

    token_route.mock(return_value=httpx.Response(401, text="do-not-leak token"))
    rejected = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com", "client", MemorySecrets("do-not-leak")
    )
    with pytest.raises(PanoptoAuthenticationError) as captured:
        rejected.access_token()
    assert "do-not-leak" not in str(captured.value)
    assert "token" not in str(captured.value).lower()


def test_missing_refresh_token_requires_browser_connection():
    provider = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com",
        "client",
        MemorySecrets(refresh_token=None),
    )

    with pytest.raises(PanoptoAuthenticationError, match="Connect Panopto"):
        provider.access_token()


def test_disconnect_removes_only_refresh_token_and_clears_cached_access():
    secrets = MemorySecrets()
    provider = PanoptoTokenProvider(
        "https://lmunet.hosted.panopto.com",
        "client",
        secrets,
    )

    provider.disconnect()

    assert secrets.get("panopto-refresh-token") is None
    assert secrets.get("panopto-client-secret") == "secret"
    assert provider.connected() is False


@respx.mock
def test_search_is_bounded_and_uses_verified_get_endpoint():
    respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "token", "expires_in": 3600}))
    route = respx.get(
        "https://lmunet.hosted.panopto.com/Panopto/api/v1/sessions/search"
    ).mock(
        side_effect=[
            httpx.Response(200, json={"Results": [session_payload()]}),
            httpx.Response(200, json={"Results": []}),
        ]
    )
    client = PanoptoClient(
        "https://lmunet.hosted.panopto.com",
        PanoptoTokenProvider(
            "https://lmunet.hosted.panopto.com", "client", MemorySecrets()
        ),
    )

    results = client.search_sessions("Shoulder", max_pages=3)

    assert len(results) == 1
    assert route.call_count == 2
    first_request = route.calls[0].request
    assert first_request.url.params["sortField"] == "CreatedDate"
    assert first_request.url.params["sortOrder"] == "Desc"
    assert first_request.url.params["pageNumber"] == "0"


@respx.mock
def test_client_rejects_permission_failure_html_and_oversize_caption():
    respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(return_value=httpx.Response(200, json={"access_token": "token", "expires_in": 3600}))
    client = PanoptoClient(
        "https://lmunet.hosted.panopto.com",
        PanoptoTokenProvider(
            "https://lmunet.hosted.panopto.com", "client", MemorySecrets()
        ),
    )
    caption = respx.get("https://captions.example/file.txt").mock(
        return_value=httpx.Response(403, text="token secret")
    )
    with pytest.raises(PanoptoPermissionError) as captured:
        client.download_captions("https://captions.example/file.txt", 1024)
    assert "token" not in str(captured.value).lower()
    assert "secret" not in str(captured.value).lower()

    caption.mock(
        return_value=httpx.Response(
            200,
            content=b"<html>login</html>",
            headers={"content-type": "text/html"},
        )
    )
    with pytest.raises(PanoptoResponseError):
        client.download_captions("https://captions.example/file.txt", 1024)

    caption.mock(return_value=httpx.Response(200, content=b"x" * 1025))
    with pytest.raises(PanoptoResponseError):
        client.download_captions("https://captions.example/file.txt", 1024)
