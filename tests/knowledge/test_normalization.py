from __future__ import annotations

import pytest

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.knowledge.ids import evidence_id, sha256_text
from oms_hub.knowledge.models import EvidenceLocator, EvidenceLocatorKind, EvidenceUnit
from oms_hub.knowledge.normalization import (
    CourseRevisionInput,
    SlideInput,
    TranscriptSegmentInput,
    normalize_course_revision,
    render_index_markdown,
)
from oms_hub.providers.contracts import AuthorityClass


def test_slide_text_and_speaker_notes_keep_distinct_locators() -> None:
    units = normalize_course_revision(
        CourseRevisionInput.synthetic(
            source_revision_id="sr_1",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            slides=[
                SlideInput(
                    number=1,
                    text="Intrinsic pathway",
                    speaker_notes="Know factor VIII",
                ),
            ],
        )
    )

    assert [(unit.locator.kind.value, unit.locator.value) for unit in units] == [
        ("slide", "1"),
        ("speaker_note", "1"),
    ]
    assert [unit.normalized_text for unit in units] == [
        "Intrinsic pathway",
        "Know factor VIII",
    ]


def test_rendered_index_markdown_contains_stable_markers() -> None:
    unit = normalize_course_revision(
        CourseRevisionInput.synthetic(
            source_revision_id="sr_1",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            slides=[SlideInput(number=42, text="Intrinsic pathway")],
        )
    )[0]

    markdown = render_index_markdown((unit,))

    assert markdown == (
        f"[EVIDENCE:{unit.evidence_id}]\n"
        "[SOURCE_REVISION:sr_1]\n"
        "[AUTHORITY:course_material]\n"
        "[LOCATION:slide 42]\n"
        "\n"
        "Intrinsic pathway\n"
    )


def test_parsed_segments_map_in_upstream_order_without_merging_kinds() -> None:
    parsed = ParsedDocument(
        source_id="source",
        source_sha256="a" * 64,
        source_format="pptx",
        parser_name="fixture",
        parser_version="1",
        segments=(
            ParsedSegment(
                key="slide-text",
                kind=SegmentKind.PARAGRAPH,
                text="Slide text",
                locator=DocumentLocator("slide 2", slide_number=2),
            ),
            ParsedSegment(
                key="page-text",
                kind=SegmentKind.HEADING,
                text="Page heading",
                locator=DocumentLocator("page 3", page_number=3),
            ),
            ParsedSegment(
                key="note",
                kind=SegmentKind.NOTE,
                text="Speaker note",
                locator=DocumentLocator("slide 2 notes", slide_number=2),
            ),
            ParsedSegment(
                key="table",
                kind=SegmentKind.TABLE,
                text="A | B\n1 | 2",
                locator=DocumentLocator("slide 4 table", slide_number=4),
            ),
            ParsedSegment(
                key="figure",
                kind=SegmentKind.IMAGE,
                text="Figure caption",
                locator=DocumentLocator("slide 5 image", slide_number=5),
                asset_keys=("asset-1",),
            ),
        ),
        assets=(
            ParsedAsset(
                key="asset-1",
                path=None,
                media_type="image/png",
                sha256="b" * 64,
                locator=DocumentLocator("slide 5 image", slide_number=5),
            ),
        ),
        warnings=(),
    )

    units = normalize_course_revision(
        CourseRevisionInput(
            source_revision_id="sr_parsed",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            parsed_document=parsed,
        )
    )

    assert [(unit.locator.kind, unit.locator.value) for unit in units] == [
        (EvidenceLocatorKind.SLIDE, "2"),
        (EvidenceLocatorKind.PAGE, "3"),
        (EvidenceLocatorKind.SPEAKER_NOTE, "slide 2 notes"),
        (EvidenceLocatorKind.TABLE, "slide 4 table"),
        (EvidenceLocatorKind.FIGURE, "slide 5 image"),
    ]
    assert [unit.normalized_text for unit in units] == [
        "Slide text",
        "Page heading",
        "Speaker note",
        "A | B\n1 | 2",
        "Figure caption",
    ]
    assert units[-1].image_asset_id == "asset-1"


