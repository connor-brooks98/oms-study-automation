from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlsplit

import httpx


class AnkiConnectError(RuntimeError):
    """Base class for safe local AnkiConnect diagnostics."""


class UnsafeAnkiConnectURL(ValueError):
    """AnkiConnect was configured outside the required loopback boundary."""


class AnkiConnectUnavailable(AnkiConnectError):
    """AnkiConnect could not be reached or returned an HTTP failure."""


class AnkiConnectProtocolError(AnkiConnectError):
    """AnkiConnect returned a malformed or unsupported response."""


class AnkiConnectActionError(AnkiConnectError):
    """AnkiConnect rejected a requested action."""


class AnkiConnectClient:
    def __init__(
        self,
        url: str = "http://127.0.0.1:8765",
        *,
        http: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.url = _loopback_url(url)
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout_seconds)

    async def version(self) -> int:
        result = await self._invoke("version")
        if isinstance(result, bool) or not isinstance(result, int):
            raise AnkiConnectProtocolError(
                "AnkiConnect returned an invalid version"
            )
        if result < 6:
            raise AnkiConnectProtocolError(
                "AnkiConnect version 6 or newer is required"
            )
        return cast(int, result)

    async def get_active_profile(self) -> str:
        result = await self._invoke("getActiveProfile")
        if not isinstance(result, str) or not result.strip():
            raise AnkiConnectProtocolError(
                "AnkiConnect returned an invalid active profile"
            )
        return result.strip()

    async def find_notes(self, query: str) -> list[int]:
        return self._integer_list(
            await self._invoke("findNotes", query=query)
        )

    async def notes_info(
        self,
        note_ids: Sequence[int],
    ) -> list[dict[str, Any]]:
        return self._object_list(
            await self._invoke("notesInfo", notes=list(note_ids)),
            "note information",
        )

    async def find_cards(self, query: str) -> list[int]:
        return self._integer_list(
            await self._invoke("findCards", query=query)
        )

    async def cards_info(
        self,
        card_ids: Sequence[int],
    ) -> list[dict[str, Any]]:
        return self._object_list(
            await self._invoke("cardsInfo", cards=list(card_ids)),
            "card information",
        )

    async def model_field_names(self, model_name: str) -> list[str]:
        result = await self._invoke(
            "modelFieldNames",
            modelName=model_name,
        )
        if not isinstance(result, list) or not all(
            isinstance(item, str) for item in result
        ):
            raise AnkiConnectProtocolError(
                "AnkiConnect returned invalid model fields"
            )
        return cast(list[str], result)

    async def retrieve_media_file(self, filename: str) -> str | None:
        result = await self._invoke(
            "retrieveMediaFile",
            filename=filename,
        )
        if result is False:
            return None
        if not isinstance(result, str):
            raise AnkiConnectProtocolError(
                "AnkiConnect returned invalid media data"
            )
        return result

    async def store_media_file(self, filename: str, data_base64: str) -> str:
        result = await self._invoke(
            "storeMediaFile",
            filename=filename,
            data=data_base64,
        )
        if not isinstance(result, str) or result != filename:
            raise AnkiConnectProtocolError(
                "AnkiConnect returned an invalid media filename"
            )
        return result

    async def add_tags(
        self,
        note_ids: Sequence[int],
        tags: Sequence[str],
    ) -> None:
        await self._empty_result(
            "addTags",
            notes=list(note_ids),
            tags=" ".join(tags),
        )

    async def remove_tags(
        self,
        note_ids: Sequence[int],
        tags: Sequence[str],
    ) -> None:
        await self._empty_result(
            "removeTags",
            notes=list(note_ids),
            tags=" ".join(tags),
        )

    async def add_notes(
        self,
        notes: Sequence[dict[str, Any]],
    ) -> list[int | None]:
        result = self._optional_integer_list(
            await self._invoke("addNotes", notes=list(notes))
        )
        if len(result) != len(notes):
            raise AnkiConnectProtocolError(
                "AnkiConnect did not return one addNotes result per note"
            )
        return result

    async def sync(self) -> None:
        await self._empty_result("sync")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _empty_result(self, action: str, **params: object) -> None:
        result = await self._invoke(action, **params)
        if result is not None:
            raise AnkiConnectProtocolError(
                f"AnkiConnect returned an invalid {action} result"
            )

    async def _invoke(self, action: str, **params: object) -> Any:
        try:
            response = await self._http.post(
                self.url,
                json={
                    "action": action,
                    "version": 6,
                    "params": params,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AnkiConnectUnavailable(
                "AnkiConnect is unavailable"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise AnkiConnectProtocolError(
                "AnkiConnect returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "result",
            "error",
        }:
            raise AnkiConnectProtocolError(
                "AnkiConnect returned an invalid response envelope"
            )
        error = payload["error"]
        if error is not None:
            safe_error = str(error).strip()[:500] or "unknown action error"
            raise AnkiConnectActionError(
                f"AnkiConnect action failed: {safe_error}"
            )
        return payload["result"]

    @staticmethod
    def _integer_list(result: Any) -> list[int]:
        if not isinstance(result, list) or not all(
            isinstance(item, int)
            and not isinstance(item, bool)
            and item > 0
            for item in result
        ):
            raise AnkiConnectProtocolError(
                "AnkiConnect returned an invalid ID list"
            )
        return cast(list[int], result)

    @staticmethod
    def _optional_integer_list(result: Any) -> list[int | None]:
        if not isinstance(result, list) or not all(
            item is None
            or (
                isinstance(item, int)
                and not isinstance(item, bool)
                and item > 0
            )
            for item in result
        ):
            raise AnkiConnectProtocolError(
                "AnkiConnect returned an invalid addNotes result"
            )
        return cast(list[int | None], result)

    @staticmethod
    def _object_list(
        result: Any,
        description: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(result, list) or not all(
            isinstance(item, dict) for item in result
        ):
            raise AnkiConnectProtocolError(
                f"AnkiConnect returned invalid {description}"
            )
        return cast(list[dict[str, Any]], result)


def _loopback_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeAnkiConnectURL(
            "AnkiConnect must use a loopback URL with a valid port"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or not 1024 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeAnkiConnectURL(
            "AnkiConnect must use an HTTP loopback URL with an explicit "
            "port from 1024 through 65535"
        )
    return f"http://{parsed.hostname}:{port}"
