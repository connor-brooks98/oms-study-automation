import hashlib
import socket
from pathlib import Path

import httpx
import pytest
import respx

from oms_hub.document_processing.snapshots import URLSnapshotService


def _public_dns(host: str, _: object):
    address = "127.0.0.1" if host == "private.example" else "8.8.8.8"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


@respx.mock
def test_url_snapshot_rechecks_redirect_destination(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    respx.get("https://professor.example/questions").mock(
        return_value=httpx.Response(302, headers={"location": "http://private.example/private"})
    )
    service = URLSnapshotService(tmp_path, max_bytes=1024)

    with pytest.raises(ValueError, match="public address"):
        service.fetch("source-1", "Questions", "https://professor.example/questions")


@respx.mock
def test_url_snapshot_writes_immutable_typed_snapshot_and_final_url(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    payload = b"<h1>Questions</h1>"
    respx.get("https://professor.example/questions").mock(
        return_value=httpx.Response(
            302, headers={"location": "/final"}, request=httpx.Request("GET", "https://professor.example/questions")
        )
    )
    respx.get("https://professor.example/final").mock(
        return_value=httpx.Response(200, content=payload, headers={"content-type": "text/html"})
    )

    snapshot = URLSnapshotService(tmp_path, max_bytes=1024).fetch(
        "source-1", "Questions", "https://professor.example/questions"
    )

    assert snapshot.path == tmp_path / "source-1" / "snapshot.html"
    assert snapshot.path.read_bytes() == payload
    assert snapshot.sha256 == hashlib.sha256(payload).hexdigest()
    assert snapshot.original_url == "https://professor.example/final"


@respx.mock
def test_web_image_is_snapshotted_with_the_same_ssrf_rules(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    respx.get("https://professor.example/figure.png").mock(
        return_value=httpx.Response(
            200, content=_png_bytes(), headers={"content-type": "image/png"}
        )
    )

    asset = URLSnapshotService(tmp_path, max_bytes=1024 * 1024).fetch_asset(
        "https://professor.example/questions", "/figure.png", tmp_path / "assets"
    )

    assert asset.media_type == "image/png"
    assert asset.path is not None and asset.path.is_file()


@respx.mock
def test_url_snapshot_rejects_streams_larger_than_its_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    respx.get("https://professor.example/questions.txt").mock(
        return_value=httpx.Response(
            200, content=b"too large", headers={"content-type": "text/plain"}
        )
    )

    with pytest.raises(ValueError, match="byte limit"):
        URLSnapshotService(tmp_path, max_bytes=3).fetch(
            "source-1", "Questions", "https://professor.example/questions.txt"
        )


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (2, 2), "purple").save(output, format="PNG")
    return output.getvalue()
