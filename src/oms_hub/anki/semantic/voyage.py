import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

import httpx
import numpy as np

from oms_hub.anki.semantic.domain import FloatMatrix, InputType
from oms_hub.security.secret_store import (
    VOYAGE_API_KEY_SECRET,
    SecretStore,
)


class VoyageEmbeddingError(RuntimeError):
    """Voyage failed without exposing credentials or source text."""


_MAX_BATCH_CHARACTERS = 400_000
_CurlPost = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[httpx.Response]]


class VoyageEmbeddingClient:
    url = "https://api.voyageai.com/v1/embeddings"

    def __init__(
        self,
        secrets: SecretStore,
        *,
        model: str = "voyage-4-large",
        dimensions: int = 1024,
        batch_size: int = 128,
        max_attempts: int = 3,
        retry_base_seconds: float = 0.5,
        api_key: str | None = None,
        http: httpx.AsyncClient | None = None,
        curl_post: _CurlPost | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not model.strip():
            raise ValueError("model cannot be empty")
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds cannot be negative")
        self.secrets = secrets
        self.model = model.strip()
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.api_key = api_key.strip() if api_key is not None else None
        self._sleep = sleep
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=120.0, trust_env=False)
        self._curl_post = curl_post or _curl_post

    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        if input_type not in {"document", "query"}:
            raise ValueError("input_type must be document or query")
        normalized = [text.strip() for text in texts]
        if any(not text for text in normalized):
            raise ValueError("embedding inputs cannot be blank")
        if not normalized:
            return np.empty((0, self.dimensions), dtype=np.float32)
        api_key = self._credential()
        batches: list[FloatMatrix] = []
        for batch_index, batch in enumerate(
            _embedding_batches(normalized, self.batch_size)
        ):
            batches.append(
                await self._embed_batch(
                    batch,
                    input_type=input_type,
                    api_key=api_key,
                    batch_index=batch_index,
                )
            )
        return np.concatenate(batches, axis=0).astype(
            np.float32,
            copy=False,
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _credential(self) -> str:
        if self.api_key:
            return self.api_key
        try:
            value = self.secrets.get(VOYAGE_API_KEY_SECRET)
        except Exception as exc:
            raise VoyageEmbeddingError(
                "Voyage credential storage is unavailable"
            ) from exc
        if value is None or not value.strip():
            raise VoyageEmbeddingError("Voyage credential is not configured")
        return value.strip()

    async def _embed_batch(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
        api_key: str,
        batch_index: int,
    ) -> FloatMatrix:
        payload = {
            "input": list(texts),
            "model": self.model,
            "input_type": input_type,
            "truncation": True,
            "output_dimension": self.dimensions,
            "output_dtype": "float",
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        for attempt in range(self.max_attempts):
            try:
                response = await self._http.post(
                    self.url,
                    headers=headers,
                    json=payload,
                )
            except httpx.RequestError as exc:
                if attempt + 1 < self.max_attempts:
                    await self._backoff(attempt)
                    continue
                raise VoyageEmbeddingError(
                    f"Voyage embedding batch {batch_index} is unavailable"
                ) from exc
            if _is_html_bad_request(response):
                response = await self._curl_post(self.url, headers, payload)
            if response.status_code == 200:
                return self._validated_vectors(
                    response,
                    expected_count=len(texts),
                    batch_index=batch_index,
                )
            if (
                response.status_code == 429
                or response.status_code >= 500
            ) and attempt + 1 < self.max_attempts:
                await self._backoff(attempt)
                continue
            request_id = _request_id(response)
            suffix = f" (request {request_id})" if request_id else ""
            raise VoyageEmbeddingError(
                f"Voyage embedding batch {batch_index} failed with "
                f"HTTP {response.status_code}{suffix}"
            )
        raise AssertionError("embedding retry loop did not return or raise")

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(self.retry_base_seconds * (2**attempt))

    def _validated_vectors(
        self,
        response: httpx.Response,
        *,
        expected_count: int,
        batch_index: int,
    ) -> FloatMatrix:
        try:
            payload = response.json()
        except ValueError as exc:
            raise VoyageEmbeddingError(
                f"Voyage embedding batch {batch_index} returned invalid JSON"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("model") != self.model
            or not isinstance(payload.get("data"), list)
        ):
            raise VoyageEmbeddingError(
                f"Voyage embedding batch {batch_index} returned invalid metadata"
            )
        data = cast(list[Any], payload["data"])
        if len(data) != expected_count:
            raise VoyageEmbeddingError(
                f"Voyage embedding batch {batch_index} returned an invalid row count"
            )
        ordered: list[list[float] | None] = [None] * expected_count
        for item in data:
            if (
                not isinstance(item, dict)
                or item.get("object") != "embedding"
                or not isinstance(item.get("index"), int)
                or isinstance(item.get("index"), bool)
                or not isinstance(item.get("embedding"), list)
            ):
                raise VoyageEmbeddingError(
                    f"Voyage embedding batch {batch_index} returned invalid rows"
                )
            index = cast(int, item["index"])
            if not 0 <= index < expected_count or ordered[index] is not None:
                raise VoyageEmbeddingError(
                    f"Voyage embedding batch {batch_index} returned invalid indices"
                )
            vector = item["embedding"]
            if not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                for value in vector
            ):
                raise VoyageEmbeddingError(
                    f"Voyage embedding batch {batch_index} returned invalid values"
                )
            ordered[index] = cast(list[float], vector)
        if any(vector is None for vector in ordered):
            raise VoyageEmbeddingError(
                f"Voyage embedding batch {batch_index} returned incomplete rows"
            )
        matrix = np.asarray(ordered, dtype=np.float32)
        if matrix.shape != (expected_count, self.dimensions):
            raise VoyageEmbeddingError(
                f"Voyage embedding batch {batch_index} returned invalid dimensions"
            )
        if not np.isfinite(matrix).all():
            raise VoyageEmbeddingError(
                f"Voyage embedding batch {batch_index} returned invalid values"
            )
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise VoyageEmbeddingError(
                f"Voyage embedding batch {batch_index} returned invalid zero vectors"
            )
        return (matrix / norms).astype(np.float32, copy=False)


def _request_id(response: httpx.Response) -> str | None:
    for name in ("request-id", "x-request-id"):
        value = response.headers.get(name)
        if value:
            return str(value)[:200]
    return None


def _is_html_bad_request(response: httpx.Response) -> bool:
    return (
        response.status_code == 400
        and "text/html" in response.headers.get("content-type", "").casefold()
    )


async def _curl_post(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> httpx.Response:
    return await asyncio.to_thread(_curl_post_sync, url, headers, payload)


def _curl_post_sync(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> httpx.Response:
    from curl_cffi import requests

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        impersonate="chrome",
        http_version="v1",
        timeout=120,
    )
    return httpx.Response(
        response.status_code,
        headers={
            str(name): str(value)
            for name, value in response.headers.items()
            if value is not None
        },
        content=response.content,
    )


def _embedding_batches(
    texts: Sequence[str],
    batch_size: int,
) -> Sequence[Sequence[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    current_characters = 0
    for text in texts:
        text_characters = len(text)
        if current and (
            len(current) >= batch_size
            or current_characters + text_characters > _MAX_BATCH_CHARACTERS
        ):
            batches.append(current)
            current = []
            current_characters = 0
        current.append(text)
        current_characters += text_characters
    if current:
        batches.append(current)
    return batches
