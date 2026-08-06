import hashlib
import socket
from collections.abc import Iterator
from pathlib import Path
from threading import Event

import httpx
import pytest

from oms_hub.document_processing.snapshots import URLSnapshotService


def _public_dns(host: str, _: object):
    address = "127.0.0.1" if host == "private.example" else "8.8.8.8"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


def test_url_snapshot_rechecks_redirect_destination(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    service = URLSnapshotService(
        tmp_path,
        max_bytes=1024,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"location": "http://private.example/private"})
        ),
    )

    with pytest.raises(ValueError, match="public address"):
        service.fetch("source-1", "Questions", "https://professor.example/questions")


def test_url_snapshot_sanitizes_a_malformed_port(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as error:
        URLSnapshotService(tmp_path, max_bytes=1024).fetch(
            "source-1",
            "Questions",
            "https://professor.example:sentinel-secret/questions",
        )

    assert str(error.value) == "URL must be HTTP or HTTPS"
    assert "sentinel-secret" not in str(error.value)
    assert error.value.__cause__ is None


def test_url_snapshot_sanitizes_a_malformed_redirect_target(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    service = URLSnapshotService(
        tmp_path,
        max_bytes=1024,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                302,
                headers={"location": "https://second.example:sentinel-secret/final"},
            )
        ),
    )

    with pytest.raises(ValueError) as error:
        service.fetch("source-1", "Questions", "https://professor.example/questions")

    assert str(error.value) in {
        "URL redirect target is invalid",
        "URL could not be downloaded",
    }
    assert "sentinel-secret" not in str(error.value)
    assert error.value.__cause__ is None


def test_url_snapshot_writes_immutable_typed_snapshot_and_final_url(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    payload = b"<h1>Questions</h1>"
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/questions":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=payload, headers={"content-type": "text/html"})

    snapshot = URLSnapshotService(
        tmp_path, max_bytes=1024, transport=httpx.MockTransport(handler)
    ).fetch(
        "source-1", "Questions", "https://professor.example/questions"
    )

    assert snapshot.path == tmp_path / "source-1" / "snapshot.html"
    assert snapshot.path.read_bytes() == payload
    assert snapshot.sha256 == hashlib.sha256(payload).hexdigest()
    assert snapshot.original_url == "https://professor.example/final"


def test_web_image_is_snapshotted_with_the_same_ssrf_rules(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    asset = URLSnapshotService(
        tmp_path,
        max_bytes=1024 * 1024,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=_png_bytes(), headers={"content-type": "image/png"}
            )
        ),
    ).fetch_asset(
        "https://professor.example/questions", "/figure.png", tmp_path / "assets"
    )

    assert asset.media_type == "image/png"
    assert asset.path is not None and asset.path.is_file()


def test_url_snapshot_rejects_streams_larger_than_its_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    with pytest.raises(ValueError, match="byte limit"):
        URLSnapshotService(
            tmp_path,
            max_bytes=3,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=b"too large", headers={"content-type": "text/plain"}
                )
            ),
        ).fetch(
            "source-1", "Questions", "https://professor.example/questions.txt"
        )


def test_snapshot_binds_connection_to_the_single_validated_address_and_keeps_host_sni(
    tmp_path: Path, monkeypatch
) -> None:
    lookups: list[str] = []

    def rebinding_dns(host: str, _: object):
        lookups.append(host)
        address = "8.8.8.8" if len(lookups) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_dns)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"Question", headers={"content-type": "text/plain"})

    URLSnapshotService(
        tmp_path,
        max_bytes=1024,
        transport=httpx.MockTransport(handler),
    ).fetch("source-1", "Questions", "https://professor.example/questions")

    assert lookups == ["professor.example"]
    assert str(requests[0].url.host) == "8.8.8.8"
    assert requests[0].headers["host"] == "professor.example"
    assert requests[0].extensions["sni_hostname"] == "professor.example"


