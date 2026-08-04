import asyncio
import json
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
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


_CurlPost = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[httpx.Response]]
_CURL_STATUS_MARKER = b"\n__OMS_HUB_STATUS__:"


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
        self._owns_http = http is None and not _runs_on_windows()
        self._http = http or (
            None if _runs_on_windows() else httpx.AsyncClient(timeout=120.0)
        )
        self._curl_post = (
            (curl_post or _windows_curl_post)
            if http is None and _runs_on_windows()
            else None
        )

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
        for start in range(0, len(normalized), self.batch_size):
            batches.append(
                await self._embed_batch(
                    normalized[start : start + self.batch_size],
                    input_type=input_type,
                    api_key=api_key,
                    batch_index=start // self.batch_size,
                )
            )
        return np.concatenate(batches, axis=0).astype(
            np.float32,
            copy=False,
        )

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
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
        for attempt in range(self.max_attempts):
            try:
                headers = {"Authorization": f"Bearer {api_key}"}
                if self._curl_post is not None:
                    response = await self._curl_post(self.url, headers, payload)
                else:
                    if self._http is None:
                        raise AssertionError("Voyage transport was not configured")
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


def _runs_on_windows() -> bool:
    return sys.platform == "win32"


async def _windows_curl_post(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> httpx.Response:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix="oms-voyage-",
        delete=False,
    ) as body_file:
        json.dump(payload, body_file, ensure_ascii=False, separators=(",", ":"))
        body_path = Path(body_file.name)
    try:
        process = await asyncio.create_subprocess_exec(
            "curl.exe",
            "--config",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate(
            _curl_config(url, headers, body_path).encode("utf-8")
        )
    except FileNotFoundError as exc:
        raise httpx.RequestError("native curl is unavailable") from exc
    finally:
        body_path.unlink(missing_ok=True)
    if process.returncode != 0:
        raise httpx.RequestError("native curl request failed")
    return _curl_response(stdout)


def _curl_config(
    url: str,
    headers: dict[str, str],
    body_path: Path,
) -> str:
    curl_path = body_path.as_posix()
    header_lines = [
        'header = "Content-Type: application/json"',
        *[
            f'header = "{_curl_config_value(f"{name}: {value}")}"'
            for name, value in headers.items()
        ],
    ]
    return "\n".join(
        [
            f'url = "{_curl_config_value(url)}"',
            'request = "POST"',
            *header_lines,
            f'data-binary = "@{_curl_config_value(curl_path)}"',
            "http1.1",
            "silent",
            "show-error",
            "connect-timeout = 30",
            "max-time = 120",
            'write-out = "\\n__OMS_HUB_STATUS__:%{http_code}"',
        ]
    )


def _curl_config_value(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("curl configuration values cannot contain newlines")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _curl_response(stdout: bytes) -> httpx.Response:
    try:
        content, status_text = stdout.rsplit(_CURL_STATUS_MARKER, 1)
        status_code = int(status_text)
    except (TypeError, ValueError) as exc:
        raise httpx.RequestError("native curl returned an invalid response") from exc
    if not 100 <= status_code <= 599:
        raise httpx.RequestError("native curl returned an invalid status")
    return httpx.Response(status_code, content=content)
