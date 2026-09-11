"""Lecture-scoped immutable source evidence for GPT generation.

Bindings must be loaded from trusted Hub revision/ownership records by the caller,
never constructed from a provider response or inferred from uploaded filenames.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from oms_hub.document_processing.domain import ParsedAsset, ParsedDocument, SourceSnapshot
from oms_hub.document_processing.presentation_render import PresentationRenderer
from oms_hub.document_processing.router import DocumentProcessorRouter
from oms_hub.document_processing.run_styles import (
    StyledTextRunSidecar,
    extract_styled_text_run_sidecar,
)
from oms_hub.files.atomic import sha256_file, verified_atomic_write
from oms_hub.llm.codex_session import (
    MAX_OUTPUT_BYTES,
    CodexSessionClient,
    SessionError,
    SessionLifecycle,
    SessionRequest,
)
from oms_hub.study_generation.domain import PromptSnapshot, QuizImageRef
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.practice_contracts import (
    AssetCitation,
    ExtractedQuestion,
    ExtractionPayload,
    SegmentCitation,
    validate_source_references,
)
from oms_hub.study_generation.quiz_images import MAX_QUIZ_IMAGE_BYTES, sanitize_quiz_image

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from oms_hub.study_generation.practice_domain import QuestionDraftValue
    from oms_hub.study_generation.practice_review import ReviewQuestion
    from oms_hub.study_generation.quiz_images import StudioQuizImageService
    from oms_hub.study_generation.studio_domain import StudioRun
    from oms_hub.study_generation.studio_repository import StudioRepository


class GeneratedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=100)
    stem: str = Field(min_length=1, max_length=10_000)
    choices: list[str] = Field(min_length=2, max_length=8)
    correct_index: int = Field(ge=0)
    rationale: str = Field(min_length=1, max_length=20_000)
    distractor_explanations: list[str] = Field(min_length=2, max_length=8)
    objective_ids: list[str] = Field(min_length=1)
    source_segments: list[SegmentCitation] = Field(min_length=1, max_length=50)
    image: AssetCitation | None


class GeneratedLectureQuiz(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=300)
    questions: list[GeneratedQuestion] = Field(min_length=1, max_length=500)


class LectureGenerationError(SessionError):
    def __init__(
        self,
        code: str,
        *,
        objective_ids: tuple[str, ...] = (),
        counts: dict[str, int] | None = None,
    ):
        super().__init__(code)
        self.objective_ids = objective_ids
        self.counts = counts or {}


MAX_BATCH_OBJECTIVES = 25
MAX_BATCH_IMAGES = 20
MAX_SOURCE_CHARACTERS = 100_000


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _quiz_prompt() -> PromptSnapshot:
    path = Path(__file__).parent / "prompt_assets" / "gpt-lecture-quiz.md"
    payload = path.read_bytes()
    return PromptSnapshot(
        path,
        payload.decode("utf-8"),
        hashlib.sha256(payload).hexdigest(),
        datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
    )


def _write_record(path: Path, value: object) -> None:
    payload = _canonical(value).encode()
    if len(payload) > 2 * MAX_OUTPUT_BYTES:
        raise SessionError("context_limit")
    verified_atomic_write(payload, path)
    path.chmod(0o600)


def _read_record(path: Path) -> dict[str, object]:
    with path.open("rb") as source:
        raw = source.read(2 * MAX_OUTPUT_BYTES + 1)
    if len(raw) > 2 * MAX_OUTPUT_BYTES:
        raise ValueError("private artifact is oversized")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("private artifact must be an object")
    return value


def _batch_sources(
    inputs: LectureInputs,
) -> tuple[list[tuple[LectureInputs, str]], tuple[ParsedAsset, ...]]:
    bindings = {binding.snapshot.id: binding for binding in inputs.bindings}
    assets = tuple(
        sorted(
            (
                asset
                for document in inputs.documents
                if document.source_id == inputs.slide_source_id
                for asset in document.assets
            ),
            key=lambda asset: asset.key,
        )
    )
    sources = [
        {
            "source_id": document.source_id,
            "sha256": document.source_sha256,
            "title": bindings[document.source_id].snapshot.title,
            "role": bindings[document.source_id].role,
            "segments": [asdict(segment) for segment in document.segments],
        }
        for document in inputs.documents
    ]
    images = [
        {
            "source_id": inputs.slide_source_id,
            "asset_key": asset.key,
            "sha256": asset.sha256,
            "locator": asdict(asset.locator),
            "width": asset.width,
            "height": asset.height,
            "input_index": index,
        }
        for index, asset in enumerate(assets)
    ]
    batches = []
    for start in range(0, len(inputs.objectives), MAX_BATCH_OBJECTIVES):
        batch = replace(inputs, objectives=inputs.objectives[start : start + MAX_BATCH_OBJECTIVES])
        # ponytail: repeat full evidence; partition source groups only with an authoritative
        # objective/evidence map. Oversized lectures stop without dropping source material.
        source = _canonical(
            {
                "lecture_id": inputs.lecture_id,
                "subject": inputs.subject,
                "exam_number": inputs.exam_number,
                "image_required": inputs.image_required,
                "objectives": [{"id": key, "text": text} for key, text in batch.objectives],
                "sources": sources,
                "images": images,
                "run_styles": [sidecar.model_dump(mode="json") for sidecar in inputs.run_styles],
            }
        )
        if len(assets) > MAX_BATCH_IMAGES or len(source) > MAX_SOURCE_CHARACTERS:
            raise LectureGenerationError(
                "context_limit",
                objective_ids=tuple(key for key, _ in inputs.objectives),
                counts={
                    "images": len(assets),
                    "source_characters": len(source),
                    "objectives": len(inputs.objectives),
                },
            )
        batches.append((batch, source))
    if len(batches) * 3 > 500:
        raise LectureGenerationError(
            "context_limit",
            objective_ids=tuple(key for key, _ in inputs.objectives),
            counts={"minimum_questions": len(batches) * 3},
        )
    return batches, assets


def _cached_batch(
    directory: Path, descriptor: dict[str, object], inputs: LectureInputs
) -> GeneratedLectureQuiz:
    record = _read_record(directory / "complete.json")
    if (
        record.get("descriptor") != descriptor
        or record.get("quiz_sha256") != _digest(record.get("quiz"))
        or record.get("raw_sha256") != sha256_file(directory / "raw.txt")
    ):
        raise ValueError("completed batch evidence does not match its immutable binding")
    quiz = GeneratedLectureQuiz.model_validate(record["quiz"])
    validate_generated_quiz(quiz, inputs, require_images=False)
    return quiz


def generate_lecture_quiz(
    client: CodexSessionClient,
    request_id: str,
    model: str,
    inputs: LectureInputs,
    *,
    cancelled: Callable[[], bool],
    on_lifecycle: Callable[[SessionLifecycle], None],
    artifact_root: Path | None = None,
    resume: bool = False,
) -> GeneratedLectureQuiz:
    """Produce a private validated draft; this function never reviews or publishes it."""
    if not request_id.strip() or not model.strip():
        raise SessionError("invalid_output")
    manifest = source_manifest(inputs)
    batches, assets = _batch_sources(inputs)
    prompt = _quiz_prompt()
    schema = GeneratedLectureQuiz.model_json_schema()
    binding = {
        "manifest_sha256": manifest["sha256"],
        "prompt_sha256": prompt.sha256,
        "requested_model": model,
        "schema_sha256": _digest(schema),
    }
    root = (artifact_root or client.work_root / "gpt-artifacts") / hashlib.sha256(
        request_id.encode()
    ).hexdigest()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    descriptors = [
        {
            **binding,
            "batch_sha256": _digest(source),
            "batch_index": index,
            "request_id": f"{request_id}:batch-{index + 1:04d}",
        }
        for index, (_, source) in enumerate(batches)
    ]
    questions: list[GeneratedQuestion] = []
    titles: list[str] = []
    try:
        plan = {"request_id": request_id, "batches": descriptors}
        try:
            with (root / "plan.json").open("xb") as file:
                file.write(_canonical(plan).encode())
                file.flush()
                os.fsync(file.fileno())
            previously_started = False
        except FileExistsError:
            if _read_record(root / "plan.json") != plan:
                raise ValueError("run identity is bound to another generation plan") from None
            previously_started = True
        (root / "plan.json").chmod(0o600)
        _write_record(root / "manifest.json", manifest)
        verified_atomic_write(prompt.content.encode(), root / "prompt.txt")
        (root / "prompt.txt").chmod(0o600)
        for index, ((batch_inputs, source), descriptor) in enumerate(
            zip(batches, descriptors, strict=True)
        ):
            if cancelled():
                raise SessionError("interrupted")
            batch_key = _digest(descriptor)
            directory = root / f"batch-{index + 1:04d}-{batch_key}"
            directory.mkdir(mode=0o700, exist_ok=True)
            if (directory / "complete.json").exists():
                quiz = _cached_batch(directory, descriptor, batch_inputs)
            else:
                if (directory / "invalid.json").exists():
                    invalid = _read_record(directory / "invalid.json")
                    missing = invalid.get("objective_ids", [])
                    if not isinstance(missing, list) or any(
                        not isinstance(key, str)
                        or key not in {key for key, _ in batch_inputs.objectives}
                        for key in missing
                    ):
                        raise ValueError("invalid batch diagnostic")
                    raise LectureGenerationError("invalid_output", objective_ids=tuple(missing))
                prior_preflights = set(directory.glob("preflight-*.json"))
                if (directory / "dispatch.json").exists() or (previously_started and not resume):
                    raise SessionError("interrupted")
                client.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with TemporaryDirectory(prefix="lecture-images-", dir=client.work_root) as staged:
                    paths = []
                    for image_index, asset in enumerate(assets):
                        assert asset.path is not None
                        with asset.path.open("rb") as source_image:
                            payload = source_image.read(MAX_QUIZ_IMAGE_BYTES + 1)
                        if hashlib.sha256(payload).hexdigest() != asset.sha256:
                            raise ValueError("source image changed before staging")
                        path = Path(staged) / f"image-{image_index:04d}.png"
                        verified_atomic_write(payload, path)
                        paths.append(path)
                    # Exclusive reservation prevents two callers queuing the same provider batch.
                    # Any partial/crashed reservation is ambiguous and may never be replayed.
                    try:
                        with (directory / "dispatch.json").open("xb") as file:
                            file.write(_canonical(descriptor).encode())
                            file.flush()
                            os.fsync(file.fileno())
                    except FileExistsError:
                        raise SessionError("interrupted") from None
                    lifecycle_seen = False

                    def record_lifecycle(event: SessionLifecycle) -> None:
                        nonlocal lifecycle_seen
                        # A failed observer is ambiguous too: mark before calling it.
                        lifecycle_seen = True
                        on_lifecycle(event)

                    try:
                        if set(directory.glob("preflight-*.json")) != prior_preflights:
                            # A concurrent failed owner retired its claim while we staged.
                            # Only a fresh explicit resume may use that released reservation.
                            raise SessionError("interrupted")
                        result = client.generate(
                            SessionRequest(
                                str(descriptor["request_id"]),
                                model,
                                prompt.content,
                                source,
                                image_paths=tuple(paths),
                                output_schema=schema,
                                image_sha256=tuple(asset.sha256 for asset in assets),
                            ),
                            cancelled=cancelled,
                            on_lifecycle=record_lifecycle,
                        )
                    except SessionError as error:
                        if not lifecycle_seen:
                            (directory / "dispatch.json").rename(
                                directory / f"preflight-{uuid4().hex}-{error.code}.json"
                            )
                        raise
                raw = result.text.encode("utf-8")
                verified_atomic_write(raw[:MAX_OUTPUT_BYTES], directory / "raw.txt")
                (directory / "raw.txt").chmod(0o600)
                provider = {
                    "descriptor": descriptor,
                    "requested_model": model,
                    "actual_model": None,
                    "model_evidence": "unverified",
                    "thread_id": result.thread_id,
                    "turn_id": result.turn_id,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "raw_truncated": len(raw) > MAX_OUTPUT_BYTES,
                }
                _write_record(directory / "provider.json", provider)
                quiz = None
                try:
                    if len(raw) > MAX_OUTPUT_BYTES:
                        raise ValueError("provider response exceeds the raw artifact ceiling")
                    quiz = GeneratedLectureQuiz.model_validate_json(raw)
                    validate_generated_quiz(quiz, batch_inputs, require_images=False)
                except ValueError as error:
                    missing = uncovered_objectives(
                        tuple(key for key, _ in batch_inputs.objectives),
                        tuple(tuple(question.objective_ids) for question in quiz.questions)
                        if quiz
                        else (),
                    )
                    _write_record(
                        directory / "invalid.json",
                        {
                            "code": "invalid_output",
                            "detail": str(error)[:4096],
                            "objective_ids": missing,
                        },
                    )
                    raise LectureGenerationError("invalid_output", objective_ids=missing) from None
                _write_record(
                    directory / "complete.json",
                    {
                        **provider,
                        "quiz": quiz.model_dump(mode="json"),
                        "quiz_sha256": _digest(quiz.model_dump(mode="json")),
                    },
                )
            assert quiz is not None
            titles.append(quiz.title)
            questions.extend(
                question.model_copy(update={"id": "q-" + _digest([batch_key, question.id])})
                for question in quiz.questions
            )
        if len(questions) > 500:
            raise LectureGenerationError(
                "context_limit",
                objective_ids=tuple(key for key, _ in inputs.objectives),
                counts={"questions": len(questions)},
            )
        final = GeneratedLectureQuiz(title=titles[0], questions=questions)
        validate_generated_quiz(final, inputs, require_images=inputs.image_required)
        _write_record(root / "quiz.json", final.model_dump(mode="json"))
        return final
    except OSError:
        raise SessionError("protocol_error") from None
    except ValueError:
        raise SessionError("invalid_output") from None


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def to_review_drafts(
    quiz: GeneratedLectureQuiz, inputs: LectureInputs
) -> tuple[QuestionDraftValue, ...]:
    from oms_hub.study_generation.practice_domain import (
        AnswerProvenance,
        QuestionDraft,
        QuestionSourceRef,
    )
    from oms_hub.study_generation.practice_review import _image_key

    validate_generated_quiz(quiz, inputs, require_images=inputs.image_required)
    segments = {
        (document.source_id, segment.key): segment
        for document in inputs.documents for segment in document.segments
    }
    drafts = []
    for question in quiz.questions:
        refs = tuple(
            QuestionSourceRef(citation.source_id, citation.segment_key,
                segments[citation.source_id, citation.segment_key].locator.label)
            for citation in question.source_segments
        )
        explanations = tuple(
            f"{choice}: {explanation}"
            for choice, explanation in zip(
                question.choices, question.distractor_explanations, strict=True
            )
        )
        drafts.append(QuestionDraft(
            question_id=question.id,
            original_identifier=question.id,
            stem=question.stem,
            choices=tuple(question.choices),
            correct_index=question.correct_index,
            rationale=question.rationale + "\n\n" + "\n\n".join(explanations),
            image_ref=QuizImageRef(_image_key(question.id), "Lecture slides",
                source_asset(inputs, question.image.source_id,
                    question.image.asset_key).locator.label, "Lecture source image")
                if question.image else None,
            source_refs=refs,
            answer_provenance=AnswerProvenance.GENERATED_BY_AI,
            extraction_confidence=0.0,
            diagnostics=(),
            verification_required=True,
            verified_at=None,
            answer_evidence=tuple(
                f"{ref.source_id}:{ref.segment_key} ({ref.locator})" for ref in refs
            ),
        ))
    return tuple(drafts)


def validate_generated_quiz(
    quiz: GeneratedLectureQuiz, inputs: LectureInputs, *, require_images: bool
) -> None:
    """Validate mechanical evidence/coverage; clinical correctness still requires review."""
    validate_lecture_inputs(inputs)
    # Revalidate even objects built by model_copy/model_construct or subsequently mutated lists.
    quiz = GeneratedLectureQuiz.model_validate(quiz.model_dump())
    if len(quiz.questions) < 3:
        raise ValueError("a lecture quiz requires at least three independent questions")
    ids = [question.id for question in quiz.questions]
    stems = [_normalized(question.stem) for question in quiz.questions]
    if any(key != key.strip() for key in ids) or len(set(ids)) != len(ids):
        raise ValueError("question ids must be stable and unique")
    if len(set(stems)) != len(stems):
        raise ValueError("duplicate normalized question stems")
    expected = {key for key, _ in inputs.objectives}
    for question in quiz.questions:
        if (
            len(set(question.objective_ids)) != len(question.objective_ids)
            or set(question.objective_ids) - expected
        ):
            raise ValueError("question contains duplicate or unknown objectives")
        if len({_normalized(choice) for choice in question.choices}) != len(question.choices):
            raise ValueError("duplicate normalized choices")
        if len(question.distractor_explanations) != len(question.choices) or any(
            not explanation.strip() for explanation in question.distractor_explanations
        ):
            raise ValueError("each choice requires a nonempty explanation")
    missing = uncovered_objectives(
        tuple(key for key, _ in inputs.objectives),
        tuple(tuple(question.objective_ids) for question in quiz.questions),
    )
    if missing:
        raise ValueError("source-gap blocker: uncovered objectives " + ", ".join(missing))
    parse_native_quiz(
        json.dumps(
            {
                "title": quiz.title,
                "questions": [
                    {
                        "stem": question.stem,
                        "choices": question.choices,
                        "correct_index": question.correct_index,
                        "rationale": question.rationale,
                    }
                    for question in quiz.questions
                ],
            }
        )
    )
    validate_source_references(
        ExtractionPayload(
            questions=tuple(
                ExtractedQuestion(
                    stem=question.stem,
                    choices=tuple(question.choices),
                    supplied_correct_index=question.correct_index,
                    rationale=question.rationale,
                    source_segments=tuple(question.source_segments),
                    candidate_assets=(question.image,) if question.image else (),
                    confidence=0.0,
                )
                for question in quiz.questions
            )
        ),
        documents=inputs.documents,
    )
    documents = {document.source_id: document for document in inputs.documents}
    for document in inputs.documents:
        if len({segment.key for segment in document.segments}) != len(document.segments) or len(
            {asset.key for asset in document.assets}
        ) != len(document.assets):
            raise ValueError("source citation keys are ambiguous")
    for question in quiz.questions:
        if question.image is None:
            continue
        if question.image.source_id != inputs.slide_source_id:
            raise ValueError("question image must belong to the selected lecture slides")
        document = documents[question.image.source_id]
        asset = next(asset for asset in document.assets if asset.key == question.image.asset_key)
        segments = [
            segment
            for citation in question.source_segments
            if citation.source_id == document.source_id
            for segment in document.segments
            if segment.key == citation.segment_key
        ]
        if not any(
            asset.key in segment.asset_keys
            or (asset.locator.page_number, asset.locator.slide_number)
            == (segment.locator.page_number, segment.locator.slide_number)
            for segment in segments
        ):
            raise ValueError("image is not associated with the question's cited source evidence")
    if require_images and not any(question.image for question in quiz.questions):
        raise ValueError("image quiz requires at least one source-supported image question")


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
                document = replace(
                    document,
                    assets=tuple(dict.fromkeys((*document.assets, *rendered.assets))),
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


class GptLectureWorker:
    """Adapt validated local GPT artifacts into the existing unresolved review queue."""

    def __init__(
        self,
        repository: StudioRepository,
        client: CodexSessionClient,
        load_inputs: Callable[[StudioRun], LectureInputs],
        model: str,
        image_service: StudioQuizImageService,
        artifact_root: Path,
    ):
        self.repository = repository
        self.client = client
        self.load_inputs = load_inputs
        self.model = model
        self.image_service = image_service
        self.artifact_root = artifact_root

    def _artifact(
        self, run_id: str, key: str, session: Session | None = None
    ) -> dict[str, object] | None:
        from sqlalchemy import select

        from oms_hub.models import StudioRunArtifactModel

        artifact = session.scalar(select(StudioRunArtifactModel).where(
            StudioRunArtifactModel.run_id == run_id,
            StudioRunArtifactModel.artifact_key == key,
        )) if session else self.repository.run_artifact(run_id, key)
        if artifact is None:
            return None
        if hashlib.sha256(artifact.payload_json.encode()).hexdigest() != artifact.signature_sha256:
            raise ValueError("GPT artifact checksum changed")
        payload = json.loads(artifact.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("GPT artifact must be an object")
        return payload

    def _save_artifact(self, run_id: str, key: str, payload: dict[str, object]) -> None:
        encoded = _canonical(payload)
        self.repository.save_run_artifact(
            run_id, key, hashlib.sha256(encoded.encode()).hexdigest(), encoded
        )

    def _selected_model(self, run_id: str, session: Session | None = None) -> str:
        settings = self._artifact(run_id, "gpt:settings", session)
        model = settings.get("model") if settings else None
        if not isinstance(model, str) or not model.strip():
            raise ValueError("frozen GPT model selection is missing")
        return model

    def _response(
        self, run_id: str, inputs: LectureInputs, session: Session | None = None
    ) -> GeneratedLectureQuiz | None:
        response = self._artifact(run_id, "gpt:response", session)
        if response is None:
            return None
        directory = self.artifact_root / hashlib.sha256(run_id.encode()).hexdigest()
        if (
            response.get("manifest_sha256") != source_manifest(inputs)["sha256"]
            or response.get("requested_model") != self._selected_model(run_id, session)
            or response.get("evidence_directory") != str(directory)
            or response.get("quiz_sha256") != _digest(response.get("quiz"))
            or _read_record(directory / "quiz.json") != response.get("quiz")
        ):
            raise ValueError("GPT response no longer matches its source and artifact evidence")
        quiz = GeneratedLectureQuiz.model_validate(response["quiz"])
        validate_generated_quiz(quiz, inputs, require_images=inputs.image_required)
        return quiz

    @staticmethod
    def _coverage(quiz: GeneratedLectureQuiz, inputs: LectureInputs) -> dict[str, object]:
        return {
            "manifest_sha256": source_manifest(inputs)["sha256"],
            "objectives": [
                {
                    "id": key,
                    "question_ids": [q.id for q in quiz.questions if key in q.objective_ids],
                    "source_segments": [
                        citation.model_dump(mode="json")
                        for q in quiz.questions if key in q.objective_ids
                        for citation in q.source_segments
                    ],
                }
                for key, _ in inputs.objectives
            ],
        }

    def run(self, run: StudioRun) -> None:
        from oms_hub.study_generation.practice_domain import QuizWorkflowKind
        from oms_hub.study_generation.practice_review import (
            PracticeReviewService,
            ReviewQuestion,
            _candidate_id,
        )
        from oms_hub.study_generation.quiz_import_worker import _drafts_json
        from oms_hub.study_generation.studio_domain import StudioRunState

        try:
            current = self.repository.get_run(run.id)
            if current.state is not StudioRunState.RUNNING:
                return
            if current.workflow_kind is not QuizWorkflowKind.LECTURE_GENERATION:
                raise ValueError("GPT worker requires a lecture generation run")
            inputs = self.load_inputs(current)
            model = self._selected_model(run.id)
            quiz = self._response(run.id, inputs)
            if quiz is None:
                quiz = generate_lecture_quiz(
                    self.client, run.id, model, inputs,
                    cancelled=lambda: self.repository.gpt_cancelled(run.id),
                    on_lifecycle=lambda event: self.repository.record_gpt_lifecycle(run.id, event),
                    artifact_root=self.artifact_root,
                    resume=self.repository.gpt_resume_requested(run.id),
                )
                self._save_artifact(run.id, "gpt:response", {
                    "manifest_sha256": source_manifest(inputs)["sha256"],
                    "requested_model": model,
                    "actual_model": None,
                    "model_evidence": "unverified",
                    "quiz": quiz.model_dump(mode="json"),
                    "quiz_sha256": _digest(quiz.model_dump(mode="json")),
                    "evidence_directory": str(
                        self.artifact_root / hashlib.sha256(run.id.encode()).hexdigest()
                    ),
                })
            if self.repository.gpt_cancelled(run.id):
                raise SessionError("interrupted")
            # Recheck current source ownership after generation, before entering review.
            if source_manifest(self.load_inputs(current)) != source_manifest(inputs):
                raise ValueError("lecture source changed during generation")
            drafts = to_review_drafts(quiz, inputs)
            self._save_artifact(run.id, "gpt:coverage", self._coverage(quiz, inputs))
            normalized = _drafts_json(drafts)
            self.repository.save_run_artifact(run.id, "normalized",
                hashlib.sha256(normalized.encode()).hexdigest(), normalized)
            review = PracticeReviewService(self.repository, self.image_service)
            if self.repository.run_artifact(run.id, "review:questions") is None:
                questions = []
                for generated, draft in zip(quiz.questions, drafts, strict=True):
                    candidate_id = None
                    if generated.image is not None:
                        asset = source_asset(inputs, generated.image.source_id,
                            generated.image.asset_key)
                        assert asset.path is not None and draft.image_ref is not None
                        copied = self.image_service.copy_import_candidate(
                            run.id, draft.image_ref.key, draft.image_ref.source_title,
                            draft.image_ref.locator, draft.image_ref.description,
                            asset.path, asset.sha256, asset.key,
                        )
                        if copied.sha256 != asset.sha256:
                            raise ValueError("review image differs from the approved source image")
                        candidate_id = _candidate_id(draft.question_id,
                            generated.image.source_id, generated.image.asset_key)
                    questions.append(ReviewQuestion(draft,
                        learning_objective=", ".join(generated.objective_ids),
                        chosen_image=draft.image_ref, selected_candidate_id=candidate_id))
                review.store_review(run.id, tuple(questions))
            self.repository.await_import_review(run.id,
                tuple(question.draft for question in review.review(run.id)))
        except SessionError as error:
            self.repository.stop_gpt_run(run.id, error)
        except Exception:
            # Persist only a safe failure code; private source/output stays in private artifacts.
            self.repository.stop_gpt_run(run.id, SessionError("invalid_output"))

    def validate_review(
        self, run_id: str, questions: tuple[ReviewQuestion, ...], session: Session | None = None
    ) -> None:
        from sqlalchemy import select

        from oms_hub.models import StudioQuizImageRequirementModel
        from oms_hub.study_generation.practice_domain import AnswerProvenance, QuestionDraft
        from oms_hub.study_generation.practice_review import _candidate_id, _image_key

        if session is None:
            with self.repository.database.session() as owned:
                self.validate_review(run_id, questions, owned)
            return
        self.repository.validate_gpt_manifest_in_session(session, run_id)
        inputs = self.load_inputs(self.repository.get_run(run_id))
        original = self._response(run_id, inputs, session)
        if original is None or self._artifact(run_id, "gpt:coverage", session) != self._coverage(
            original, inputs
        ):
            raise ValueError("original GPT response or coverage evidence is missing")
        originals = {question.id: question for question in original.questions}
        original_drafts = {draft.question_id: draft for draft in to_review_drafts(original, inputs)}
        revised = []
        for question in questions:
            draft = question.draft
            original_question = originals.get(draft.question_id)
            if not isinstance(draft, QuestionDraft) or original_question is None:
                raise ValueError("review question has no original lecture provenance")
            if draft.correct_index is None or not draft.rationale:
                raise ValueError("lecture answer and explanation are incomplete")
            if (
                draft.source_refs != original_drafts[draft.question_id].source_refs
                or question.learning_objective != ", ".join(original_question.objective_ids)
                or not draft.verification_required
                or draft.answer_provenance not in {
                    AnswerProvenance.GENERATED_BY_AI, AnswerProvenance.MANUALLY_CORRECTED
                }
            ):
                raise ValueError("lecture source and objective provenance cannot be replaced")
            image = None
            if question.chosen_image is not None:
                selected = next((asset for document in inputs.documents
                    if document.source_id == inputs.slide_source_id for asset in document.assets
                    if _candidate_id(draft.question_id, inputs.slide_source_id, asset.key)
                        == question.selected_candidate_id), None)
                if selected is None or question.chosen_image.key != _image_key(draft.question_id):
                    raise ValueError("selected image is not an original lecture slide asset")
                binding = session.scalar(select(StudioQuizImageRequirementModel).where(
                    StudioQuizImageRequirementModel.run_id == run_id,
                    StudioQuizImageRequirementModel.image_key == question.chosen_image.key,
                ))
                if (binding is None or binding.asset_sha256 != selected.sha256
                    or not binding.asset_path
                    or sha256_file(Path(binding.asset_path)) != selected.sha256):
                    raise ValueError("selected lecture image bytes or ownership changed")
                image = AssetCitation(source_id=inputs.slide_source_id, asset_key=selected.key)
            revised.append(GeneratedQuestion(
                id=draft.question_id, stem=draft.stem, choices=list(draft.choices),
                correct_index=draft.correct_index, rationale=draft.rationale,
                # Edited rationale is reviewed manually; original explanations remain immutable.
                distractor_explanations=[draft.rationale or ""] * len(draft.choices),
                objective_ids=original_question.objective_ids,
                source_segments=original_question.source_segments, image=image,
            ))
        validate_generated_quiz(GeneratedLectureQuiz(title=original.title, questions=revised),
            inputs, require_images=inputs.image_required)
