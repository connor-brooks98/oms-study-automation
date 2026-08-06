"""Safe acquisition of immutable URL snapshots and web image assets."""

import hashlib
import socket
from collections.abc import Callable
from ipaddress import ip_address
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic
from typing import cast
from urllib.parse import urljoin, urlparse

import httpx

from oms_hub.document_processing.domain import DocumentLocator, ParsedAsset, SourceSnapshot
from oms_hub.files.atomic import verified_atomic_write
from oms_hub.study_generation.quiz_images import sanitize_quiz_image

_DOCUMENT_TYPES = {
    "application/json": ".json",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/xml": ".xml",
    "text/html": ".html",
    "text/plain": ".txt",
    "text/xml": ".xml",
}
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_MAX_REDIRECTS = 4


class URLSnapshotService:
    """Fetch public HTTP(S) resources into immutable local files.

    Redirects are deliberately followed by this service rather than httpx so each
    destination is DNS-resolved and rejected before a connection is attempted.
    """

    def __init__(
        self,
        root: Path,
        max_bytes: int,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = monotonic,
        deadline_seconds: float = 15.0,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("URL snapshot byte limit must be positive")
        if deadline_seconds <= 0:
            raise ValueError("URL snapshot deadline must be positive")
        self.root = root
        self.max_bytes = max_bytes
        self.transport = transport
        self.clock = clock
        self.deadline_seconds = deadline_seconds

    def fetch(self, source_id: str, title: str, url: str) -> SourceSnapshot:
        deadline = self.clock() + self.deadline_seconds
        payload, media_type, final_url = self._download(
            url, self.max_bytes, _DOCUMENT_TYPES, deadline
        )
        _remaining(deadline, self.clock)
        suffix = _DOCUMENT_TYPES[media_type]
        destination = self.root / source_id / f"snapshot{suffix}"
        digest = verified_atomic_write(payload, destination)
        return SourceSnapshot(
            id=source_id,
            title=title,
            path=destination,
            media_type=media_type,
            sha256=digest,
            original_url=final_url,
        )

    def fetch_asset(
        self,
        base_url: str,
        asset_url: str,
        asset_root: Path,
        *,
        max_bytes: int | None = None,
    ) -> ParsedAsset:
        resolved_url = _safe_urljoin(base_url, asset_url, "URL asset target is invalid")
        asset_limit = self.max_bytes if max_bytes is None else max_bytes
        deadline = self.clock() + self.deadline_seconds
        payload, media_type, final_url = self._download(
            resolved_url, asset_limit, _IMAGE_TYPES, deadline
        )
        key = f"web-image-{hashlib.sha256(final_url.encode('utf-8')).hexdigest()[:24]}"
        sanitized = sanitize_quiz_image(payload)
        if len(sanitized.payload) > asset_limit:
            raise ValueError("sanitized web image exceeds the byte limit")
        _remaining(deadline, self.clock)
        path = asset_root / f"{key}-{sanitized.sha256}.png"
        verified_atomic_write(sanitized.payload, path)
        asset = ParsedAsset(
            key=key,
            path=path,
            media_type=sanitized.media_type,
            sha256=sanitized.sha256,
            locator=DocumentLocator(label=f"web image {key}"),
            width=sanitized.width,
            height=sanitized.height,
            origin=final_url,
        )
        return asset

    def _download(
        self,
        url: str,
        byte_limit: int,
        accepted_media_types: dict[str, str] | frozenset[str],
        deadline: float,
    ) -> tuple[bytes, str, str]:
        if byte_limit < 1:
            raise ValueError("URL snapshot byte limit has been exhausted")
        current_url = url.strip()
        try:
            for redirect_count in range(_MAX_REDIRECTS + 1):
                remaining = _remaining(deadline, self.clock)
                bound_urls, hostname, host_header = self._bound_public_urls(
                    current_url, deadline, self.clock
                )
                response: httpx.Response | None = None
                client: httpx.Client | None = None
                transport_error: httpx.TransportError | None = None
                for bound_url in bound_urls:
                    remaining = _remaining(deadline, self.clock)
                    client = self._client(remaining)
                    request = client.build_request(
                        "GET",
                        bound_url,
                        headers={"Host": host_header, "User-Agent": "Study Hub source snapshotter"},
                    )
                    request.extensions["sni_hostname"] = hostname
                    try:
                        response = _bounded_call(
                            _send_streaming_request(client, request), deadline, self.clock
                        )
                    except httpx.TransportError as error:
                        transport_error = error
                        _close_without_waiting(client.close)
                        client = None
                        continue
                    except Exception:
                        _close_without_waiting(client.close)
                        client = None
                        raise
                    break
                if response is None:
                    if transport_error is not None:
                        raise transport_error
                    raise ValueError("URL could not be downloaded")
                try:
                    if response.is_redirect:
                        if redirect_count >= _MAX_REDIRECTS:
                            raise ValueError("URL redirected too many times")
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("URL redirect is missing its target")
                        current_url = _safe_urljoin(
                            current_url,
                            location,
                            "URL redirect target is invalid",
                        )
                        continue
                    _remaining(deadline, self.clock)
                    response.raise_for_status()
                    media_type = _normalized_media_type(response.headers.get("content-type", ""))
                    if media_type not in accepted_media_types:
                        raise ValueError("URL returned an unsupported content type")
                    content_length = response.headers.get("content-length")
                    if content_length is not None and _content_length_exceeds(
                        content_length, byte_limit
                    ):
                        raise ValueError("URL response exceeds the byte limit")
                    payload = _stream_limited(response, byte_limit, deadline, self.clock)
                    return payload, media_type, current_url
                finally:
                    if response is not None:
                        _close_without_waiting(response.close)
                    if client is not None:
                        _close_without_waiting(client.close)
        except ValueError:
            raise
        except httpx.InvalidURL:
            raise ValueError("URL must be HTTP or HTTPS") from None
        except (httpx.HTTPError, OSError):
            raise ValueError("URL could not be downloaded") from None
        raise ValueError("URL redirected too many times")

    def _client(self, remaining: float) -> httpx.Client:
        return httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(remaining, connect=min(5.0, remaining)),
            headers={"User-Agent": "Study Hub source snapshotter"},
            transport=self.transport,
            trust_env=False,
        )

    @staticmethod
    def _bound_public_urls(
        url: str, deadline: float, clock: Callable[[], float]
    ) -> tuple[tuple[httpx.URL, ...], str, str]:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            port = parsed.port
            parsed_url = httpx.URL(url)
        except (httpx.InvalidURL, TypeError, ValueError):
            raise ValueError("URL must be HTTP or HTTPS") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("URL must be HTTP or HTTPS")
        try:
            addresses = {
                ip_address(result[4][0])
                for result in _bounded_call(
                    lambda: socket.getaddrinfo(hostname, None), deadline, clock
                )
            }
        except OSError as error:
            raise ValueError("URL host could not be resolved") from error
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("URL must point to a public address")
        host_header = hostname if port is None else f"{hostname}:{port}"
        return (
            tuple(
                parsed_url.copy_with(host=str(address))
                for address in sorted(
                    addresses,
                    key=lambda address: (address.version, int(address)),
                )
            ),
            hostname,
            host_header,
        )


