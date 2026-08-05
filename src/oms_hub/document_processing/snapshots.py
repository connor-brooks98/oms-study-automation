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
        resolved_url = urljoin(base_url, asset_url)
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
                bound_url, hostname, host_header = self._bound_public_url(
                    current_url, deadline, self.clock
                )
                remaining = _remaining(deadline, self.clock)
                client = self._client(remaining)
                response: httpx.Response | None = None
                try:
                    request = client.build_request(
                        "GET",
                        bound_url,
                        headers={"Host": host_header, "User-Agent": "Study Hub source snapshotter"},
                    )
                    request.extensions["sni_hostname"] = hostname
                    response = _bounded_call(
                        _send_streaming_request(client, request), deadline, self.clock
                    )
                    if response.is_redirect:
                        if redirect_count >= _MAX_REDIRECTS:
                            raise ValueError("URL redirected too many times")
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("URL redirect is missing its target")
                        current_url = str(httpx.URL(current_url).join(location))
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
                    _close_without_waiting(client.close)
        except ValueError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise ValueError("URL could not be downloaded") from error
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
    def _bound_public_url(
        url: str, deadline: float, clock: Callable[[], float]
    ) -> tuple[httpx.URL, str, str]:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("URL must be HTTP or HTTPS")
        try:
            addresses = {
                ip_address(result[4][0])
                for result in _bounded_call(
                    lambda: socket.getaddrinfo(parsed.hostname, None), deadline, clock
                )
            }
        except OSError as error:
            raise ValueError("URL host could not be resolved") from error
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("URL must point to a public address")
        chosen = sorted(addresses, key=str)[0]
        host_header = parsed.netloc
        return httpx.URL(url).copy_with(host=str(chosen)), parsed.hostname, host_header


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
