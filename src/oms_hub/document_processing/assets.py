"""Safe, immutable persistence for parser-extracted document assets."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from oms_hub.files.atomic import verified_atomic_write
from oms_hub.study_generation.quiz_images import SanitizedQuizImage, sanitize_quiz_image

_ASSET_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")
_RASTER_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


@dataclass(frozen=True, slots=True)
class PersistedAsset:
    """Safe asset details, or unserved diagnostic metadata for an object payload."""

    key: str
    path: Path | None
    media_type: str
    sha256: str
    width: int | None
    height: int | None
    diagnostic: str | None = None


def persist_asset(asset_root: Path, key: str, media_type: str, payload: bytes) -> PersistedAsset:
    """Persist a safe image by content hash without ever serving object payloads."""
    if not _ASSET_KEY.fullmatch(key):
        raise ValueError(
            "asset key must contain only letters, digits, dots, underscores, or hyphens"
        )
    normalized_media_type = media_type.casefold().strip()
    if not _MEDIA_TYPE.fullmatch(normalized_media_type):
        raise ValueError("asset media type must be a valid MIME type")
    if normalized_media_type not in _RASTER_MEDIA_TYPES:
        return PersistedAsset(
            key=key,
            path=None,
            media_type=normalized_media_type,
            sha256=hashlib.sha256(payload).hexdigest(),
            width=None,
            height=None,
            diagnostic=f"unsupported embedded asset media type: {normalized_media_type}",
        )

    sanitized = sanitize_quiz_image(payload)
    destination = asset_root / f"{key}-{sanitized.sha256}.png"
    verified_atomic_write(sanitized.payload, destination)
    return _persisted_image(key, destination, sanitized)


def _persisted_image(key: str, path: Path, image: SanitizedQuizImage) -> PersistedAsset:
    return PersistedAsset(
        key=key,
        path=path,
        media_type=image.media_type,
        sha256=image.sha256,
        width=image.width,
        height=image.height,
    )
