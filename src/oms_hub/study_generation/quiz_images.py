from __future__ import annotations

import hashlib
import re
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from PIL import Image, ImageOps, UnidentifiedImageError

from oms_hub.files.atomic import verified_atomic_write
from oms_hub.study_generation.studio_domain import (
    StudioQuizImageRequirement,
    StudioSource,
    StudioStoredImage,
)
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

    def auto_bind_from_sources(
        self,
        run_id: str,
        requirements: tuple[StudioQuizImageRequirement, ...],
        sources: tuple[StudioSource, ...],
    ) -> tuple[str, ...]:
        """Bind unambiguous images embedded in the selected source files.

        NotebookLM remains the authority for whether an image is needed. This
        helper only fills a requirement when the requested source title matches
        exactly and its page/slide locator identifies one extracted image.
        """
        bound: list[str] = []
        for requirement in requirements:
            if requirement.image is not None:
                continue
            source = next(
                (
                    item
                    for item in sources
                    if _same_label(item.title, requirement.source_title)
                ),
                None,
            )
            if source is None or source.payload_path is None:
                continue
            candidates = _extract_source_images(source.payload_path)
            candidate = _select_candidate(candidates, requirement.locator)
            if candidate is None:
                continue
            filename = candidate.filename
            payload = candidate.payload
            try:
                sanitized = sanitize_quiz_image(payload)
            except QuizImageError:
                continue
            path = self.media_root / run_id / f"{requirement.image_key}-{sanitized.sha256}.png"
            verified_atomic_write(sanitized.payload, path)
            self.repository.bind_image(
                run_id,
                requirement.image_key,
                StudioStoredImage(
                    path,
                    sanitized.sha256,
                    sanitized.media_type,
                    sanitized.width,
                    sanitized.height,
                    filename[:500] or "extracted-image",
                ),
            )
            bound.append(requirement.image_key)
        return tuple(bound)


@dataclass(frozen=True, slots=True)
class _ExtractedImage:
    filename: str
    payload: bytes
    locator: str


def _same_label(left: str, right: str) -> bool:
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())


def _select_candidate(
    candidates: tuple[_ExtractedImage, ...],
    locator: str,
) -> _ExtractedImage | None:
    if not candidates:
        return None
    numbers = [int(value) for value in re.findall(r"(?:page|slide|p\.?)[^0-9]{0,4}(\d+)", locator.casefold())]
    if numbers:
        matches = tuple(
            candidate
            for candidate in candidates
            if any(
                re.search(rf"(?:page|slide)\s*{number}(?:\D|$)", candidate.locator.casefold())
                for number in numbers
            )
        )
        return matches[0] if len(matches) == 1 else None
    return candidates[0] if len(candidates) == 1 else None


def _extract_source_images(path: Path) -> tuple[_ExtractedImage, ...]:
    suffix = path.suffix.casefold()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        try:
            return (_ExtractedImage(path.name, path.read_bytes(), "source image"),)
        except OSError:
            return ()
    if suffix == ".pdf":
        return _extract_pdf_images(path)
    if suffix in {".pptx", ".docx"}:
        return _extract_zip_images(path)
    return ()


def _extract_pdf_images(path: Path) -> tuple[_ExtractedImage, ...]:
    try:
        import fitz  # type: ignore[import-untyped]

        images: list[_ExtractedImage] = []
        with fitz.open(path) as document:
            for page_index, page in enumerate(document, start=1):
                for image_index, metadata in enumerate(page.get_images(full=True), start=1):
                    payload = document.extract_image(metadata[0]).get("image")
                    if isinstance(payload, bytes):
                        images.append(
                            _ExtractedImage(
                                f"page-{page_index}-image-{image_index}",
                                payload,
                                f"page {page_index} image {image_index}",
                            )
                        )
        return tuple(images)
    except (ImportError, OSError, RuntimeError, ValueError):
        return ()


def _extract_zip_images(path: Path) -> tuple[_ExtractedImage, ...]:
    try:
        with ZipFile(path) as archive:
            names = tuple(
                name
                for name in archive.namelist()
                if "/media/" in name.casefold()
                and Path(name).suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
            )
            return tuple(
                _ExtractedImage(Path(name).name, archive.read(name), f"embedded image {index}")
                for index, name in enumerate(names, start=1)
            )
    except (OSError, ValueError, KeyError):
        return ()
