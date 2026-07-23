from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from oms_hub.panopto.auth import PanoptoAuthenticationError
from oms_hub.panopto.domain import PanoptoSession


class AccessTokenProvider(Protocol):
    def access_token(self) -> str: ...


class PanoptoPermissionError(RuntimeError):
    pass


class PanoptoResponseError(RuntimeError):
    pass


class CaptionNotReady(RuntimeError):
    pass


class PanoptoClient:
    def __init__(
        self,
        tenant_url: str,
        tokens: AccessTokenProvider,
        http: httpx.Client | None = None,
    ):
        self.tenant_url = tenant_url.rstrip("/")
        self.tokens = tokens
        self.http = http or httpx.Client(timeout=30.0, follow_redirects=False)

    def search_sessions(
        self,
        search_query: str,
        max_pages: int = 3,
    ) -> list[PanoptoSession]:
        if max_pages < 1:
            return []
        sessions: list[PanoptoSession] = []
        for page_number in range(max_pages):
            payload = self._get_json(
                f"{self.tenant_url}/Panopto/api/v1/sessions/search",
                params={
                    "searchQuery": search_query,
                    "sortField": "CreatedDate",
                    "sortOrder": "Desc",
                    "pageNumber": str(page_number),
                },
            )
            raw_results = payload.get("Results", payload.get("results", []))
            if not isinstance(raw_results, list):
                raise PanoptoResponseError("Panopto session search returned invalid results")
            if not raw_results:
                break
            for raw in raw_results:
                if isinstance(raw, dict):
                    sessions.append(self._parse_session(raw))
        return sessions

    def get_session(self, session_id: str) -> PanoptoSession:
        payload = self._get_json(
            f"{self.tenant_url}/Panopto/api/v1/sessions/{session_id}"
        )
        return self._parse_session(payload)

    def download_captions(self, download_url: str, max_bytes: int) -> bytes:
        if not download_url:
            raise CaptionNotReady("English (United States) captions are not ready")
        parsed = urlparse(download_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise PanoptoResponseError("Caption download URL is invalid")
        try:
            response = self.http.get(download_url)
        except httpx.RequestError as error:
            raise PanoptoResponseError("Caption download is unavailable") from error
        self._raise_sanitized_status(response, caption=True)
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            raise PanoptoResponseError("Caption download returned HTML")
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise PanoptoResponseError("Caption download exceeds the size limit")
            except ValueError as error:
                raise PanoptoResponseError(
                    "Caption download returned an invalid size"
                ) from error
        if len(response.content) > max_bytes:
            raise PanoptoResponseError("Caption download exceeds the size limit")
        return response.content

    def _get_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.http.get(
                url,
                headers={"Authorization": f"Bearer {self.tokens.access_token()}"},
                params=params,
            )
        except PanoptoAuthenticationError:
            raise
        except httpx.RequestError as error:
            raise PanoptoResponseError("Panopto API is unavailable") from error
        self._raise_sanitized_status(response)
        try:
            payload = response.json()
        except ValueError as error:
            raise PanoptoResponseError("Panopto API returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise PanoptoResponseError("Panopto API returned an invalid response")
        return payload

    @staticmethod
    def _raise_sanitized_status(
        response: httpx.Response,
        *,
        caption: bool = False,
    ) -> None:
        if response.status_code == 401:
            if caption:
                raise PanoptoPermissionError("Caption download authorization failed")
            raise PanoptoAuthenticationError("Panopto credentials were rejected")
        if response.status_code == 403:
            raise PanoptoPermissionError("Panopto access was denied")
        if response.is_redirect:
            location = response.headers.get("location", "")
            if "/Pages/Auth/" in location:
                raise PanoptoAuthenticationError("Panopto authentication is required")
            raise PanoptoResponseError("Panopto returned an unexpected redirect")
        try:
            response.raise_for_status()
        except httpx.HTTPError as error:
            message = "Caption download failed" if caption else "Panopto API request failed"
            raise PanoptoResponseError(message) from error

    @staticmethod
    def _parse_session(payload: dict[str, Any]) -> PanoptoSession:
        try:
            raw_created = str(payload["CreatedDate"])
            created = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            folder = payload.get("Folder")
            folder_name = (
                str(folder.get("Name", ""))
                if isinstance(folder, dict)
                else str(folder or "")
            )
            urls = payload.get("Urls")
            caption_url = (
                urls.get("CaptionDownloadUrl") if isinstance(urls, dict) else None
            )
            return PanoptoSession(
                session_id=str(payload["Id"]),
                name=str(payload["Name"]),
                created_utc=created.astimezone(UTC),
                duration_seconds=float(payload.get("Duration", 0.0)),
                folder_name=folder_name,
                content_language=(
                    str(payload["ContentLanguage"])
                    if payload.get("ContentLanguage") is not None
                    else None
                ),
                caption_download_url=str(caption_url) if caption_url else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PanoptoResponseError("Panopto session data is invalid") from error
