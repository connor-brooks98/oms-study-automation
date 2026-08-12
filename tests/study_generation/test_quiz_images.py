from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.study_generation import quiz_images


class _Reader:
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.read_sizes: list[int] = []

    def __enter__(self) -> _Reader:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.chunks.pop(0) if self.chunks else b""


class _Archive:
    def __init__(self, infos: list[object], readers: dict[str, _Reader]):
        self.infos = infos
        self.readers = readers
        self.opened: list[str] = []

    def __enter__(self) -> _Archive:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def infolist(self) -> list[object]:
        return self.infos

    def open(self, name: str) -> _Reader:
        self.opened.append(name)
        return self.readers[name]


def _info(
    name: str = "ppt/media/image1.png", *, size: int = 1, compressed: int = 1
) -> object:
    return SimpleNamespace(filename=name, file_size=size, compress_size=compressed)


def _use_archive(monkeypatch: pytest.MonkeyPatch, archive: _Archive) -> None:
    monkeypatch.setattr(quiz_images, "ZipFile", lambda _path: archive)


def test_member_count_bomb_is_rejected_before_member_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _Archive([_info("a"), _info("b")], {})
    monkeypatch.setattr(quiz_images, "MAX_OFFICE_ARCHIVE_MEMBERS", 1)
    _use_archive(monkeypatch, archive)

    with pytest.raises(quiz_images.QuizImageError, match="too many members"):
        quiz_images._extract_zip_images(Path("bomb.pptx"))

    assert archive.opened == []


def test_declared_per_member_bomb_is_rejected_before_member_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _Archive([_info(size=5)], {})
    monkeypatch.setattr(quiz_images, "MAX_OFFICE_ARCHIVE_MEMBER_BYTES", 4)
    _use_archive(monkeypatch, archive)

    with pytest.raises(quiz_images.QuizImageError, match="member exceeds"):
        quiz_images._extract_zip_images(Path("bomb.pptx"))

    assert archive.opened == []


def test_total_expanded_bomb_is_rejected_before_member_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _Archive([_info("a", size=3), _info("b", size=3)], {})
    monkeypatch.setattr(quiz_images, "MAX_OFFICE_ARCHIVE_TOTAL_BYTES", 5)
    _use_archive(monkeypatch, archive)

    with pytest.raises(quiz_images.QuizImageError, match="total expanded"):
        quiz_images._extract_zip_images(Path("bomb.pptx"))

    assert archive.opened == []


@pytest.mark.parametrize("size,compressed", [(101, 1), (1, 0)])
def test_high_or_zero_compressed_ratio_is_rejected_before_member_open(
    monkeypatch: pytest.MonkeyPatch, size: int, compressed: int
) -> None:
    archive = _Archive([_info(size=size, compressed=compressed)], {})
    monkeypatch.setattr(quiz_images, "MAX_OFFICE_ARCHIVE_COMPRESSION_RATIO", 100)
    _use_archive(monkeypatch, archive)

    with pytest.raises(quiz_images.QuizImageError, match="compression-ratio"):
        quiz_images._extract_zip_images(Path("bomb.pptx"))

    assert archive.opened == []


def test_actual_per_member_bomb_aborts_after_bounded_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _Reader([b"ab", b"cd", b"e"])
    archive = _Archive([_info(size=4)], {"ppt/media/image1.png": reader})
    monkeypatch.setattr(quiz_images, "MAX_OFFICE_ARCHIVE_MEMBER_BYTES", 4)
    monkeypatch.setattr(quiz_images, "MAX_OFFICE_ARCHIVE_TOTAL_BYTES", 10)
    monkeypatch.setattr(quiz_images, "OFFICE_ARCHIVE_READ_CHUNK_BYTES", 2)
    _use_archive(monkeypatch, archive)

    with pytest.raises(quiz_images.QuizImageError, match="member exceeds"):
        quiz_images._extract_zip_images(Path("lying.pptx"))

    assert reader.read_sizes == [2, 2, 1]
    assert max(reader.read_sizes) == 2