def _safe_urljoin(base_url: str, target: str, message: str) -> str:
    """Join a remote URL without surfacing malformed URL components to callers."""
    try:
        joined = urljoin(base_url, target)
        return str(httpx.URL(joined))
    except (httpx.InvalidURL, TypeError, ValueError):
        raise ValueError(message) from None


def _content_length_exceeds(value: str, byte_limit: int) -> bool:
    try:
        return int(value) > byte_limit
    except ValueError as error:
        raise ValueError("URL response has an invalid content length") from error


def _stream_limited(
    response: httpx.Response,
    byte_limit: int,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    iterator = iter(response.iter_bytes())
    while True:
        _remaining(deadline, clock)
        try:
            chunk = _bounded_call(lambda: next(iterator), deadline, clock)
        except StopIteration:
            break
        _remaining(deadline, clock)
        total += len(chunk)
        if total > byte_limit:
            raise ValueError("URL response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise ValueError("URL acquisition exceeded the 15-second deadline")
    return remaining


def _bounded_call[Result](
    operation: Callable[[], Result], deadline: float, clock: Callable[[], float]
) -> Result:
    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def run() -> None:
        try:
            result.put((True, operation()))
        except BaseException as error:  # transmit network and iterator failures to the caller
            result.put((False, error))

    Thread(target=run, daemon=True).start()
    try:
        succeeded, value = result.get(timeout=_remaining(deadline, clock))
    except Empty as error:
        raise ValueError("URL acquisition exceeded the 15-second deadline") from error
    if succeeded:
        return cast(Result, value)
    if isinstance(value, BaseException):
        raise value
    raise RuntimeError("bounded URL operation returned an invalid result")


def _close_without_waiting(close: Callable[[], None]) -> None:
    Thread(target=close, daemon=True).start()


def _send_streaming_request(
    client: httpx.Client, request: httpx.Request
) -> Callable[[], httpx.Response]:
    def send() -> httpx.Response:
        return client.send(request, stream=True)

    return send


def _normalized_media_type(value: str) -> str:
    return value.split(";", 1)[0].casefold().strip()
