import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from oms_anki_agent.ankiconnect import (
    AnkiConnectActionError,
    AnkiConnectClient,
    AnkiConnectProtocolError,
    AnkiConnectUnavailable,
)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    url: str = "http://127.0.0.1:8765",
) -> AnkiConnectClient:
    return AnkiConnectClient(
        url=url,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    ("method_name", "arguments", "action", "params", "result", "expected"),
    [
        ("version", (), "version", {}, 6, 6),
        (
            "find_notes",
            ('deck:"Anking Step Deck"',),
            "findNotes",
            {"query": 'deck:"Anking Step Deck"'},
            [11, 22],
            [11, 22],
        ),
        (
            "notes_info",
            ([11, 22],),
            "notesInfo",
            {"notes": [11, 22]},
            [{"noteId": 11}, {"noteId": 22}],
            [{"noteId": 11}, {"noteId": 22}],
        ),
        (
            "model_field_names",
            ("AnKingOverhaul (OMS_II_Extra/JCBrooks)",),
            "modelFieldNames",
            {"modelName": "AnKingOverhaul (OMS_II_Extra/JCBrooks)"},
            ["Text", "Extra"],
            ["Text", "Extra"],
        ),
        (
            "retrieve_media_file",
            ("anemia.png",),
            "retrieveMediaFile",
            {"filename": "anemia.png"},
            "aGVsbG8=",
            "aGVsbG8=",
        ),
        (
            "store_media_file",
            ("anemia.png", "aGVsbG8="),
            "storeMediaFile",
            {"filename": "anemia.png", "data": "aGVsbG8="},
            "anemia.png",
            "anemia.png",
        ),
        (
            "add_tags",
            ([11, 22], ["lecture-tag"]),
            "addTags",
            {"notes": [11, 22], "tags": "lecture-tag"},
            None,
            None,
        ),
        (
            "add_notes",
            ([{"deckName": "custom", "fields": {"Text": "value"}}],),
            "addNotes",
            {"notes": [{"deckName": "custom", "fields": {"Text": "value"}}]},
            [33],
            [33],
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

    client = _client(handler)

    assert getattr(client, method_name)(*arguments) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8765",
        "http://192.168.1.10:8765",
        "https://127.0.0.1:8765",
        "http://127.0.0.1:9999",
    ],
)
def test_ankiconnect_rejects_non_loopback_or_nonstandard_urls(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        _client(lambda request: httpx.Response(200), url=url)


def test_ankiconnect_rejects_old_protocol_version() -> None:
    client = _client(
        lambda request: httpx.Response(200, json={"result": 5, "error": None})
    )

    with pytest.raises(AnkiConnectProtocolError, match="version 6"):
        client.version()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"result": 6}),
        httpx.Response(200, json={"result": 6, "error": None, "extra": True}),
    ],
)
def test_ankiconnect_rejects_malformed_responses(response: httpx.Response) -> None:
    client = _client(lambda request: response)

    with pytest.raises(AnkiConnectProtocolError):
        client.version()


def test_ankiconnect_surfaces_action_errors_without_echoing_request_data() -> None:
    client = _client(
        lambda request: httpx.Response(
            200,
            json={"result": None, "error": "collection unavailable"},
        )
    )

    with pytest.raises(AnkiConnectActionError, match="collection unavailable"):
        client.find_notes("private-query")


def test_ankiconnect_classifies_network_and_http_failures_as_unavailable() -> None:
    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(AnkiConnectUnavailable):
        _client(network_failure).version()
    with pytest.raises(AnkiConnectUnavailable):
        _client(lambda request: httpx.Response(503)).version()