def test_actual_total_bomb_aborts_after_bounded_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _Reader([b"ab", b""])
    second = _Reader([b"cd", b"e"])
    archive = _Archive(
        [_info("ppt/media/one.png", size=2), _info("ppt/media/two.png", size=2)],
        {"ppt/media/one.png": first, "ppt/media/two.png": second},
    )
    monkeypatch.setattr(quiz_images, "MAX_OFFICE_ARCHIVE_MEMBER_BYTES", 4)
    monkeypatch.setattr(quiz_images, "MAX_OFFICE_ARCHIVE_TOTAL_BYTES", 4)
    monkeypatch.setattr(quiz_images, "OFFICE_ARCHIVE_READ_CHUNK_BYTES", 2)
    _use_archive(monkeypatch, archive)

    with pytest.raises(quiz_images.QuizImageError, match="total expanded"):
        quiz_images._extract_zip_images(Path("lying-total.pptx"))

    assert second.read_sizes == [2, 1]
    assert max(second.read_sizes) == 2


def test_valid_member_is_read_in_bounded_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _Reader([b"ab", b"cd", b""])
    archive = _Archive([_info(size=4)], {"ppt/media/image1.png": reader})
    monkeypatch.setattr(quiz_images, "OFFICE_ARCHIVE_READ_CHUNK_BYTES", 2)
    _use_archive(monkeypatch, archive)

    images = quiz_images._extract_zip_images(Path("valid.pptx"))

    assert images[0].payload == b"abcd"
    assert reader.read_sizes == [2, 2, 2]
    assert max(reader.read_sizes) == 2


def test_auto_bind_extracts_one_source_once_for_multiple_requirements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[Path] = []
    candidates = (
        quiz_images._ExtractedImage("one.png", b"one", "slide 1"),
        quiz_images._ExtractedImage("two.png", b"two", "slide 2"),
    )
    monkeypatch.setattr(
        quiz_images,
        "_extract_source_images",
        lambda path: calls.append(path) or candidates,
    )
    monkeypatch.setattr(
        quiz_images,
        "sanitize_quiz_image",
        lambda payload: quiz_images.SanitizedQuizImage(payload, payload.hex(), 1, 1),
    )
    monkeypatch.setattr(quiz_images, "verified_atomic_write", lambda *_args: None)
    bound: list[str] = []
    service = quiz_images.StudioQuizImageService(
        SimpleNamespace(bind_image=lambda _run_id, image_key, _image: bound.append(image_key)),
        tmp_path,
    )
    source = SimpleNamespace(id="source-1", title="Slides", payload_path=tmp_path / "slides.pptx")
    requirements = (
        SimpleNamespace(image=None, source_title="Slides", locator="slide 1", image_key="one"),
        SimpleNamespace(image=None, source_title="Slides", locator="slide 2", image_key="two"),
    )

    assert service.auto_bind_from_sources("run-1", requirements, (source,)) == ("one", "two")
    assert calls == [source.payload_path]
    assert bound == ["one", "two"]


def test_auto_bind_does_not_share_cached_candidates_across_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        quiz_images,
        "_extract_source_images",
        lambda path: calls.append(path)
        or (quiz_images._ExtractedImage(path.name, b"image", "slide 1"),),
    )
    monkeypatch.setattr(
        quiz_images,
        "sanitize_quiz_image",
        lambda payload: quiz_images.SanitizedQuizImage(payload, payload.hex(), 1, 1),
    )
    monkeypatch.setattr(quiz_images, "verified_atomic_write", lambda *_args: None)
    service = quiz_images.StudioQuizImageService(
        SimpleNamespace(bind_image=lambda *_args: None), tmp_path
    )
    first = SimpleNamespace(id="source-1", title="Slides A", payload_path=tmp_path / "a.pptx")
    second = SimpleNamespace(id="source-2", title="Slides B", payload_path=tmp_path / "b.pptx")
    requirements = (
        SimpleNamespace(image=None, source_title="Slides A", locator="slide 1", image_key="one"),
        SimpleNamespace(image=None, source_title="Slides B", locator="slide 1", image_key="two"),
    )

    service.auto_bind_from_sources("run-1", requirements, (first, second))

    assert calls == [first.payload_path, second.payload_path]
