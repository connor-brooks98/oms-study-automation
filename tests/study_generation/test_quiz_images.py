import json
from io import BytesIO

import pytest
from PIL import Image

from oms_hub.db import Database
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.quiz_images import (
    QuizImageError,
    StudioQuizImageService,
    sanitize_quiz_image,
)
from oms_hub.study_generation.studio_repository import StudioRepository


def _image_bytes(
    image_format: str,
    *,
    size: tuple[int, int] = (8, 6),
    exif: Image.Exif | None = None,
    comment: bytes | None = None,
) -> bytes:
    output = BytesIO()
    options: dict[str, object] = {}
    if exif is not None:
        options["exif"] = exif
    if comment is not None:
        options["comment"] = comment
    Image.new("RGB", size, (20, 90, 160)).save(
        output,
        format=image_format,
        **options,
    )
    return output.getvalue()


def _animated_webp() -> bytes:
    output = BytesIO()
    first = Image.new("RGB", (4, 4), "red")
    second = Image.new("RGB", (4, 4), "blue")
    first.save(
        output,
        format="WEBP",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )
    return output.getvalue()


def _review_service(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    repository = StudioRepository(database)
    run = repository.queue_run(
        "Neuro",
        1,
        "Prompt",
        [],
        "Image quiz",
        "Neuro",
        1,
    )
    quiz = parse_native_quiz(
        json.dumps(
            {
                "title": "Image quiz",
                "questions": [
                    {
                        "stem": "Use the image.",
                        "choices": ["A", "B"],
                        "correct_index": 0,
                        "rationale": "A is correct.",
                        "image_ref": {
                            "key": "image-1",
                            "source_title": "Slides",
                            "locator": "Slide 4",
                            "description": "Histology figure",
                        },
                    }
                ],
            }
        )
    )
    repository.await_image_review(run.id, "notebook-1", "raw", quiz)
    service = StudioQuizImageService(repository, tmp_path / "media")
    return database, repository, run, service


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_supported_still_image_is_sanitized_as_png(image_format):
    sanitized = sanitize_quiz_image(_image_bytes(image_format))
    decoded = Image.open(BytesIO(sanitized.payload))

    assert sanitized.media_type == "image/png"
    assert sanitized.width == 8
    assert sanitized.height == 6
    assert len(sanitized.sha256) == 64
    assert decoded.format == "PNG"
    assert decoded.info == {}


def test_jpeg_orientation_is_applied_before_metadata_is_removed():
    exif = Image.Exif()
    exif[274] = 6
    payload = _image_bytes(
        "JPEG",
        size=(2, 3),
        exif=exif,
        comment=b"private source metadata",
    )

    sanitized = sanitize_quiz_image(payload)
    decoded = Image.open(BytesIO(sanitized.payload))

    assert (sanitized.width, sanitized.height) == (3, 2)
    assert decoded.getexif().get(274) is None
    assert "comment" not in decoded.info


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "empty"),
        (b"not an image", "valid PNG, JPEG, or WebP"),
        (_animated_webp(), "animated"),
        (_image_bytes("JPEG")[:-8], "valid PNG, JPEG, or WebP"),
    ],
)
def test_unsafe_image_payload_is_rejected(payload, message):
    with pytest.raises(QuizImageError, match=message):
        sanitize_quiz_image(payload)


def test_byte_and_decoded_pixel_limits_are_enforced_before_storage():
    payload = _image_bytes("PNG", size=(20, 20))

    with pytest.raises(QuizImageError, match="10 MiB"):
        sanitize_quiz_image(payload, max_bytes=len(payload) - 1)
    with pytest.raises(QuizImageError, match="40 million"):
        sanitize_quiz_image(payload, max_pixels=399)


def test_upload_uses_content_addressed_path_and_safe_original_name(tmp_path):
    database, repository, run, service = _review_service(tmp_path)

    stored = service.upload(
        run.id,
        "image-1",
        r"C:\screenshots\figure.jpg",
        _image_bytes("JPEG"),
    )
    review = repository.quiz_review(run.id)

    assert stored == review.requirements[0].image
    assert stored.path.is_file()
    assert stored.path.parent == tmp_path / "media" / run.id
    assert stored.path.name == f"image-1-{stored.sha256}.png"
    assert stored.original_filename == "figure.jpg"
    assert stored.path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    database.close()


def test_failed_replacement_keeps_existing_bound_image(tmp_path):
    database, repository, run, service = _review_service(tmp_path)
    first = service.upload(run.id, "image-1", "first.jpg", _image_bytes("JPEG"))

    with pytest.raises(QuizImageError, match="animated"):
        service.upload(run.id, "image-1", "bad.webp", _animated_webp())

    assert repository.quiz_review(run.id).requirements[0].image == first
    database.close()


def test_unknown_image_key_is_rejected_before_a_file_is_written(tmp_path):
    database, _repository, run, service = _review_service(tmp_path)

    with pytest.raises(KeyError):
        service.upload(run.id, "image-9", "figure.png", _image_bytes("PNG"))

    assert list((tmp_path / "media").rglob("*.png")) == []
    database.close()
