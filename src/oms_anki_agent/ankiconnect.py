from collections.abc import Sequence
from typing import Any, cast

import httpx


class AnkiConnectError(RuntimeError):
    """Base class for safe AnkiConnect diagnostics."""


class AnkiConnectUnavailable(AnkiConnectError):
    """AnkiConnect could not be reached or returned an HTTP failure."""


class AnkiConnectProtocolError(AnkiConnectError):
    """AnkiConnect returned a malformed or unsupported response."""


class AnkiConnectActionError(AnkiConnectError):
    """AnkiConnect rejected a requested action."""


class AnkiConnectClient:
    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:8765",
        http: httpx.Client | None = None,
    ) -> None:
        if url != "http://127.0.0.1:8765":
            raise ValueError(
                "AnkiConnect must use the loopback URL http://127.0.0.1:8765"
            )
        self.url = url
        self.http = http or httpx.Client(timeout=30.0)

    def version(self) -> int:
        result = self._invoke("version", {})
        if isinstance(result, bool) or not isinstance(result, int):
            raise AnkiConnectProtocolError("AnkiConnect returned an invalid version")
        if result < 6:
            raise AnkiConnectProtocolError("AnkiConnect version 6 or newer is required")
        return cast(int, result)

    def find_notes(self, query: str) -> list[int]:
        return self._integer_list(self._invoke("findNotes", {"query": query}))

    def notes_info(self, note_ids: Sequence[int]) -> list[dict[str, Any]]:
        result = self._invoke("notesInfo", {"notes": list(note_ids)})
        if not isinstance(result, list) or not all(
            isinstance(item, dict) for item in result
        ):
            raise AnkiConnectProtocolError("AnkiConnect returned invalid note information")
        return cast(list[dict[str, Any]], result)

    def model_field_names(self, model_name: str) -> list[str]:
        result = self._invoke("modelFieldNames", {"modelName": model_name})
        if not isinstance(result, list) or not all(
            isinstance(item, str) for item in result
        ):
            raise AnkiConnectProtocolError("AnkiConnect returned invalid model fields")
        return cast(list[str], result)

    def retrieve_media_file(self, filename: str) -> str | None:
        result = self._invoke("retrieveMediaFile", {"filename": filename})
        if result is False:
            return None
        if not isinstance(result, str):
            raise AnkiConnectProtocolError("AnkiConnect returned invalid media data")
        return result

    def store_media_file(self, filename: str, data_base64: str) -> str:
        result = self._invoke(
            "storeMediaFile",
            {"filename": filename, "data": data_base64},
        )
        if not isinstance(result, str):
            raise AnkiConnectProtocolError("AnkiConnect returned an invalid media filename")
        return result

    def add_tags(self, note_ids: Sequence[int], tags: Sequence[str]) -> None:
        result = self._invoke(
            "addTags",
            {"notes": list(note_ids), "tags": " ".join(tags)},
        )
        if result is not None:
            raise AnkiConnectProtocolError("AnkiConnect returned an invalid addTags result")

    def add_notes(self, notes: Sequence[dict[str, Any]]) -> list[int]:
        return self._integer_list(
            self._invoke("addNotes", {"notes": list(notes)})
        )

    def sync(self) -> None:
        result = self._invoke("sync", {})
        if result is not None:
            raise AnkiConnectProtocolError("AnkiConnect returned an invalid sync result")

    def _invoke(self, action: str, params: dict[str, Any]) -> Any:
        try:
            response = self.http.post(
                self.url,
                json={"action": action, "version": 6, "params": params},
            )
        except httpx.RequestError as exc:
            raise AnkiConnectUnavailable("AnkiConnect is unavailable") from exc
        if response.status_code >= 400:
            raise AnkiConnectUnavailable(
                f"AnkiConnect returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AnkiConnectProtocolError(
                "AnkiConnect returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {"result", "error"}:
            raise AnkiConnectProtocolError(
                "AnkiConnect returned an invalid response envelope"
            )
        error = payload["error"]
        if error is not None:
            safe_error = str(error).strip()[:500] or "unknown action error"
            raise AnkiConnectActionError(f"AnkiConnect action failed: {safe_error}")
        return payload["result"]

    @staticmethod
    def _integer_list(result: Any) -> list[int]:
        if not isinstance(result, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in result
        ):
            raise AnkiConnectProtocolError("AnkiConnect returned an invalid ID list")
        return cast(list[int], result)
