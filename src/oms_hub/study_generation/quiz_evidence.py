"""Compact prompt evidence; immutable source manifests retain the audit representation.

No source text is truncated. Paragraph kind is the default; numbered locators omit
their redundant display label. Segment keys and nondefault relationships remain
unchanged. Image context uses source-local segment keys instead of repeated text.

Run styles retain positive emphasis, every nonneutral resolved color, unresolved
declared colors, and text absent from parsed segments. A duplicated run with only
black/white or no recorded color and no positive emphasis can be omitted. Omission
never establishes the effective style of an inherited/unresolved run. Resolved
colors already include the extractor's transformations; no colors are inferred.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from oms_hub.document_processing.domain import DocumentLocator, ParsedSegment, SegmentKind

if TYPE_CHECKING:
    from oms_hub.study_generation.gpt_lecture import LectureInputs


def _locator(locator: DocumentLocator) -> dict[str, object]:
    result = {key: value for key, value in asdict(locator).items() if value is not None}
    if len(result) > 1:
        del result["label"]
    return result


def _segment(segment: ParsedSegment) -> dict[str, object]:
    result = {
        key: value for key, value in asdict(segment).items()
        if value is not None and value != ()
    }
    result["locator"] = _locator(segment.locator)
    if segment.kind == SegmentKind.PARAGRAPH:
        del result["kind"]
    return result


def _same_location(left: DocumentLocator, right: DocumentLocator) -> bool:
    for field in ("slide_number", "page_number"):
        number = getattr(left, field)
        if number is not None:
            return bool(number == getattr(right, field))
    return left == right


def _styles(run: dict[str, Any]) -> dict[str, object]:
    result = {
        key: run[key] for key in ("bold", "italic", "underline", "highlight")
        if run.get(key)
    }
    if run["resolved_color"] is not None:
        result["resolved_color"] = run["resolved_color"]
    elif any(run.get(key) for key in (
        "color_attempted", "explicit_rgb", "theme_color", "inherited_color_resolution"
    )):
        # Unresolved declarations remain visible and cannot become inferred red text.
        result["color_unresolved"] = True
        result.update({key: run[key] for key in (
            "explicit_rgb", "theme_color", "inherited_color_resolution", "brightness"
        ) if run.get(key) is not None})
    return result


def compact_evidence(inputs: LectureInputs) -> dict[str, object]:
    """Return complete eligible text and a selectable, byte-free image inventory."""
    # The instruction filter owns eligibility; keep this helper out of its import cycle.
    from oms_hub.study_generation.gpt_lecture import (
        _instruction_run_styles,
        quiz_instruction_documents,
    )

    documents = quiz_instruction_documents(inputs)
    bindings = {binding.snapshot.id: binding for binding in inputs.bindings}
    sources = [{
        "source_id": document.source_id,
        "title": bindings[document.source_id].snapshot.title,
        "role": bindings[document.source_id].role,
        "sha256": document.source_sha256,
        "segments": [_segment(segment) for segment in document.segments],
        **({"warnings": list(document.warnings)} if document.warnings else {}),
    } for document in documents]
    images = []
    for document in documents:
        if document.source_id != inputs.slide_source_id:
            continue
        for asset in sorted(document.assets, key=lambda value: value.key):
            nearby = [segment for segment in document.segments if (
                asset.key in segment.asset_keys
                or _same_location(asset.locator, segment.locator)
            )]
            native = [segment.text for segment in nearby if (
                segment.kind not in {SegmentKind.IMAGE, SegmentKind.HEADING, SegmentKind.NOTE}
                and "ocr" not in segment.key.casefold()
                and "ocr" not in segment.locator.label.casefold()
            )]
            # ponytail: conservative sparse-text heuristic; replace with parser signals
            # if reliable native-versus-OCR provenance becomes available.
            unknown = any(
                f"BLOCKER: OCR is required but unavailable or empty for {unit} {number}"
                in document.warnings
                for unit, number in (("slide", asset.locator.slide_number),
                                     ("page", asset.locator.page_number))
                if number is not None
            )
            needs_preview = unknown or len(" ".join(native).strip()) < 80
            renders = sorted(
                candidate.key for candidate in document.assets
                if candidate.origin in {"full-slide-render", "full-page-render"}
                and _same_location(asset.locator, candidate.locator)
            )
            if renders:
                needs_preview = needs_preview and asset.key == renders[0]
            associated_keys = [segment.key for segment in nearby if asset.key in segment.asset_keys]
            citation = min((segment for segment in nearby if (
                _same_location(asset.locator, segment.locator)
                and segment.text.strip() and segment.kind != SegmentKind.IMAGE
            )), key=lambda segment: segment.kind == SegmentKind.HEADING, default=None)
            images.append({
                "source_id": document.source_id,
                "asset_key": asset.key,
                "locator": _locator(asset.locator),
                **({"width": asset.width} if asset.width is not None else {}),
                **({"height": asset.height} if asset.height is not None else {}),
                **({"nearby_segment_keys": associated_keys} if associated_keys else {}),
                **({"citation_segment_key": citation.key} if citation is not None else {}),
                **({"needs_preview": True} if needs_preview else {}),
            })

    run_styles = []
    by_source = {document.source_id: document for document in documents}
    for sidecar in _instruction_run_styles(inputs):
        source_id = str(sidecar["source_id"])
        document = by_source[source_id]
        runs = []
        styles: list[dict[str, object]] = []
        for run in cast(list[dict[str, Any]], sidecar["runs"]):
            if not run["text"].strip():
                continue
            style = _styles(run)
            emphasized = any(key != "resolved_color" for key in style) or (
                style.get("resolved_color") not in {None, "000000", "FFFFFF"}
            )
            containing = next((segment for segment in document.segments if (
                segment.locator.slide_number == run["slide_number"]
                and run["text"] in segment.text
            )), None)
            if emphasized or containing is None:
                text: str | list[str | int] = run["text"]
                if containing is not None:
                    start = containing.text.index(run["text"])
                    reference: list[str | int] = [
                        containing.key, start, start + len(run["text"]),
                    ]
                    # Use a span only when it saves space; it reconstructs exact run text.
                    if len(run["text"]) > len(containing.key) + 18:
                        text = reference
                if style not in styles:
                    styles.append(style)
                runs.append([run["slide_number"], run["locator"], text, styles.index(style)])
        if runs:
            run_styles.append({"source_id": source_id, "styles": styles, "runs": runs})

    return {
        "lecture_id": inputs.lecture_id,
        "subject": inputs.subject,
        "exam_number": inputs.exam_number,
        "image_required": inputs.image_required,
        "objectives": [{"id": key, "text": text} for key, text in inputs.objectives],
        "sources": sources,
        "images": images,
        "run_styles": run_styles,
        "evidence_schema": (
            "Missing segment kind means paragraph; numbered locators omit display labels. "
            "Missing image needs_preview means false; missing nearby_segment_keys means []. "
            "Image nearby_segment_keys reference directly associated text in that source; "
            "all nearby text and keys are source segments matching the image's slide/page. "
            "Image citation_segment_key points to nonblank text on that page in that source; "
            "it anchors the image's page but does not prove a question's claim. "
            "needs_preview flags unavailable required OCR or fewer than 80 native body-text "
            "characters, excluding OCR. "
            "Prefer one full-page/slide render for preview, otherwise all local images. "
            "Run-style rows are [slide_number, exact_locator, text_or_span, style_index]; "
            "style_index selects that source's styles array. "
            "A span [segment_key,start,end] selects exact source-local text[start:end]. "
            "Run styles preserve positive emphasis, nonneutral resolved RGB, unresolved "
            "color declarations, and text absent from segments. Duplicated runs with only "
            "black/white or no recorded color and no positive emphasis may be omitted. "
            "Omitted styles never establish effective inherited/unresolved formatting."
        ),
        **({"quiz_instructions": inputs.instructions} if inputs.instructions else {}),
    }