def test_snapshot_fails_over_only_after_a_transport_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
        ],
    )
    attempted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(str(request.url.host))
        if request.url.host == "1.1.1.1":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, content=b"Question", headers={"content-type": "text/plain"})

    snapshot = URLSnapshotService(
        tmp_path, max_bytes=1024, transport=httpx.MockTransport(handler)
    ).fetch("source-1", "Questions", "https://professor.example/questions")

    assert snapshot.path.read_bytes() == b"Question"
    assert attempted == ["1.1.1.1", "8.8.8.8"]


def test_snapshot_never_fails_over_after_an_http_response(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
        ],
    )
    attempted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(str(request.url.host))
        return httpx.Response(503, content=b"unavailable")

    with pytest.raises(ValueError, match="could not be downloaded"):
        URLSnapshotService(
            tmp_path, max_bytes=1024, transport=httpx.MockTransport(handler)
        ).fetch("source-1", "Questions", "https://professor.example/questions")

    assert attempted == ["1.1.1.1"]


def test_snapshot_binds_each_redirect_hop_without_proxy_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, _: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("8.8.8.8" if host == "one.example" else "1.1.1.1", 0),
            )
        ],
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers["host"] == "one.example":
            return httpx.Response(302, headers={"location": "https://two.example/final"})
        return httpx.Response(200, content=b"Question", headers={"content-type": "text/plain"})

    URLSnapshotService(
        tmp_path,
        max_bytes=1024,
        transport=httpx.MockTransport(handler),
    ).fetch("source-1", "Questions", "https://one.example/questions")

    assert [str(request.url.host) for request in requests] == ["8.8.8.8", "1.1.1.1"]
    assert [request.headers["host"] for request in requests] == ["one.example", "two.example"]


def test_url_snapshot_enforces_one_deadline_across_streamed_chunks(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    clock = _Clock()

    def chunks() -> Iterator[bytes]:
        yield b"first"
        clock.advance(16)
        yield b"second"

    service = URLSnapshotService(
        tmp_path,
        max_bytes=1024,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=chunks(), headers={"content-type": "text/plain"})
        ),
        clock=clock,
    )

    with pytest.raises(ValueError, match="deadline"):
        service.fetch("source-1", "Questions", "https://professor.example/questions.txt")


def test_url_snapshot_deadline_covers_redirects_and_final_response(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    clock = _Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/questions":
            clock.advance(10)
            return httpx.Response(302, headers={"location": "/final"})
        clock.advance(6)
        return httpx.Response(200, content=b"Question", headers={"content-type": "text/plain"})

    service = URLSnapshotService(
        tmp_path,
        max_bytes=1024,
        transport=httpx.MockTransport(handler),
        clock=clock,
    )

    with pytest.raises(ValueError, match="deadline"):
        service.fetch("source-1", "Questions", "https://professor.example/questions")


def test_web_asset_sanitized_expansion_is_checked_before_an_atomic_write(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    compressed = _noisy_jpeg_bytes()
    asset_root = tmp_path / "assets"
    service = URLSnapshotService(
        tmp_path,
        max_bytes=len(compressed) + 1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=compressed, headers={"content-type": "image/jpeg"}
            )
        ),
    )

    with pytest.raises(ValueError, match="sanitized web image exceeds"):
        service.fetch_asset("https://professor.example/page", "/figure.jpg", asset_root)

    assert not asset_root.exists()


def test_web_asset_snapshot_is_scoped_to_the_requested_asset_root(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=_png_bytes(), headers={"content-type": "image/png"})

    service = URLSnapshotService(
        tmp_path, max_bytes=1024 * 1024, transport=httpx.MockTransport(handler)
    )
    first = service.fetch_asset(
        "https://professor.example/page", "/figure.png", tmp_path / "first-assets"
    )
    second = service.fetch_asset(
        "https://professor.example/page", "/figure.png", tmp_path / "second-assets"
    )

    assert requests == 2
    assert first.path is not None and first.path.parent.name == "first-assets"
    assert second.path is not None and second.path.parent.name == "second-assets"


