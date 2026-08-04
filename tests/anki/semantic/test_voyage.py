import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import numpy as np
import pytest

from oms_hub.anki.semantic.voyage import (
    VoyageEmbeddingClient,
    VoyageEmbeddingError,
)
from oms_hub.security.secret_store import (
    VOYAGE_API_KEY_SECRET,
    KeyringSecretStore,
)


class MemorySecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _response(
    vectors: list[list[float]],
    *,
    indices: list[int] | None = None,
    model: str = "voyage-4-large",
) -> dict[str, Any]:
    ordered_indices = indices or list(range(len(vectors)))
    return {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "embedding": vector,
                "index": index,
            }
            for vector, index in zip(vectors, ordered_indices, strict=True)
        ],
        "model": model,
        "usage": {"total_tokens": 12},
    }


def _embed(
    handler: Callable[[httpx.Request], httpx.Response],
    texts: list[str],
    *,
    input_type: str = "document",
    batch_size: int = 128,
    max_attempts: int = 3,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> np.ndarray:
    async def scenario() -> np.ndarray:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            client = VoyageEmbeddingClient(
                MemorySecrets({VOYAGE_API_KEY_SECRET: "voyage-secret"}),
                model="voyage-4-large",
                dimensions=3,
                batch_size=batch_size,
                max_attempts=max_attempts,
                retry_base_seconds=0.01,
                http=http,
                sleep=sleep or asyncio.sleep,
            )
            return await client.embed(texts, input_type=input_type)  # type: ignore[arg-type]

    return asyncio.run(scenario())


def test_embed_uses_document_contract_and_normalizes_float32() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer voyage-secret"
        assert request.url == "https://api.voyageai.com/v1/embeddings"
        assert request.headers["content-type"] == "application/json"
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload == {
            "input": ["alpha", "beta"],
            "model": "voyage-4-large",
            "input_type": "document",
            "truncation": True,
            "output_dimension": 3,
            "output_dtype": "float",
        }
        return httpx.Response(
            200,
            json=_response([[3.0, 4.0, 0.0], [0.0, 0.0, 2.0]]),
        )

    result = _embed(handler, ["alpha", "beta"])

    assert result.dtype == np.float32
    np.testing.assert_allclose(
        result,
        np.asarray([[0.6, 0.8, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32),
    )


def test_embed_batches_and_restores_response_index_order() -> None:
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        inputs = list(payload["input"])
        batches.append(inputs)
        if inputs == ["one", "two"]:
            return httpx.Response(
                200,
                json=_response(
                    [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
                    indices=[1, 0],
                ),
            )
        return httpx.Response(
            200,
            json=_response([[0.0, 0.0, 1.0]]),
        )

    result = _embed(handler, ["one", "two", "three"], batch_size=2)

    assert batches == [["one", "two"], ["three"]]
    np.testing.assert_array_equal(
        result,
        np.asarray(
            [
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float32,
        ),
    )


def test_embed_splits_large_payloads_before_calling_voyage() -> None:
    batches: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = list(json.loads(request.content)["input"])
        batches.append(len(inputs))
        return httpx.Response(
            200,
            json=_response([[1.0, 0.0, 0.0] for _ in inputs]),
        )

    result = _embed(
        handler,
        ["a" * 250_000, "b" * 250_000],
    )

    assert batches == [1, 1]
    assert result.shape == (2, 3)


def test_embed_query_contract_and_empty_input() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert payload["input_type"] == "query"
        return httpx.Response(
            200,
            json=_response([[1.0, 0.0, 0.0]]),
        )

    query = _embed(handler, ["mechanism"], input_type="query")
    empty = _embed(handler, [], input_type="document")

    np.testing.assert_array_equal(
        query,
        np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32),
    )
    assert empty.shape == (0, 3)
    assert empty.dtype == np.float32
    assert calls == 1


def test_embed_retries_rate_limit_then_succeeds() -> None:
    attempts = 0

    async def no_wait(_: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"request-id": "rate-limit-1"},
                json={"detail": "slow down"},
            )
        return httpx.Response(
            200,
            json=_response([[1.0, 0.0, 0.0]]),
        )

    result = _embed(handler, ["alpha"], sleep=no_wait)

    assert attempts == 2
    np.testing.assert_array_equal(
        result,
        np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32),
    )


@pytest.mark.parametrize(
    "payload",
    [
        _response([[1.0, 0.0]]),
        _response([[0.0, 0.0, 0.0]]),
        _response([[1.0, 0.0, float("nan")]]),
        _response([[1.0, 0.0, 0.0]], model="voyage-other"),
    ],
)
def test_embed_rejects_malformed_vectors(payload: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(payload, allow_nan=True).encode(),
            headers={"content-type": "application/json"},
        )

    with pytest.raises(VoyageEmbeddingError, match="invalid"):
        _embed(handler, ["alpha"])


def test_embed_errors_do_not_expose_api_key_or_input() -> None:
    with pytest.raises(VoyageEmbeddingError) as raised:
        _embed(
            lambda request: httpx.Response(
                401,
                headers={"request-id": "auth-1"},
                json={"detail": "voyage-secret private lecture text"},
            ),
            ["private lecture text"],
            max_attempts=1,
        )

    message = str(raised.value)
    assert "voyage-secret" not in message
    assert "private lecture text" not in message
    assert "auth-1" in message


def test_embed_uses_explicit_api_key_before_credential_manager() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer env-secret"
        return httpx.Response(
            200,
            json=_response([[1.0, 0.0, 0.0]]),
        )

    async def scenario() -> np.ndarray:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            client = VoyageEmbeddingClient(
                MemorySecrets({VOYAGE_API_KEY_SECRET: "keyring-secret"}),
                model="voyage-4-large",
                dimensions=3,
                api_key="env-secret",
                http=http,
            )
            return await client.embed(["alpha"], input_type="document")

    result = asyncio.run(scenario())

    np.testing.assert_array_equal(
        result,
        np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32),
    )


def test_voyage_secret_uses_keyring_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        "keyring.set_password",
        lambda service, key, value: values.__setitem__((service, key), value),
    )
    monkeypatch.setattr(
        "keyring.get_password",
        lambda service, key: values.get((service, key)),
    )
    monkeypatch.setattr(
        "keyring.delete_password",
        lambda service, key: values.pop((service, key)),
    )
    store = KeyringSecretStore("OMSStudyHubTest")

    store.set(VOYAGE_API_KEY_SECRET, "stored-secret")
    assert store.get(VOYAGE_API_KEY_SECRET) == "stored-secret"
    store.delete(VOYAGE_API_KEY_SECRET)
    assert store.get(VOYAGE_API_KEY_SECRET) is None