def test_equal_text_parsed_blocks_keep_detailed_locators_and_ids() -> None:
    parsed = ParsedDocument(
        source_id="source",
        source_sha256="a" * 64,
        source_format="pptx",
        parser_name="fixture",
        parser_version="1",
        segments=(
            ParsedSegment(
                key="first",
                kind=SegmentKind.PARAGRAPH,
                text="Same text",
                locator=DocumentLocator("slide 1 content 1", slide_number=1),
            ),
            ParsedSegment(
                key="second",
                kind=SegmentKind.PARAGRAPH,
                text="Same text",
                locator=DocumentLocator("slide 1 content 2", slide_number=1),
            ),
            ParsedSegment(
                key="indexed-first",
                kind=SegmentKind.PARAGRAPH,
                text="Same text",
                locator=DocumentLocator("slide 2", slide_number=2, block_index=1),
            ),
            ParsedSegment(
                key="indexed-second",
                kind=SegmentKind.PARAGRAPH,
                text="Same text",
                locator=DocumentLocator("slide 2", slide_number=2, block_index=2),
            ),
        ),
        assets=(),
        warnings=(),
    )

    units = normalize_course_revision(
        CourseRevisionInput(
            source_revision_id="sr_blocks",
            course_id="heme",
            parsed_document=parsed,
        )
    )

    assert [unit.locator.value for unit in units] == [
        "slide 1 content 1",
        "slide 1 content 2",
        "2:1",
        "2:2",
    ]
    assert len({unit.evidence_id for unit in units}) == len(units)


def test_markdown_rejects_non_course_evidence_before_rendering() -> None:
    unit = EvidenceUnit(
        evidence_id="ev_journal",
        source_revision_id="sr_journal",
        authority_class=AuthorityClass.PUBLISHED_JOURNAL,
        course_id=None,
        exam_id=None,
        lecture_id=None,
        locator=EvidenceLocator(EvidenceLocatorKind.ARTICLE_PAGE, "1"),
        normalized_text="Journal text",
        content_sha256=sha256_text("Journal text"),
    )

    with pytest.raises(ValueError, match="course_material"):
        render_index_markdown((unit,))


def test_equal_text_parsed_notes_keep_detailed_locators_and_ids() -> None:
    parsed = ParsedDocument(
        source_id="source",
        source_sha256="a" * 64,
        source_format="pptx",
        parser_name="fixture",
        parser_version="1",
        segments=(
            ParsedSegment(
                key="note-first",
                kind=SegmentKind.NOTE,
                text="Same note",
                locator=DocumentLocator("slide 1 notes 1", slide_number=1),
            ),
            ParsedSegment(
                key="note-second",
                kind=SegmentKind.NOTE,
                text="Same note",
                locator=DocumentLocator("slide 1 notes 2", slide_number=1),
            ),
        ),
        assets=(),
        warnings=(),
    )

    units = normalize_course_revision(
        CourseRevisionInput(
            source_revision_id="sr_notes",
            course_id="heme",
            parsed_document=parsed,
        )
    )

    assert [unit.locator.value for unit in units] == [
        "slide 1 notes 1",
        "slide 1 notes 2",
    ]
    assert units[0].evidence_id != units[1].evidence_id


def test_transcript_segments_are_distinct_ordered_evidence() -> None:
    units = normalize_course_revision(
        CourseRevisionInput.synthetic(
            source_revision_id="sr_transcript",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            transcript_segments=[
                TranscriptSegmentInput(number=7, text="First transcript fact"),
                TranscriptSegmentInput(number=8, text="Second transcript fact"),
            ],
        )
    )

    assert [(unit.locator.kind.value, unit.locator.value) for unit in units] == [
        ("transcript_segment", "7"),
        ("transcript_segment", "8"),
    ]
    assert [unit.normalized_text for unit in units] == [
        "First transcript fact",
        "Second transcript fact",
    ]


def test_native_slide_text_wins_and_ocr_is_fallback_only() -> None:
    units = normalize_course_revision(
        CourseRevisionInput.synthetic(
            source_revision_id="sr_ocr",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            slides=[
                SlideInput(number=1, text="Native text", ocr_text="OCR text"),
                SlideInput(number=2, text="", ocr_text="OCR fallback"),
            ],
        )
    )

    assert [unit.normalized_text for unit in units] == ["Native text", "OCR fallback"]


