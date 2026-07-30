from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from oms_hub.anki.domain import SourceKind
from oms_hub.anki.sources import (
    LectureSourceExtractor,
    SourcePassage,
)
from oms_hub.ingestion.domain import StudyRevision, UploadKind


class FakeRevisionRepository:
    def __init__(self, revisions: dict[int, StudyRevision]) -> None:
        self.revisions = revisions

    def get_study_revision(self, revision_id: int) -> StudyRevision:
        return self.revisions[revision_id]


def _revision(
    revision_id: int,
    kind: UploadKind,
    source: Path,
    *,
    derived: Path | None = None,
) -> StudyRevision:
    return StudyRevision(
        id=revision_id,
        upload_item_id=f"upload-{revision_id}",
        lecture_id=12,
        kind=kind,
        source_sha256=f"{revision_id:064x}",
        immutable_source_path=source,
        derived_sha256=None,
        immutable_derived_path=derived,
        canonical_source_path=None,
        canonical_derived_path=None,
        icloud_path=None,
        prompt_sha256=None,
        state="current",
        current=True,
    )


def _presentation(path: Path, image_path: Path) -> None:
    deck = Presentation()
    first = deck.slides.add_slide(deck.slide_layouts[1])
    first.shapes.title.text = "Iron deficiency anemia"
    first.placeholders[1].text = "Ferritin is low before serum iron."
    first.notes_slide.notes_text_frame.text = (
        "Remember that total iron-binding capacity rises."
    )
    second = deck.slides.add_slide(deck.slide_layouts[6])
    second.shapes.add_picture(
        str(image_path),
        Inches(1),
        Inches(1),
        width=Inches(2),
    )
    deck.save(path)


def test_extracts_stable_slide_notes_and_image_marker(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "marker.png"
    Image.new("RGB", (8, 8), "white").save(image_path)
    slides_path = tmp_path / "lecture.pptx"
    _presentation(slides_path, image_path)
    repository = FakeRevisionRepository(
        {7: _revision(7, UploadKind.SLIDES, slides_path)}
    )
    extractor = LectureSourceExtractor(repository)

    first = extractor.extract([7])
    second = extractor.extract([7])

    assert [passage.passage_id for passage in first] == [
        passage.passage_id for passage in second
    ]
    assert [
        (passage.source_kind, passage.slide_number, passage.locator)
        for passage in first
    ] == [
        (SourceKind.SLIDE, 1, "slide:1"),
        (SourceKind.SPEAKER_NOTES, 1, "slide:1:notes"),
        (SourceKind.VISION, 2, "slide:2:image"),
    ]
    assert "Iron deficiency anemia" in first[0].text
    assert "total iron-binding capacity" in first[1].text
    assert first[2].text == ""
    assert first[2].extraction_status == "vision_unavailable"
    assert first[0].citation == "Lecture 12, slide 1"


def test_segments_timestamped_transcript_with_stable_overlap(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "cleaned.txt"
    transcript.write_text(
        "[00:00] Iron deficiency begins with depletion of iron stores. "
        "Ferritin falls early.\n"
        "[00:18] Total iron-binding capacity rises as deficiency progresses. "
        "Microcytosis appears later.\n"
        "[00:36] Treat the cause and replace iron when appropriate.\n",
        encoding="utf-8",
    )
    repository = FakeRevisionRepository(
        {
            8: _revision(
                8,
                UploadKind.TRANSCRIPTS,
                tmp_path / "raw.txt",
                derived=transcript,
            )
        }
    )
    extractor = LectureSourceExtractor(
        repository,
        transcript_max_chars=105,
        transcript_overlap_sentences=1,
    )

    passages = extractor.extract([8])

    assert len(passages) >= 2
    assert all(
        passage.source_kind is SourceKind.TRANSCRIPT
        for passage in passages
    )
    assert passages[0].start_seconds == 0
    assert passages[1].start_seconds is not None
    assert set(passages[0].text.split()) & set(passages[1].text.split())
    assert passages == extractor.extract([8])
    assert passages[0].citation.startswith("Lecture 12, transcript ")


def test_passage_rejects_blank_non_vision_evidence() -> None:
    try:
        SourcePassage.create(
            revision_id=9,
            lecture_id=12,
            artifact_id="upload-9",
            source_kind=SourceKind.SLIDE,
            locator="slide:1",
            text="",
            slide_number=1,
        )
    except ValueError as error:
        assert "blank" in str(error)
    else:
        raise AssertionError("blank source evidence was accepted")
