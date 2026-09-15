import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.document_processing.run_styles import (
    StyledTextRun,
    StyledTextRunSidecar,
    normalized_text_sha256,
)
from oms_hub.study_generation.gpt_lecture import source_manifest
from oms_hub.study_generation.quiz_evidence import compact_evidence
from tests.study_generation.test_gpt_lecture import _inputs


def _run(inputs, text, index, **style):
    return StyledTextRun(
        source_id=inputs.slide_source_id,
        source_sha256=inputs.documents[0].source_sha256,
        slide_number=2,
        locator=f"slide:2:shape:1:p:1:r:{index}",
        text=text,
        normalized_text_sha256=normalized_text_sha256(text),
        **style,
    )


def test_compaction_preserves_text_keys_locations_associations_styles_and_manifest(tmp_path):
    inputs = _inputs(tmp_path)
    segment = replace(
        inputs.documents[0].segments[0], text="Neutral Red Blue Underline Mystery Body",
        locator=DocumentLocator("slide 2 content 1", slide_number=2, block_index=1),
    )
    runs = (
        _run(inputs, "Neutral", 1, resolved_color="000000"),
        _run(inputs, "Red", 2, resolved_color="FF0000", inherited_color_resolution="theme"),
        _run(inputs, "Blue", 3, resolved_color="0070C0", bold=True),
        _run(inputs, "Underline", 4, underline=True, italic=True, highlight="FFFF00"),
        _run(inputs, "Mystery", 5, theme_color="accent1", color_attempted=True),
        _run(inputs, "Body", 6),
        _run(inputs, "Text missing from native parser", 7),
    )
    inputs = replace(
        inputs,
        documents=(replace(inputs.documents[0], segments=(segment,)), inputs.documents[1]),
        run_styles=(StyledTextRunSidecar(
            source_id=inputs.slide_source_id, source_sha256=inputs.documents[0].source_sha256,
            parser_version="test", runs=runs,
        ),),
    )
    original = source_manifest(inputs)
    evidence = json.loads(json.dumps(compact_evidence(inputs)))
    assert evidence["objectives"] == [{"id": key, "text": text} for key, text in inputs.objectives]
    for source, document in zip(evidence["sources"], inputs.documents, strict=True):
        assert source["source_id"] == document.source_id
        assert source["sha256"] == document.source_sha256
        for value, parsed in zip(source["segments"], document.segments, strict=True):
            assert value["key"] == parsed.key and value["text"] == parsed.text
            assert value.get("kind", "paragraph") == parsed.kind
            assert value["locator"] == {
                key: value for key, value in asdict(parsed.locator).items()
                if key != "label" and value is not None
            }
            assert value.get("asset_keys", []) == list(parsed.asset_keys)
            assert value["style_metadata"] == list(parsed.style_metadata)
    by_text = {
        row[2]: {"locator": row[1], **evidence["run_styles"][0]["styles"][row[3]]}
        for row in evidence["run_styles"][0]["runs"]
    }
    assert set(by_text) == {
        "Red", "Blue", "Underline", "Mystery", "Text missing from native parser",
    }
    assert by_text["Red"]["resolved_color"] == "FF0000"
    assert by_text["Red"]["locator"] == runs[1].locator
    assert by_text["Blue"]["bold"] is True
    assert by_text["Underline"]["highlight"] == "FFFF00"
    assert by_text["Underline"]["underline"] is True
    assert by_text["Underline"]["italic"] is True
    assert by_text["Mystery"]["color_unresolved"] is True
    assert by_text["Mystery"]["theme_color"] == "accent1"
    assert source_manifest(inputs) == original
    assert "sidecar_sha256" not in json.dumps(evidence)
    assert "normalized_text_sha256" not in json.dumps(evidence)


