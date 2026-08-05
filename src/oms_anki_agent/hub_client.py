from typing import Any, BinaryIO, cast
from uuid import UUID

import httpx
from pydantic import ValidationError

from oms_anki_agent.snapshot import PreparedSnapshot, SnapshotUpload
from oms_hub.anki.contracts import AgentCommand, AgentHeartbeat, EnvelopeReceipt
from oms_hub.security.secret_store import SecretStore


class HubClientError(RuntimeError):
    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


class HubUnavailable(HubClientError):
    """The private Hub endpoint could not be reached."""


class HubAuthenticationError(HubClientError):
    """The Hub bearer credential is missing or rejected."""


class HubProtocolError(HubClientError):
    """The Hub returned an invalid agent response."""


class HubClient:
    def __init__(
        self,
        *,
        hub_url: str,
        agent_id: str,
        token_key: str,
        secrets: SecretStore,
        http: httpx.Client | None = None,
    ) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.agent_id = agent_id
        self.token_key = token_key
        self.secrets = secrets
        self.http = http or httpx.Client(timeout=30.0)

    def health(self) -> dict[str, str]:
        return self._object(self._request("GET", "/agent/v1/health"))

    def post_heartbeat(self, heartbeat: AgentHeartbeat) -> dict[str, str]:
        return self._object(
            self._request(
                "POST",
                "/agent/v1/heartbeat",
                json=heartbeat.model_dump(mode="json"),
            )
        )

    def next_command(self) -> AgentCommand | None:
        response = self._request("GET", "/agent/v1/commands/next")
        if response.status_code == 204:
            return None
        try:
            return AgentCommand.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise HubProtocolError(
                "Study Hub returned an invalid agent command",
                transient=False,
            ) from exc

    def upload_snapshot(
        self,
        command_id: UUID,
        snapshot: SnapshotUpload,
        ) -> dict[str, str]:
        if isinstance(snapshot, PreparedSnapshot):
            with snapshot.payload_path.open("rb") as stream:
                return self._object(
                    self._request(
                        "POST",
                        f"/agent/v1/commands/{command_id}/snapshot",
                        content=stream,
                        content_length=snapshot.payload_path.stat().st_size,
                    )
                )
        return self._object(
            self._request(
                "POST",
                f"/agent/v1/commands/{command_id}/snapshot",
                json=snapshot.model_dump(mode="json"),
            )
        )

    def upload_receipt(self, command_id: UUID, receipt: EnvelopeReceipt) -> dict[str, str]:
        return self._object(self._request(
            "POST", f"/agent/v1/commands/{command_id}/receipt", json=receipt.model_dump(mode="json")
        ))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        content: BinaryIO | None = None,
        content_length: int | None = None,
    ) -> httpx.Response:
        token = self.secrets.get(self.token_key)
        if not token:
            raise HubAuthenticationError(
                "Hub bearer token is missing from Keychain",
                transient=False,
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "X-OMS-Agent-ID": self.agent_id,
        }
        if content is not None:
            headers["Content-Type"] = "application/json"
            if content_length is None:
                raise ValueError("streamed Hub requests require a content length")
            headers["Content-Length"] = str(content_length)
        try:
            response = self.http.request(
                method,
                self.hub_url + path,
                headers=headers,
                json=json,
                content=content,
            )
        except httpx.RequestError as exc:
            raise HubUnavailable("Study Hub is unavailable", transient=True) from exc
        if response.status_code in {401, 403}:
            raise HubAuthenticationError(
                "Study Hub rejected the agent credential",
                transient=False,
            )
        if response.status_code >= 500:
            raise HubUnavailable(
                f"Study Hub returned HTTP {response.status_code}",
                transient=True,
            )
        if response.status_code >= 400:
            raise HubProtocolError(
                f"Study Hub rejected the request with HTTP {response.status_code}",
                transient=False,
            )
        return response

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, str]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise HubProtocolError(
                "Study Hub returned invalid JSON",
                transient=False,
            ) from exc
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in payload.items()
        ):
            raise HubProtocolError(
                "Study Hub returned an invalid response",
                transient=False,
            )
        return cast(dict[str, str], payload)
