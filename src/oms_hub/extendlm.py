"""ExtendLM's public OAuth and narrowly scoped HTTP MCP upload client."""

import base64
import hashlib
import json
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

MCP_URL = "https://mcp.extendlm.com/mcp"
ISSUER = "https://api.extendlm.com"
OAUTH = f"{ISSUER}/rest/v1/mcp/oauth"
SCOPES = "notebooks:read sources:read sources:write exports:create"
TOOLS = frozenset(
    {
        "extension_status",
        "list_notebook_users",
        "get_notebooklm_capabilities",
        "list_notebooks",
        "list_notebook_sources",
        "prepare_pdf_source_upload",
        "prepare_file_source_upload",
        "add_pdf_source",
        "add_file_source",
    }
)
MAX_BYTES = 100 * 1024 * 1024


class ExtendLMError(RuntimeError):
    """Safe, actionable message; never includes provider responses or credentials."""


def checked(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise ExtendLMError("ExtendLM sign-in expired. Sign in again.")
    if response.status_code == 403:
        raise ExtendLMError("ExtendLM denied access. Check the approved upload permissions.")
    if response.status_code == 429:
        raise ExtendLMError("ExtendLM is rate limiting requests. Wait before trying again.")
    if not response.is_success:
        raise ExtendLMError(f"ExtendLM returned HTTP {response.status_code}. Try again later.")


def object_json(response: httpx.Response) -> dict[str, Any]:
    checked(response)
    try:
        result = response.json()
    except ValueError as error:
        raise ExtendLMError("ExtendLM returned an unreadable response.") from error
    if not isinstance(result, dict):
        raise ExtendLMError("ExtendLM returned an unexpected response.")
    return result


def register(client: httpx.Client, callback: str) -> str:
    result = object_json(
        client.post(
            f"{OAUTH}/register",
            json={
                "client_name": "OMS Study Hub",
                "redirect_uris": [callback],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": SCOPES,
            },
        )
    )
    client_id = result.get("client_id")
    if not isinstance(client_id, str) or not client_id or len(client_id) > 2048:
        raise ExtendLMError("ExtendLM did not register the Hub callback URL.")
    return client_id


def authorization(client_id: str, callback: str) -> tuple[str, dict[str, Any]]:
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    pending = {
        "state": state,
        "verifier": verifier,
        "callback": callback,
        "client_id": client_id,
        "expires": time.time() + 600,
    }
    url = f"{OAUTH}/authorize?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": callback,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
            "resource": MCP_URL,
        }
    )
    return url, pending


def exchange(client: httpx.Client, data: dict[str, str]) -> dict[str, Any]:
    result = object_json(client.post(f"{OAUTH}/token", data={**data, "resource": MCP_URL}))
    if (
        not isinstance(result.get("access_token"), str)
        or not result["access_token"]
        or str(result.get("token_type", "")).lower() != "bearer"
    ):
        raise ExtendLMError("ExtendLM did not return a usable authorization.")
    try:
        lifetime = float(result.get("expires_in", 3600))
    except (TypeError, ValueError) as error:
        raise ExtendLMError("ExtendLM returned an invalid token lifetime.") from error
    if not 0 < lifetime <= 365 * 86400:
        raise ExtendLMError("ExtendLM returned an invalid token lifetime.")
    result["expires_at"] = time.time() + lifetime
    return result


