from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError

from oms_hub.files.atomic import verified_atomic_write
from oms_hub.study_generation.studio_domain import StudioStoredImage
from oms_hub.study_generation.studio_repository import StudioRepository

MAX_QUIZ_IMAGE_BYTES = 10 * 1024 * 1024
MAX_QUIZ_IMAGE_PIXELS = 40_000_000
_SUPPORTED_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})


class QuizImageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SanitizedQuizImage:
    payload: bytes
    sha256: str
    width: int
    height: int
    media_type: str = "image/png"


def sanitize_quiz_image(
    payload: bytes,
    *,
    max_bytes: int = MAX_QUIZ_IMAGE_BYTES,
    max_pixels: int = MAX_QUIZ_IMAGE_PIXELS,
) -> SanitizedQuizImage:
    if not payload:
        raise QuizImageError("quiz image is empty")
    if len(payload) > max_bytes:
        raise QuizImageError("quiz image exceeds the 10 MiB upload limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as probe:
                image_format = (probe.format or "").upper()
                if image_format not in _SUPPORTED_FORMATS:
                    raise QuizImageError("quiz image must be a valid PNG, JPEG, or WebP")
                width, height = probe.size
                if width < 1 or height < 1 or width * height > max_pixels:
                    raise QuizImageError("quiz image exceeds the 40 million pixel limit")
                if getattr(probe, "n_frames", 1) != 1:
                    raise QuizImageError("animated quiz images are not supported")
                probe.verify()

            with Image.open(BytesIO(payload)) as decoded:
                if getattr(decoded, "n_frames", 1) != 1:
                    raise QuizImageError("animated quiz images are not supported")
                oriented = ImageOps.exif_transpose(decoded)
                oriented.load()
                has_alpha = "A" in oriented.getbands() or "transparency" in decoded.info
                safe = oriented.convert("RGBA" if has_alpha else "RGB")
                output = BytesIO()
                safe.save(output, format="PNG", optimize=True)
    except QuizImageError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ) as error:
        raise QuizImageError("quiz image must be a valid PNG, JPEG, or WebP") from error

    sanitized = output.getvalue()
    return SanitizedQuizImage(
        sanitized,
        hashlib.sha256(sanitized).hexdigest(),
        safe.width,
        safe.height,
    )


class StudioQuizImageService:
    def __init__(self, repository: StudioRepository, media_root: Path):
        self.repository = repository
        self.media_root = media_root

    def upload(
        self,
        run_id: str,
        image_key: str,
        original_filename: str,
        payload: bytes,
    ) -> StudioStoredImage:
        review = self.repository.quiz_review(run_id)
        if image_key not in {item.image_key for item in review.requirements}:
            raise KeyError(image_key)
        sanitized = sanitize_quiz_image(payload)
        safe_name = PurePosixPath(original_filename.replace("\\", "/")).name
        if not safe_name:
            safe_name = "image"
        safe_name = safe_name[:500]
        path = (
            self.media_root
            / run_id
            / f"{image_key}-{sanitized.sha256}.png"
        )
        verified_atomic_write(sanitized.payload, path)
        image = StudioStoredImage(
            path,
            sanitized.sha256,
            sanitized.media_type,
            sanitized.width,
            sanitized.height,
            safe_name,
        )
        self.repository.bind_image(run_id, image_key, image)
        return image