def test_inventory_references_complete_page_text_without_opening_assets(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    slides = inputs.documents[0]
    body = replace(slides.segments[0], text="Native body content " * 12, asset_keys=())
    image = ParsedSegment(
        "image-only", SegmentKind.IMAGE, "Figure", DocumentLocator("image", slide_number=2),
        asset_keys=(slides.assets[0].key,),
    )
    slides = replace(slides, segments=(body, image))
    inputs = replace(inputs, documents=(slides, inputs.documents[1]))

    def forbidden(*args, **kwargs):
        raise AssertionError("Prompt inventory must not read source or image files")

    for method in ("open", "read_bytes", "read_text", "stat"):
        monkeypatch.setattr(Path, method, forbidden)
    evidence = compact_evidence(inputs)
    asset = evidence["images"][0]
    assert asset["nearby_segment_keys"] == [image.key]
    assert asset["citation_segment_key"] == body.key
    assert asset["locator"] == {"slide_number": body.locator.slide_number}
    assert asset.get("needs_preview", False) is False
    assert asset["width"] == 20 and asset["height"] == 20
    assert "path" not in asset and "text" not in asset


@pytest.mark.parametrize("body_text,heading_text,expected", [
    ("Actual caption on this image's page", "Page heading", "body"),
    (" \n ", "Page heading", "heading"),
    ("", " ", None),
])
def test_image_citation_anchor_uses_nonblank_same_page_source_text(
    tmp_path, body_text, heading_text, expected,
):
    inputs = _inputs(tmp_path)
    slides, transcript = inputs.documents
    base = slides.segments[0]
    segments = (
        replace(base, key="elsewhere", text="Associated text on another page",
                locator=DocumentLocator("slide 3", slide_number=3)),
        replace(base, key="heading", kind=SegmentKind.HEADING, text=heading_text, asset_keys=()),
        replace(base, key="image", kind=SegmentKind.IMAGE, text="Image placeholder"),
        replace(base, key="body", text=body_text, asset_keys=()),
    )
    evidence = compact_evidence(replace(
        inputs, documents=(replace(slides, segments=segments), transcript),
    ))
    image = evidence["images"][0]
    assert image["nearby_segment_keys"] == ["elsewhere", "image"]
    assert image.get("citation_segment_key") == expected
    if expected is not None:
        source = next(source for source in evidence["sources"]
                      if source["source_id"] == image["source_id"])
        anchor = next(segment for segment in source["segments"] if segment["key"] == expected)
        assert anchor["text"].strip()
        assert anchor["locator"]["slide_number"] == image["locator"]["slide_number"]
    else:
        assert "citation_segment_key" not in image


def test_style_span_reconstructs_verbatim_emphasized_text(tmp_path):
    inputs = _inputs(tmp_path)
    text = "A long exact teaching phrase with Unicode β and meaningful  double spaces."
    slides = replace(inputs.documents[0], segments=(replace(
        inputs.documents[0].segments[0], text="Prefix " + text + " suffix",
    ),))
    run = _run(inputs, text, 1, resolved_color="C00000", bold=True)
    inputs = replace(inputs, documents=(slides, inputs.documents[1]), run_styles=(
        StyledTextRunSidecar(source_id=inputs.slide_source_id,
                            source_sha256=slides.source_sha256, parser_version="test", runs=(run,)),
    ))
    evidence = compact_evidence(inputs)
    style_source = evidence["run_styles"][0]
    row = style_source["runs"][0]
    key, start, end = row[2]
    segment = next(s for s in evidence["sources"][0]["segments"] if s["key"] == key)
    assert segment["text"][start:end] == text
    assert row[:2] == [run.slide_number, run.locator]
    assert style_source["styles"][row[3]] == {"bold": True, "resolved_color": "C00000"}


@pytest.mark.parametrize("render", [False, True])
def test_sparse_page_preview_prefers_one_render_otherwise_all_embedded_images(tmp_path, render):
    inputs = _inputs(tmp_path)
    slides = inputs.documents[0]
    assets = (*slides.assets, replace(slides.assets[0], key="second-image"))
    if render:
        assets = (*assets, replace(slides.assets[0], key="render", origin="full-slide-render"))
    ocr = replace(slides.segments[0], key="slide-2-ocr", text="OCR content " * 30)
    slides = replace(slides, segments=(ocr,), assets=assets)
    evidence = compact_evidence(replace(inputs, documents=(slides, inputs.documents[1])))
    previews = {
        image["asset_key"] for image in evidence["images"] if image.get("needs_preview", False)
    }
    assert previews == ({"render"} if render else {"figure-1", "second-image"})


def test_ocr_blocker_requires_preview_even_when_native_body_is_long(tmp_path):
    inputs = _inputs(tmp_path)
    warning = "BLOCKER: OCR is required but unavailable or empty for slide 2"
    slides = replace(
        inputs.documents[0], warnings=(warning,),
        segments=(replace(inputs.documents[0].segments[0], text="Native body " * 30),),
    )
    evidence = compact_evidence(replace(inputs, documents=(slides, inputs.documents[1])))
    assert evidence["sources"][0]["warnings"] == [warning]
    assert evidence["images"][0]["needs_preview"] is True


def test_instruction_scope_does_not_reintroduce_excluded_text_or_images(tmp_path):
    inputs = _inputs(tmp_path)
    slides = inputs.documents[0]
    extra = ParsedSegment("excluded", SegmentKind.NOTE, "Excluded material", DocumentLocator(
        "slide 3", slide_number=3,
    ))
    image = ParsedAsset("excluded-image", None, "image/png", "a" * 64, extra.locator)
    inputs = replace(
        inputs, instructions="only slides 2",
        documents=(replace(slides, segments=(*slides.segments, extra),
                           assets=(*slides.assets, image)), inputs.documents[1]),
    )
    evidence = compact_evidence(inputs)
    assert evidence["quiz_instructions"] == "only slides 2"
    assert [item["key"] for item in evidence["sources"][0]["segments"]] == ["block-1"]
    assert evidence["sources"][1]["segments"] == []
    assert [item["asset_key"] for item in evidence["images"]] == ["figure-1"]
