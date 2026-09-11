"""Lecture-scoped immutable source evidence for GPT generation.

Bindings must be loaded from trusted Hub revision/ownership records by the caller,
never constructed from a provider response or inferred from uploaded filenames.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from oms_hub.document_processing.domain import ParsedAsset, ParsedDocument, SourceSnapshot
from oms_hub.document_processing.presentation_render import PresentationRenderer
from oms_hub.document_processing.router import DocumentProcessorRouter
from oms_hub.document_processing.run_styles import (
    StyledTextRunSidecar,
    extract_styled_text_run_sidecar,
)
from oms_hub.files.atomic import sha256_file
from oms_hub.study_generation.quiz_images import MAX_QUIZ_IMAGE_BYTES, sanitize_quiz_image


@dataclass(frozen=True, slots=True)
class LectureSourceBinding:
    lecture_id: int
    subject: str
    exam_number: int
    revision_id: int
    role: Literal["slides", "cleaned_transcript"]
    snapshot: SourceSnapshot
    is_current: bool
    is_approved: bool


@dataclass(frozen=True, slots=True)
class LectureInputs:
    lecture_id: int
    subject: str
    exam_number: int
    slide_revision_id: int
    transcript_revision_id: int
    slide_source_id: str
    transcript_source_id: str
    objectives: tuple[tuple[str, str], ...]
    documents: tuple[ParsedDocument, ...]
    bindings: tuple[LectureSourceBinding, ...] = ()
    prompt_version: str = "gpt-lecture-v1"
    image_required: bool = True
    run_styles: tuple[StyledTextRunSidecar, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "objectives", tuple(tuple(item) for item in self.objectives))
        for field in ("documents", "bindings", "run_styles"):
            object.__setattr__(self, field, tuple(getattr(self, field)))


def uncovered_objectives(
    expected: tuple[str, ...], assigned: tuple[tuple[str, ...], ...]
) -> tuple[str, ...]:
    covered = {objective for question in assigned for objective in question}
    return tuple(objective for objective in expected if objective not in covered)


def _validate_bindings(inputs: LectureInputs) -> tuple[LectureSourceBinding, ...]:
    if inputs.lecture_id < 1 or inputs.exam_number < 1 or not inputs.subject.strip():
        raise ValueError("lecture scope is missing")
    if not inputs.prompt_version.strip():
        raise ValueError("prompt version is missing")
    ids = tuple(item[0] for item in inputs.objectives)
    if not ids or any(not item.strip() or item != item.strip() for item in ids):
        raise ValueError("objective ids must be nonblank and stable")
    if len(ids) != len(set(ids)) or any(not text.strip() for _, text in inputs.objectives):
        raise ValueError("objective ids must be unique and objective text nonempty")
    if len(inputs.bindings) != 2:
        raise ValueError("trusted slides and cleaned transcript bindings are required")
    expected = {
        "slides": (inputs.slide_source_id, inputs.slide_revision_id),
        "cleaned_transcript": (inputs.transcript_source_id, inputs.transcript_revision_id),
    }
    if (
        set(binding.role for binding in inputs.bindings) != set(expected)
        or inputs.slide_source_id == inputs.transcript_source_id
    ):
        raise ValueError("distinct slides and cleaned transcript source roles are required")
    for binding in inputs.bindings:
        if (binding.lecture_id, binding.subject, binding.exam_number) != (
            inputs.lecture_id,
            inputs.subject,
            inputs.exam_number,
        ):
            raise ValueError("source revision ownership does not match lecture scope")
        if (binding.snapshot.id, binding.revision_id) != expected[binding.role]:
            raise ValueError("selected source revision does not match trusted binding")
        if (
            not binding.snapshot.id.strip()
            or binding.revision_id < 1
            or not binding.is_current
            or not binding.is_approved
        ):
            raise ValueError("source revision must be current and approved")
        _verify_file(binding.snapshot.path, binding.snapshot.sha256, "source")
    return tuple(next(b for b in inputs.bindings if b.role == role) for role in expected)


def validate_lecture_inputs(inputs: LectureInputs) -> None:
    bindings = _validate_bindings(inputs)
    if len(inputs.documents) != 2 or {doc.source_id for doc in inputs.documents} != {
        binding.snapshot.id for binding in bindings
    }:
        raise ValueError("parsed documents do not match selected lecture sources")
    for binding in bindings:
        document = next(doc for doc in inputs.documents if doc.source_id == binding.snapshot.id)
        if document.source_sha256 != binding.snapshot.sha256:
            raise ValueError("parsed source hash does not match selected revision")
        if not document.parser_name.strip() or not document.parser_version.strip():
            raise ValueError("parser identity and version are required")
        if any(warning.lstrip().startswith("BLOCKER:") for warning in document.warnings):
            raise ValueError("source parser reported a BLOCKER: " + "; ".join(document.warnings))
        if not any(segment.text.strip() for segment in document.segments):
            raise ValueError("source text is empty")
        for asset in document.assets:
            _verify_asset(asset)
            if binding.role == "slides" and not (
                asset.locator.page_number or asset.locator.slide_number
            ):
                raise ValueError("slide image requires an actual page or slide locator")
        if binding.role == "slides" and inputs.image_required:
            pages = {
                (segment.locator.page_number, segment.locator.slide_number)
                for segment in document.segments
                if segment.locator.page_number or segment.locator.slide_number
            }
            illustrated = {
                (asset.locator.page_number, asset.locator.slide_number) for asset in document.assets
            }
            if not document.assets or pages - illustrated:
                raise ValueError("required slide image evidence is unavailable; review blocked")
    styles = {sidecar.source_id: sidecar for sidecar in inputs.run_styles}
    if len(styles) != len(inputs.run_styles):
        raise ValueError("duplicate run-style source")
    for sidecar in inputs.run_styles:
        if (
            sidecar.source_id != inputs.slide_source_id
            or sidecar.source_sha256 != bindings[0].snapshot.sha256
        ):
            raise ValueError("run styles do not match the selected slide source hash")
    if (
        bindings[0].snapshot.path.suffix.casefold() == ".pptx"
        and inputs.slide_source_id not in styles
    ):
        raise ValueError("PowerPoint run-style evidence is missing")


def source_asset(inputs: LectureInputs, source_id: str, asset_key: str) -> ParsedAsset:
    """Resolve only a source-qualified selected slide image, rechecking its bytes."""
    validate_lecture_inputs(inputs)
    if source_id != inputs.slide_source_id:
        raise ValueError("image source is not the selected lecture slides")
    for document in inputs.documents:
        if document.source_id == source_id:
            for asset in document.assets:
                if asset.key == asset_key:
                    return asset
    raise ValueError("unknown source-qualified image asset")


def source_manifest(inputs: LectureInputs) -> dict[str, object]:
    """Return a detached JSON-ready private artifact, including durable local paths."""
    validate_lecture_inputs(inputs)
    sources = []
    for binding in _validate_bindings(inputs):
        document = next(doc for doc in inputs.documents if doc.source_id == binding.snapshot.id)
        sources.append(
            {
                "snapshot": {**asdict(binding.snapshot), "path": str(binding.snapshot.path)},
                "is_current": binding.is_current,
                "is_approved": binding.is_approved,
                "source_id": document.source_id,
                "revision_id": binding.revision_id,
                "role": binding.role,
                "sha256": document.source_sha256,
                "source_format": document.source_format,
                "parser_name": document.parser_name,
                "parser_version": document.parser_version,
                "segments": [asdict(segment) for segment in document.segments],
                "assets": [{**asdict(asset), "path": str(asset.path)} for asset in document.assets],
                "warnings": document.warnings,
            }
        )
    manifest = {
        "lecture_id": inputs.lecture_id,
        "subject": inputs.subject,
        "exam_number": inputs.exam_number,
        "prompt_version": inputs.prompt_version,
        "image_required": inputs.image_required,
        "objectives": [
            {"id": key, "text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
            for key, text in inputs.objectives
        ],
        "sources": sources,
        "run_styles": [sidecar.model_dump(mode="json") for sidecar in inputs.run_styles],
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result: dict[str, object] = json.loads(canonical)
    result["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def _verify_file(path: Path | None, expected: str, label: str) -> None:
    if path is None or not path.is_file():
        raise ValueError(f"{label} file is missing")
    if sha256_file(path) != expected:
        raise ValueError(f"{label} hash does not match immutable evidence")


def _verify_asset(asset: ParsedAsset) -> None:
    _verify_file(asset.path, asset.sha256, "asset")
    assert asset.path is not None
    if asset.path.stat().st_size > MAX_QUIZ_IMAGE_BYTES:
        raise ValueError("asset exceeds the quiz image size limit")
    image = sanitize_quiz_image(asset.path.read_bytes())
    if (
        asset.media_type != image.media_type
        or asset.width != image.width
        or asset.height != image.height
        or asset.sha256 != image.sha256
    ):
        raise ValueError("asset does not match sanitized image evidence")


def parse_lecture_sources(
    inputs: LectureInputs,
    router: DocumentProcessorRouter,
    asset_root: Path,
    *,
    renderer: PresentationRenderer | None = None,
) -> LectureInputs:
    """Parse trusted snapshots offline and retain source-qualified visual/style evidence."""
    bindings = _validate_bindings(inputs)
    documents = []
    styles = []
    for binding in bindings:
        snapshot = binding.snapshot
        # Hash the source id rather than allowing it to become a filesystem path.
        root = asset_root / hashlib.sha256(snapshot.id.encode()).hexdigest() / snapshot.sha256
        document = router.parse(snapshot, root)
        if binding.role == "slides":
            if snapshot.path.suffix.casefold() == ".pptx":
                styles.append(
                    extract_styled_text_run_sidecar(
                        snapshot.path, source_id=snapshot.id, source_sha256=snapshot.sha256
                    )
                )
            if renderer is not None:
                rendered = renderer.render(snapshot, root, max_pages=500, max_pixels=4_000_000)
                represented = {
                    (asset.locator.page_number, asset.locator.slide_number)
                    for asset in document.assets
                    if asset.path is not None
                }
                additions = tuple(
                    asset
                    for asset in rendered.assets
                    if (asset.locator.page_number, asset.locator.slide_number) not in represented
                )
                document = replace(
                    document,
                    assets=(*document.assets, *additions),
                    warnings=(*document.warnings, *rendered.warnings),
                )
        documents.append(document)
    parsed = replace(inputs, documents=tuple(documents), run_styles=tuple(styles))
    validate_lecture_inputs(parsed)
    return parsed


def lecture_inputs_from_manifest(manifest: dict[str, object]) -> LectureInputs:
    """Reload a private saved artifact without reparsing; O rechecks DB ownership/currentness."""
    payload = {key: value for key, value in manifest.items() if key != "sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if manifest.get("sha256") != hashlib.sha256(canonical.encode("utf-8")).hexdigest():
        raise ValueError("source manifest digest does not match")
    # JSON decoding makes an independent plain-data copy before validation.
    data = json.loads(canonical)
    try:
        sources = data["sources"]
        by_role = {source["role"]: source for source in sources}
        inputs = TypeAdapter(LectureInputs).validate_python(
            {
                **{
                    key: data[key]
                    for key in (
                        "lecture_id",
                        "subject",
                        "exam_number",
                        "prompt_version",
                        "image_required",
                        "run_styles",
                    )
                },
                "slide_source_id": by_role["slides"]["source_id"],
                "slide_revision_id": by_role["slides"]["revision_id"],
                "transcript_source_id": by_role["cleaned_transcript"]["source_id"],
                "transcript_revision_id": by_role["cleaned_transcript"]["revision_id"],
                "objectives": [
                    (objective["id"], objective["text"]) for objective in data["objectives"]
                ],
                "bindings": [
                    {
                        **{key: data[key] for key in ("lecture_id", "subject", "exam_number")},
                        **{
                            key: source[key]
                            for key in (
                                "revision_id",
                                "role",
                                "snapshot",
                                "is_current",
                                "is_approved",
                            )
                        },
                    }
                    for source in sources
                ],
                "documents": [
                    {
                        "source_sha256": source["sha256"],
                        **{
                            key: source[key]
                            for key in (
                                "source_id",
                                "source_format",
                                "parser_name",
                                "parser_version",
                                "segments",
                                "assets",
                                "warnings",
                            )
                        },
                    }
                    for source in sources
                ],
            }
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid source manifest: {error}") from error
    validate_lecture_inputs(inputs)
    if source_manifest(inputs) != manifest:
        raise ValueError("source manifest evidence is inconsistent")
    return inputs
