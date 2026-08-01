import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from oms_hub.anki.ankiconnect import (
    AnkiConnectActionError,
    AnkiConnectClient,
    AnkiConnectProtocolError,
    AnkiConnectUnavailable,
    UnsafeAnkiConnectURL,
)


def _run_client(
    handler: Callable[[httpx.Request], httpx.Response],
    operation: Callable[[AnkiConnectClient], Any],
    *,
    url: str = "http://127.0.0.1:8765",
) -> Any:
    async def scenario() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            client = AnkiConnectClient(url, http=http)
            return await operation(client)

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method_name", "arguments", "action", "params", "result", "expected"),
    [
        ("version", (), "version", {}, 6, 6),
        ("get_active_profile", (), "getActiveProfile", {}, "OMS NUC", "OMS NUC"),
        ("find_notes", ("deck:AnKing",), "findNotes", {"query": "deck:AnKing"}, [11], [11]),
        ("notes_info", ([11],), "notesInfo", {"notes": [11]}, [{"noteId": 11}], [{"noteId": 11}]),
        ("find_cards", ("nid:11",), "findCards", {"query": "nid:11"}, [21], [21]),
        ("cards_info", ([21],), "cardsInfo", {"cards": [21]}, [{"cardId": 21}], [{"cardId": 21}]),
        (
            "model_field_names",
            ("AnKingOverhaul",),
            "modelFieldNames",
            {"modelName": "AnKingOverhaul"},
            ["Text", "Extra"],
            ["Text", "Extra"],
        ),
        (
            "retrieve_media_file",
            ("image.png",),
            "retrieveMediaFile",
            {"filename": "image.png"},
            "aGVsbG8=",
            "aGVsbG8=",
        ),
        (
            "add_tags",
            ([11], ["lecture::03"]),
            "addTags",
            {"notes": [11], "tags": "lecture::03"},
            None,
            None,
        ),
        (
            "remove_tags",
            ([11], ["old::tag"]),
            "removeTags",
            {"notes": [11], "tags": "old::tag"},
            None,
            None,
        ),
        (
            "add_notes",
            ([{"deckName": "custom", "fields": {"Text": "value"}}],),
            "addNotes",
            {"notes": [{"deckName": "custom", "fields": {"Text": "value"}}]},
            [31],
            [31],
        ),
        ("sync", (), "sync", {}, None, None),
    ],
)
def test_ankiconnect_methods_emit_v6_contract(
    method_name: str,
    arguments: tuple[Any, ...],
    action: str,
    params: dict[str, Any],
    result: Any,
    expected: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "action": action,
            "version": 6,
            "params": params,
        }
        return httpx.Response(200, json={"result": result, "error": None})

    async def operation(client: AnkiConnectClient) -> Any:
        return await getattr(client, method_name)(*arguments)

    assert _run_client(handler, operation) == expected


def test_client_sends_requests_to_custom_loopback_port() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "http"
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 8766
        return httpx.Response(200, json={"result": 6, "error": None})

    async def version(client: AnkiConnectClient) -> int:
        return await client.version()

    assert (
        _run_client(
            handler,
            version,
            url="http://127.0.0.1:8766",
        )
        == 6
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8765",
        "http://192.168.1.10:8765",
        "https://127.0.0.1:8765",
        "http://user@127.0.0.1:8765",
    ],
)
def test_client_rejects_non_loopback_url(url: str) -> None:
    with pytest.raises(UnsafeAnkiConnectURL, match="loopback"):
        AnkiConnectClient(url)


def test_ankiconnect_rejects_malformed_or_old_protocol() -> None:
    async def version(client: AnkiConnectClient) -> int:
        return await client.version()

    with pytest.raises(AnkiConnectProtocolError, match="version 6"):
        _run_client(
            lambda request: httpx.Response(
                200, json={"result": 5, "error": None}
            ),
            version,
        )
    with pytest.raises(AnkiConnectProtocolError, match="envelope"):
        _run_client(
            lambda request: httpx.Response(200, json={"result": 6}),
            version,
        )


def test_ankiconnect_classifies_action_and_network_errors() -> None:
    async def find_notes(client: AnkiConnectClient) -> list[int]:
        return await client.find_notes("private query")

    with pytest.raises(AnkiConnectActionError, match="collection unavailable"):
        _run_client(
            lambda request: httpx.Response(
                200,
                json={"result": None, "error": "collection unavailable"},
            ),
            find_notes,
        )

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(AnkiConnectUnavailable, match="unavailable"):
        _run_client(unavailable, find_notes)


def test_add_notes_preserves_partial_duplicate_rejections() -> None:
    async def add_notes(client: AnkiConnectClient) -> list[int | None]:
        return await client.add_notes(
            [
                {"deckName": "custom", "fields": {"Text": "new"}},
                {"deckName": "custom", "fields": {"Text": "duplicate"}},
            ]
        )

    result = _run_client(
        lambda request: httpx.Response(
            200,
            json={"result": [31, None], "error": None},
        ),
        add_notes,
    )

    assert result == [31, None]