def test_blank_segments_and_empty_figures_are_omitted() -> None:
    parsed = ParsedDocument(
        source_id="source",
        source_sha256="a" * 64,
        source_format="pdf",
        parser_name="fixture",
        parser_version="1",
        segments=(
            ParsedSegment(
                key="blank",
                kind=SegmentKind.PARAGRAPH,
                text=" \r\n ",
                locator=DocumentLocator("page 1", page_number=1),
            ),
            ParsedSegment(
                key="empty-image",
                kind=SegmentKind.IMAGE,
                text="",
                locator=DocumentLocator("page 2", page_number=2),
            ),
        ),
        assets=(),
        warnings=(),
    )

    assert normalize_course_revision(
        CourseRevisionInput(
            source_revision_id="sr_blank",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            parsed_document=parsed,
        )
    ) == ()


def test_newlines_and_outer_blank_space_are_normalized_without_rephrasing() -> None:
    units = normalize_course_revision(
        CourseRevisionInput.synthetic(
            source_revision_id="sr_text",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            slides=[SlideInput(number=1, text=" \r\n  A\rB  \r\n ")],
        )
    )

    assert units[0].normalized_text == "A\nB"
    assert units[0].content_sha256 == sha256_text("A\nB")


def test_ids_include_locator_and_content_and_repeated_text_is_preserved() -> None:
    units = normalize_course_revision(
        CourseRevisionInput.synthetic(
            source_revision_id="sr_ids",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            slides=[
                SlideInput(number=1, text="Repeated text"),
                SlideInput(number=2, text="Repeated text"),
            ],
        )
    )

    assert len(units) == 2
    assert units[0].evidence_id != units[1].evidence_id
    assert units[0].evidence_id == evidence_id(
        "sr_ids", "slide:1", sha256_text("Repeated text")
    )
    assert units[1].evidence_id == evidence_id(
        "sr_ids", "slide:2", sha256_text("Repeated text")
    )


def test_normalization_is_course_material_and_preserves_scope() -> None:
    unit = normalize_course_revision(
        CourseRevisionInput.synthetic(
            source_revision_id="sr_scope",
            course_id="course-1",
            exam_id=None,
            lecture_id=None,
            slides=[SlideInput(number=1, text="Course fact")],
        )
    )[0]

    assert unit.authority_class is AuthorityClass.COURSE_MATERIAL
    assert (unit.course_id, unit.exam_id, unit.lecture_id) == ("course-1", None, None)


def test_repeated_headers_are_not_trimmed_without_upstream_metadata() -> None:
    units = normalize_course_revision(
        CourseRevisionInput.synthetic(
            source_revision_id="sr_headers",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            slides=[
                SlideInput(number=1, text="Course title\nFirst fact"),
                SlideInput(number=2, text="Course title\nSecond fact"),
            ],
        )
    )

    assert [unit.normalized_text for unit in units] == [
        "Course title\nFirst fact",
        "Course title\nSecond fact",
    ]


def test_markdown_separates_units_deterministically_and_ends_with_newline() -> None:
    units = normalize_course_revision(
        CourseRevisionInput.synthetic(
            source_revision_id="sr_markdown",
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            slides=[
                SlideInput(number=1, text="First"),
                SlideInput(number=2, text="Second"),
            ],
        )
    )

    expected_blocks = [
        "\n".join(
            (
                f"[EVIDENCE:{units[0].evidence_id}]",
                "[SOURCE_REVISION:sr_markdown]",
                "[AUTHORITY:course_material]",
                "[LOCATION:slide 1]",
                "",
                "First",
            )
        ),
        "\n".join(
            (
                f"[EVIDENCE:{units[1].evidence_id}]",
                "[SOURCE_REVISION:sr_markdown]",
                "[AUTHORITY:course_material]",
                "[LOCATION:slide 2]",
                "",
                "Second",
            )
        ),
    ]

    assert render_index_markdown(units) == "\n\n".join(expected_blocks) + "\n"
    assert render_index_markdown(()) == ""