class MCPClient:
    def __init__(self, client: httpx.Client, token: str):
        self.client = client
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        self.sequence = 0

    def rpc(
        self, method: str, params: dict[str, Any], *, notification: bool = False
    ) -> dict[str, Any]:
        self.sequence += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self.sequence
        deadline = time.monotonic() + 180
        with self.client.stream("POST", MCP_URL, headers=self.headers, json=payload) as response:
            checked(response)
            if method == "initialize" and response.headers.get("mcp-session-id"):
                self.headers["MCP-Session-Id"] = response.headers["mcp-session-id"]
            if notification:
                return {}
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                parts: list[str] = []
                size = 0
                for line in response.iter_lines():
                    size += len(line)
                    if size > 10 * 1024 * 1024 or time.monotonic() > deadline:
                        raise ExtendLMError("ExtendLM response exceeded the upload wait limit.")
                    if line.startswith("data:"):
                        parts.append(line[5:].lstrip(" "))
                    elif not line and parts:
                        try:
                            item = json.loads("\n".join(parts))
                        except ValueError as error:
                            raise ExtendLMError("ExtendLM sent an unreadable event.") from error
                        parts = []
                        if isinstance(item, dict) and item.get("id") == self.sequence:
                            return self._result(item)
                raise ExtendLMError(
                    "ExtendLM disconnected before confirming the operation. Resume it."
                )
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 10 * 1024 * 1024 or time.monotonic() > deadline:
                    raise ExtendLMError("ExtendLM response exceeded the upload wait limit.")
            try:
                item = json.loads(content)
            except ValueError as error:
                raise ExtendLMError("ExtendLM sent unreadable JSON.") from error
            if not isinstance(item, dict) or item.get("id") != self.sequence:
                raise ExtendLMError("ExtendLM returned an unrelated response.")
            return self._result(item)

    @staticmethod
    def _result(item: dict[str, Any]) -> dict[str, Any]:
        if "error" in item:
            raise ExtendLMError(
                "ExtendLM could not complete the request. Check the extension and login."
            )
        result = item.get("result")
        if not isinstance(result, dict):
            raise ExtendLMError("ExtendLM returned an unexpected result.")
        return result

    def initialize(self) -> None:
        result = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "oms-study-hub", "version": "0.1.0"},
            },
        )
        version = result.get("protocolVersion")
        if version not in {"2025-11-25", "2025-06-18", "2025-03-26"}:
            raise ExtendLMError("ExtendLM negotiated an unsupported MCP version.")
        self.headers["MCP-Protocol-Version"] = version
        self.rpc("notifications/initialized", {}, notification=True)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOLS:
            raise ExtendLMError("This MCP operation is outside the Hub upload integration.")
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise ExtendLMError(
                "ExtendLM could not complete the operation. Check the browser, "
                "account, permissions, and notebook source limit; then resume."
            )
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    parsed = json.loads(block["text"])
                except (ValueError, KeyError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
        raise ExtendLMError("ExtendLM did not return a structured operation result.")

    def upload(self, prepared: dict[str, Any], path: Path) -> None:
        url = urlsplit(str(prepared.get("upload_url", "")))
        # A provider-issued capability is still an outbound URL trust boundary.
        if (
            url.scheme != "https"
            or url.hostname != "api.extendlm.com"
            or url.port not in {None, 443}
            or url.username
            or url.password
            or url.fragment
        ):
            raise ExtendLMError("ExtendLM returned an unrecognized upload destination.")
        headers = prepared.get("required_headers", {})
        if (
            prepared.get("method") != "PUT"
            or not isinstance(headers, dict)
            or set(headers) != {"Authorization", "Content-Type", "Content-Disposition"}
            or any(not isinstance(v, str) or "\r" in v or "\n" in v for v in headers.values())
            or not headers["Authorization"].startswith("Bearer ")
            or headers["Content-Type"] != prepared.get("expected_mime_type")
            or headers["Content-Type"] not in {"application/pdf", "application/octet-stream"}
        ):
            raise ExtendLMError("ExtendLM returned invalid upload headers.")
        size = path.stat().st_size
        if not 0 < size <= min(MAX_BYTES, int(prepared.get("max_bytes", 0))):
            raise ExtendLMError("File exceeds the ExtendLM upload limit.")
        if float(prepared.get("upload_expires_at_unix_ms", 0)) <= time.time() * 1000:
            raise ExtendLMError("The temporary upload expired before sending. Start a new upload.")
        with path.open("rb") as stream:
            response = self.client.put(prepared["upload_url"], headers=headers, content=stream)
        checked(response)
        if response.status_code != 201:
            raise ExtendLMError("ExtendLM did not confirm the raw file upload.")


@contextmanager
def http_client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=httpx.Timeout(180, connect=15), follow_redirects=False) as client:
        try:
            yield client
        except httpx.HTTPError as error:
            raise ExtendLMError(
                "ExtendLM connection interrupted. Check connection and resume."
            ) from error


def validate_filename(filename: str) -> str:
    if (
        not filename
        or len(filename) > 240
        or re.search(r"[\x00-\x1f\x7f/\\]", filename)
        or Path(filename).suffix.lower() not in {".pdf", ".txt", ".md", ".docx"}
    ):
        raise ExtendLMError(
            "Choose PDF slides or a TXT, Markdown, or DOCX transcript. "
            "Upload PowerPoint through Hub materials first to convert it to PDF."
        )
    return filename