def test_web_asset_does_not_reuse_a_prior_snapshot_when_current_budget_is_exhausted(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    service = URLSnapshotService(
        tmp_path,
        max_bytes=1024 * 1024,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=_png_bytes(), headers={"content-type": "image/png"}
            )
        ),
    )
    service.fetch_asset("https://professor.example/page", "/figure.png", tmp_path / "first-assets")

    with pytest.raises(ValueError, match="byte limit"):
        service.fetch_asset(
            "https://professor.example/page",
            "/figure.png",
            tmp_path / "second-assets",
            max_bytes=1,
        )

    assert not (tmp_path / "second-assets").exists()


def test_url_snapshot_hard_deadline_stops_hanging_dns_without_writing(
    tmp_path: Path, monkeypatch
) -> None:
    released = Event()
    completed = Event()

    def hanging_dns(*_: object) -> list[object]:
        released.wait()
        completed.set()
        return []

    monkeypatch.setattr(socket, "getaddrinfo", hanging_dns)
    service = URLSnapshotService(tmp_path, max_bytes=1024, deadline_seconds=0.01)

    with pytest.raises(ValueError, match="deadline"):
        service.fetch("source-1", "Questions", "https://professor.example/questions")

    released.set()
    assert completed.wait(1)
    assert not (tmp_path / "source-1").exists()


def test_url_snapshot_hard_deadline_stops_hanging_headers_without_writing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    released = Event()
    completed = Event()

    def hanging_headers(_: httpx.Request) -> httpx.Response:
        released.wait()
        completed.set()
        return httpx.Response(200, content=b"Question", headers={"content-type": "text/plain"})

    service = URLSnapshotService(
        tmp_path,
        max_bytes=1024,
        deadline_seconds=0.01,
        transport=httpx.MockTransport(hanging_headers),
    )

    with pytest.raises(ValueError, match="deadline"):
        service.fetch("source-1", "Questions", "https://professor.example/questions")

    released.set()
    assert completed.wait(1)
    assert not (tmp_path / "source-1").exists()


def test_url_snapshot_hard_deadline_stops_hanging_stream_read_without_writing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    released = Event()
    completed = Event()
    service = URLSnapshotService(
        tmp_path,
        max_bytes=1024,
        deadline_seconds=0.01,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=_wait_for_release_chunks(released, completed),
                headers={"content-type": "text/plain"},
            )
        ),
    )

    with pytest.raises(ValueError, match="deadline"):
        service.fetch("source-1", "Questions", "https://professor.example/questions")

    released.set()
    assert completed.wait(1)
    assert not (tmp_path / "source-1").exists()


def test_snapshot_client_disables_environment_proxy_inheritance(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    original_client = httpx.Client
    client_kwargs: list[object] = []

    def recording_client(*args: object, **kwargs: object) -> httpx.Client:
        client_kwargs.append(kwargs["trust_env"])
        return original_client(*args, **kwargs)

    monkeypatch.setattr("oms_hub.document_processing.snapshots.httpx.Client", recording_client)
    URLSnapshotService(
        tmp_path,
        max_bytes=1024,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=b"Question", headers={"content-type": "text/plain"}
            )
        ),
    ).fetch("source-1", "Questions", "https://professor.example/questions")

    assert client_kwargs == [False]


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (2, 2), "purple").save(output, format="PNG")
    return output.getvalue()


def _different_png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (2, 2), "orange").save(output, format="PNG")
    return output.getvalue()


def _wait_for_release_chunks(released: Event, completed: Event) -> Iterator[bytes]:
    released.wait()
    completed.set()
    yield b"Question"


def _noisy_jpeg_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    image = Image.effect_noise((300, 300), 100).convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=20)
    return output.getvalue()
