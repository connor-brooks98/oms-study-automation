"""Small client for Clinical Notebook's shared external source API v1."""

import ipaddress
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

MAX_BYTES = 100_000_000


class ClinicalNotebookError(RuntimeError):
    """Safe operator message, never a raw provider response or credential."""

    def __init__(
        self, message: str, *, retryable: bool = False, invalidates_destination: bool = False
    ):
        super().__init__(message)
        self.retryable = retryable
        self.invalidates_destination = invalidates_destination


def normalize_endpoint(value: str) -> str:
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as error:
        raise ClinicalNotebookError("Enter a valid Clinical Notebook API URL.") from error
    if (
        not value
        or len(value) > 2048
        or any(c.isspace() for c in value)
        or "\\" in value
        or url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or "?" in value
        or "#" in value
        or url.path.rstrip("/") not in {"", "/api/external/v1"}
        or port == 0
    ):
        raise ClinicalNotebookError(
            "Use the API base URL without credentials, queries or extra paths."
        )
    local = url.hostname == "localhost"
    try:
        address = ipaddress.ip_address(url.hostname)
        local = address.is_loopback
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise ClinicalNotebookError("Choose a reachable Clinical Notebook installation.")
    except ValueError:
        pass
    if url.scheme == "http" and not local:
        raise ClinicalNotebookError("Use HTTPS for a Clinical Notebook on another computer.")
    return urlunsplit((url.scheme, url.netloc, "/api/external/v1", "", ""))


def validate_token(token: str) -> None:
    if not re.fullmatch(r"cn_int_[A-Za-z0-9_-]{24,512}", token):
        raise ClinicalNotebookError(
            "Use a named integration token from Clinical Notebook Connected Apps."
        )


def http_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(30, connect=5), follow_redirects=False, trust_env=False
    )


class SourceAPI:
    def __init__(self, client: httpx.Client, endpoint: str, token: str):
        self.client = client
        self.endpoint = normalize_endpoint(endpoint)
        validate_token(token)
        self.headers = {"Authorization": f"Bearer {token}"}

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.client.request(
                method, self.endpoint + path, headers=self.headers, follow_redirects=False, **kwargs
            )
        except httpx.HTTPError as error:
            raise ClinicalNotebookError(
                "Clinical Notebook is unavailable. Open it and check the connection, then Retry.",
                retryable=True,
            ) from error
        if response.is_redirect:
            raise ClinicalNotebookError(
                "The API redirected this request. Check the saved endpoint; "
                "no redirect was followed."
            )
        if response.status_code in {401, 403}:
            raise ClinicalNotebookError(
                "Connection revoked or permission denied. Check Connected Apps and reconnect.",
                invalidates_destination=True,
            )
        if response.status_code == 404:
            raise ClinicalNotebookError(
                "Notebook or import is inaccessible. Choose an authorized destination again.",
                invalidates_destination=True,
            )
        if response.status_code == 409:
            raise ClinicalNotebookError(
                "Source content changed or conflicts with a previous import. "
                "Review it in Clinical Notebook."
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise ClinicalNotebookError(
                "Clinical Notebook could not accept the request yet. Retry later.", retryable=True
            )
        if not response.is_success:
            raise ClinicalNotebookError(
                "Clinical Notebook rejected this source. "
                "Check its type, size and connection permissions."
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ClinicalNotebookError(
                "Clinical Notebook returned an unreadable receipt. Retry to reconcile.",
                retryable=True,
            ) from error
        if not isinstance(payload, dict):
            raise ClinicalNotebookError(
                "Clinical Notebook returned an unexpected response.", retryable=True
            )
        return payload

    def notebooks(self, cursor: str = "") -> dict[str, Any]:
        return self.request("GET", "/notebooks", params={"limit": 100, "cursor": cursor or "0"})

    def capabilities(self) -> dict[str, Any]:
        result = self.request("GET", "/capabilities")
        if result.get("api_version") != "1" or "file" not in result.get("input_types", []):
            raise ClinicalNotebookError(
                "This endpoint does not support source API v1 file imports."
            )
        return result

    def upload(self, notebook_id: str, item: dict[str, Any]) -> dict[str, Any]:
        with Path(item["path"]).open("rb") as stream:
            result = self.request(
                "POST",
                f"/notebooks/{quote(notebook_id, safe='')}/sources/file",
                data={
                    key: str(item[key])
                    for key in ("title", "idempotency_key", "external_document_id", "sha256")
                }
                | {"metadata": json.dumps(item["metadata"], separators=(",", ":"))},
                files={"file": (item["filename"], stream, item["media_type"])},
            )
        return self.receipt(result, notebook_id, item["sha256"])

    def check(self, notebook_id: str, item: dict[str, Any]) -> dict[str, Any]:
        result = self.request("GET", f"/imports/{quote(item['receipt']['id'], safe='')}")
        receipt = self.receipt(result, notebook_id, item["sha256"])
        if receipt["id"] != item["receipt"]["id"] or (
            item["receipt"].get("source_id")
            and receipt["source_id"] != item["receipt"]["source_id"]
        ):
            raise ClinicalNotebookError("Clinical Notebook returned a different import receipt.")
        return receipt

    @staticmethod
    def receipt(result: dict[str, Any], notebook_id: str, digest: str) -> dict[str, Any]:
        if (
            result.get("notebook_id") != notebook_id
            or result.get("sha256") != digest
            or not isinstance(result.get("id"), str)
            or not result["id"]
            or len(result["id"]) > 200
            or result.get("status") not in {"pending", "accepted", "ready", "needs_attention"}
            or (
                result.get("status") in {"accepted", "ready"}
                and (not isinstance(result.get("source_id"), str) or not result["source_id"])
            )
            or not isinstance(result.get("warnings", []), list)
        ):
            raise ClinicalNotebookError(
                "Receipt did not match the source and destination. Retry to reconcile.",
                retryable=True,
            )
        return {
            key: result.get(key)
            for key in ("id", "source_id", "notebook_id", "sha256", "status", "warnings")
        }
