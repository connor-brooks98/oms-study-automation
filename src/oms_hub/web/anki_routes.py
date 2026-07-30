from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid5

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import Field

from oms_hub.anki.apply import ApplyCoordinator, ApplyGateway, ApplyResult
from oms_hub.anki.contracts import (
    AddNotesOperation,
    AddTagsOperation,
    ContractModel,
    CreateCurationJobRequest,
    RemoveTagsOperation,
    TagPatchContract,
)
from oms_hub.anki.domain import (
    ApplyState,
    Candidate,
    CurationJob,
    CurationStage,
    CurationState,
    GapCard,
    GapCardEdit,
    ReviewChangeSet,
    SourceEvidence,
    SourceReference,
    TagPatch,
)
from oms_hub.anki.envelope import (
    CurrentCollectionNote,
    EnvelopeBuilder,
    EnvelopeBuildError,
)
from oms_hub.anki.gaps import (
    GapCardProposal,
    GapValidationError,
    validate_gap_card_fields,
)
from oms_hub.anki.repository import (
    AnkiCurationRepository,
    InvalidCurationTransition,
)
from oms_hub.anki.stages import revision_fingerprint
from oms_hub.anki.tag_policy import TagPolicy, TagPolicyError, tag_hash
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.domain import ProviderName
from oms_hub.repositories import CatalogRepository

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class GapCardEditRequest(ContractModel):
    concept_id: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(min_length=1, max_length=10_000)]
    extra: Annotated[str, Field(max_length=20_000)]
    selected: bool


class ReviewChangeSetRequest(ContractModel):
    expected_revision: Annotated[int, Field(ge=0)]
    reviewer: Annotated[str, Field(min_length=1, max_length=200)] = "local-user"
    candidate_selections: dict[Annotated[int, Field(gt=0)], bool]
    gap_edits: tuple[GapCardEditRequest, ...]
    tag_patches: tuple[TagPatchContract, ...]

    def to_domain(self) -> ReviewChangeSet:
        return ReviewChangeSet(
            expected_revision=self.expected_revision,
            reviewer=self.reviewer,
            candidate_selections=dict(self.candidate_selections),
            gap_edits=tuple(
                GapCardEdit(
                    concept_id=edit.concept_id,
                    text=edit.text,
                    extra=edit.extra,
                    selected=edit.selected,
                )
                for edit in self.gap_edits
            ),
            tag_patches=tuple(_tag_patch(patch) for patch in self.tag_patches),
        )


class EnvelopeRequest(ContractModel):
    review_revision: Annotated[int, Field(ge=0)]


class ApplyConfirmationRequest(EnvelopeRequest):
    confirmation: Literal["APPLY TO ANKI"]


class RetrySyncRequest(ContractModel):
    confirmation: Literal["RETRY SYNC"]


def _repository(request: Request) -> AnkiCurationRepository:
    return cast(AnkiCurationRepository, request.app.state.anki_repository)


def _tag_policy(request: Request) -> TagPolicy:
    policy = getattr(request.app.state, "anki_tag_policy", None)
    if not isinstance(policy, TagPolicy):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Anki curation is not configured",
        )
    return policy


def _gateway(request: Request) -> ApplyGateway:
    runtime = getattr(request.app.state, "anki_runtime", None)
    gateway = getattr(runtime, "gateway", None)
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local Anki is not configured",
        )
    return cast(ApplyGateway, gateway)


def _coordinator(request: Request) -> ApplyCoordinator:
    coordinator = getattr(
        request.app.state,
        "anki_apply_coordinator",
        None,
    )
    if not isinstance(coordinator, ApplyCoordinator):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Anki apply is not configured",
        )
    return coordinator


@router.get("/anki", response_class=HTMLResponse)
def anki_page(request: Request) -> HTMLResponse:
    context = _page_context(request)
    context["jobs"] = [_job_payload(job) for job in _repository(request).list_jobs(limit=50)]
    return templates.TemplateResponse(
        request=request,
        name="anki.html",
        context=context,
    )


@router.get("/anki/jobs/{job_id}", response_class=HTMLResponse)
def anki_review_page(request: Request, job_id: UUID) -> HTMLResponse:
    job = _require_job(_repository(request), job_id)
    return templates.TemplateResponse(
        request=request,
        name="anki_review.html",
        context={
            **_page_context(request),
            "job": _job_payload(job),
        },
    )


@router.get("/api/anki/bootstrap")
def anki_bootstrap(request: Request) -> dict[str, Any]:
    context = _page_context(request)
    return {
        "enabled": context["anki_enabled"],
        "lectures": context["lectures"],
        "defaults": context["defaults"],
        "tag_policy": context["tag_policy"],
    }


@router.get("/api/anki/jobs")
def list_anki_jobs(
    request: Request,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return {"jobs": [_job_payload(job) for job in _repository(request).list_jobs(limit=limit)]}


@router.post(
    "/api/anki/jobs",
    status_code=status.HTTP_201_CREATED,
)
def create_anki_job(
    request: Request,
    payload: CreateCurationJobRequest,
) -> dict[str, Any]:
    companion = getattr(request.app.state, "anki_companion_index", None)
    semantic_store = getattr(request.app.state, "anki_semantic_store", None)
    if companion is None or semantic_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Build the local Anki index before starting curation",
        )
    snapshot_id = companion.snapshot_id()
    if snapshot_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The companion index has no active snapshot",
        )
    if payload.index_snapshot_id != snapshot_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The selected Anki snapshot is stale; refresh and try again",
        )
    try:
        semantic = semantic_store.load()
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The semantic Anki index is unavailable",
        ) from exc

    revisions = IngestionRepository(request.app.state.database)
    hashes: dict[int, str] = {}
    for revision_id in payload.source_revision_ids:
        try:
            revision = revisions.get_study_revision(revision_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Source revision {revision_id} was not found",
            ) from exc
        if revision.lecture_id != payload.lecture_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Source revision {revision_id} belongs to another lecture",
            )
        if not revision.current:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Source revision {revision_id} is no longer current",
            )
        hashes[revision_id] = revision_fingerprint(revision)

    domain = replace(
        payload.to_domain(),
        source_revision_hashes=hashes,
        semantic_generation=str(semantic.manifest.generation),
        companion_generation=snapshot_id,
    )
    try:
        job = _repository(request).create_job(domain)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lecture was not found",
        ) from exc
    return _job_payload(job)


@router.get("/api/anki/jobs/{job_id}")
def read_anki_job(request: Request, job_id: UUID) -> dict[str, Any]:
    repository = _repository(request)
    job = _require_job(repository, job_id)
    envelope = repository.get_job_envelope(job_id)
    return {
        **_job_payload(job),
        "counts": _review_counts(repository, job_id),
        "envelope": (
            None
            if envelope is None
            else {
                "id": str(envelope.id),
                "state": envelope.state,
                "summary": envelope.receipt_summary,
                "plan_summary": _envelope_summary(repository.get_envelope(envelope.id)),
            }
        ),
        "recovery": _recovery_payload(job.apply_state),
    }


@router.post("/api/anki/jobs/{job_id}/cancel")
def cancel_anki_job(request: Request, job_id: UUID) -> dict[str, Any]:
    try:
        job = _repository(request).cancel_job(job_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Curation job was not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return _job_payload(job)


@router.get("/api/anki/jobs/{job_id}/review")
async def read_anki_review(
    request: Request,
    job_id: UUID,
) -> dict[str, Any]:
    repository = _repository(request)
    job = _require_job(repository, job_id)
    candidates = repository.list_candidates(job_id)
    gaps = repository.list_gap_cards(job_id)
    evidence = repository.list_source_evidence(job_id)
    reviewed_patches = _latest_tag_patches(repository.list_tag_patches(job_id))
    current = await _current_notes(
        _gateway(request),
        [candidate.note_id for candidate in candidates],
    )
    pass_1 = [
        _candidate_payload(
            request,
            candidate,
            current_note=current.get(candidate.note_id),
            reviewed_patch=reviewed_patches.get(candidate.note_id),
        )
        for candidate in candidates
        if candidate.retrieval_pass.value == "pass_1"
    ]
    pass_2 = [
        _candidate_payload(
            request,
            candidate,
            current_note=current.get(candidate.note_id),
            reviewed_patch=reviewed_patches.get(candidate.note_id),
        )
        for candidate in candidates
        if candidate.retrieval_pass.value == "pass_2_rescue"
    ]
    unresolved = _unresolved_payload(request, job_id, candidates, gaps, evidence)
    return {
        "job": _job_payload(job),
        "groups": {
            "pass_1_matches": pass_1,
            "recovered_in_pass_2": pass_2,
            "generated_cards": [_gap_payload(card, evidence) for card in gaps],
            "unresolved": unresolved,
        },
        "evidence": [_evidence_payload(item) for item in evidence],
        "tag_policy": _tag_policy_payload(_tag_policy(request)),
        "can_edit": job.state is CurationState.READY_FOR_REVIEW,
        "can_build_envelope": (job.state is CurationState.READY_FOR_REVIEW),
    }


@router.put("/api/anki/jobs/{job_id}/review")
async def save_anki_review(
    request: Request,
    job_id: UUID,
    payload: ReviewChangeSetRequest,
) -> dict[str, Any]:
    repository = _repository(request)
    job = _require_job(repository, job_id)
    if job.state is not CurationState.READY_FOR_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This review is already frozen or is not ready",
        )
    change_set = payload.to_domain()
    for edit in change_set.gap_edits:
        try:
            validate_gap_card_fields(edit.text.strip(), edit.extra.strip())
        except GapValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
    if change_set.tag_patches:
        current = await _current_notes(
            _gateway(request),
            [patch.note_id for patch in change_set.tag_patches],
        )
        policy = _tag_policy(request)
        for patch in change_set.tag_patches:
            try:
                policy.validate_tag_patch(
                    current[patch.note_id].tags,
                    patch,
                )
            except TagPolicyError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(exc),
                ) from exc
    try:
        saved = repository.save_review(job_id, change_set)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="A reviewed note or generated card was not found",
        ) from exc
    except ValueError as exc:
        code = (
            status.HTTP_409_CONFLICT
            if "stale" in str(exc).casefold()
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return {"job_id": str(saved.job_id), "revision": saved.revision}


@router.get("/api/anki/jobs/{job_id}/evidence/{evidence_id}")
def read_anki_evidence(
    request: Request,
    job_id: UUID,
    evidence_id: str,
) -> dict[str, Any]:
    evidence = next(
        (
            item
            for item in _repository(request).list_source_evidence(job_id)
            if item.evidence_id == evidence_id
        ),
        None,
    )
    if evidence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source evidence was not found",
        )
    return _evidence_payload(evidence)


@router.get("/api/anki/jobs/{job_id}/candidates/{note_id}")
def read_anki_candidate(
    request: Request,
    job_id: UUID,
    note_id: int,
) -> dict[str, Any]:
    candidate = next(
        (item for item in _repository(request).list_candidates(job_id) if item.note_id == note_id),
        None,
    )
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate note was not found",
        )
    return _candidate_payload(request, candidate)


@router.post(
    "/api/anki/jobs/{job_id}/envelope",
    status_code=status.HTTP_201_CREATED,
)
async def build_anki_envelope(
    request: Request,
    job_id: UUID,
    payload: EnvelopeRequest,
) -> dict[str, Any]:
    repository = _repository(request)
    job = _require_job(repository, job_id)
    if (
        job.state is not CurationState.READY_FOR_REVIEW
        or job.review_revision != payload.review_revision
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The review changed; reload before building the apply plan",
        )
    if repository.get_job_envelope(job_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job already has a frozen apply plan",
        )
    candidates = repository.list_candidates(job_id)
    patches = _latest_tag_patches(repository.list_tag_patches(job_id))
    affected = {candidate.note_id for candidate in candidates if candidate.selected} | set(patches)
    current = await _current_notes(_gateway(request), sorted(affected))
    evidence_ids = {item.evidence_id for item in repository.list_source_evidence(job_id)}
    try:
        proposals = tuple(
            _gap_proposal(card, job, evidence_ids)
            for card in repository.list_gap_cards(job_id)
            if card.selected
        )
        envelope = EnvelopeBuilder(_tag_policy(request)).build(
            ReviewChangeSet(
                expected_revision=job.review_revision,
                candidate_selections={
                    candidate.note_id: candidate.selected for candidate in candidates
                },
                tag_patches=tuple(patches.values()),
            ),
            current,
            envelope_id=uuid5(job.id, f"review:{job.review_revision}"),
            snapshot_id=job.index_snapshot_id,
            target_deck=job.target_deck,
            target_tag=job.target_tag,
            generated_cards=proposals,
        )
        stored = repository.create_action_envelope(
            job_id,
            envelope,
            expected_review_revision=payload.review_revision,
        )
    except (EnvelopeBuildError, GapValidationError, TagPolicyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except InvalidCurationTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return {
        "job_id": str(job_id),
        "envelope_id": str(stored.id),
        "payload_sha256": stored.payload_sha256,
        "summary": _envelope_summary(envelope),
    }


@router.post("/api/anki/jobs/{job_id}/apply")
async def apply_anki_envelope(
    request: Request,
    job_id: UUID,
    payload: ApplyConfirmationRequest,
) -> dict[str, Any]:
    repository = _repository(request)
    job = _require_job(repository, job_id)
    if job.review_revision != payload.review_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The review revision does not match the frozen plan",
        )
    stored = repository.get_job_envelope(job_id)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Build and inspect the apply plan first",
        )
    if job.state not in {
        CurationState.ENVELOPE_PENDING,
        CurationState.COMPLETE,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This apply is already in a recovery workflow",
        )
    result = await _coordinator(request).apply(stored.id)
    _advance_apply_state(repository, job_id, result)
    return _apply_payload(repository.require_job(job_id), result)


@router.post("/api/anki/jobs/{job_id}/retry-sync")
async def retry_anki_sync(
    request: Request,
    job_id: UUID,
    payload: RetrySyncRequest,
) -> dict[str, Any]:
    del payload
    repository = _repository(request)
    job = _require_job(repository, job_id)
    if job.apply_state not in {
        ApplyState.APPLIED_LOCAL_SYNC_RETRYABLE,
        ApplyState.APPLIED_LOCAL_SYNC_BLOCKED,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job does not have a pending sync retry",
        )
    stored = repository.get_job_envelope(job_id)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The frozen apply plan is unavailable",
        )
    result = await _coordinator(request).apply(stored.id)
    _advance_apply_state(repository, job_id, result)
    return _apply_payload(repository.require_job(job_id), result)


def _page_context(request: Request) -> dict[str, Any]:
    catalog = CatalogRepository(request.app.state.database)
    revisions = IngestionRepository(request.app.state.database)
    lectures = []
    for lecture in catalog.list_lectures():
        current = revisions.list_current_revisions(lecture.id)
        lectures.append(
            {
                "id": lecture.id,
                "subject": lecture.subject,
                "exam_number": lecture.exam_number,
                "lecture_number": lecture.lecture_number,
                "topic": lecture.topic,
                "revisions": [
                    {
                        "id": revision.id,
                        "kind": revision.kind.value,
                        "source_sha256": revision.source_sha256,
                    }
                    for revision in current
                ],
            }
        )
    settings = request.app.state.settings
    companion = getattr(request.app.state, "anki_companion_index", None)
    snapshot_id = companion.snapshot_id() if companion is not None else None
    return {
        "anki_enabled": (getattr(request.app.state, "anki_runtime", None) is not None),
        "lectures": lectures,
        "defaults": {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "lcl_prompt_version": "lcl-v1",
            "judgment_rubric_version": "judgment-v1",
            "gap_prompt_version": "gap-v1",
            "index_snapshot_id": snapshot_id,
            "semantic_model": settings.anki_semantic_model,
        },
        "tag_policy": _tag_policy_payload(getattr(request.app.state, "anki_tag_policy", None)),
    }


def _job_payload(job: CurationJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "lecture_id": job.lecture_id,
        "state": job.state.value,
        "apply_state": job.apply_state.value,
        "review_revision": job.review_revision,
        "attempts": job.attempts,
        "block_id": job.block_id,
        "source_revision_ids": list(job.source_revision_ids),
        "source_revision_hashes": {
            str(key): value for key, value in job.source_revision_hashes.items()
        },
        "deck_allowlist": list(job.deck_allowlist),
        "tag_allowlist": list(job.tag_allowlist),
        "target_deck": job.target_deck,
        "target_tag": job.target_tag,
        "index_snapshot_id": job.index_snapshot_id,
        "semantic_generation": job.semantic_generation,
        "companion_generation": job.companion_generation,
        "source_index_generation": job.source_index_generation,
        "provider": job.provider,
        "model": job.model,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _candidate_payload(
    request: Request,
    candidate: Candidate,
    *,
    current_note: CurrentCollectionNote | None = None,
    reviewed_patch: TagPatch | None = None,
) -> dict[str, Any]:
    companion = getattr(request.app.state, "anki_companion_index", None)
    note = companion.get_note(candidate.note_id) if companion is not None else None
    policy = _tag_policy(request)
    current_tags = (
        current_note.tags if current_note is not None else tuple(getattr(note, "tags", ()))
    )
    display_tags = reviewed_patch.after if reviewed_patch is not None else current_tags
    return {
        "note_id": candidate.note_id,
        "content_hash": candidate.content_hash,
        "concept_id": candidate.best_concept_id,
        "retrieval_pass": candidate.retrieval_pass.value,
        "selected": candidate.selected,
        "verdict": candidate.verdict,
        "confidence": candidate.confidence,
        "reason": candidate.reason,
        "scores": candidate.scores,
        "provenance": candidate.provenance,
        "context_trap": candidate.context_trap,
        "recall_direction": candidate.recall_direction,
        "mnemonic_classification": candidate.mnemonic_classification,
        "dedupe_disposition": candidate.dedupe_disposition,
        "note": (
            None
            if note is None
            else {
                "model_name": getattr(note, "model_name", ""),
                "text": getattr(note, "text", ""),
                "extra": getattr(note, "extra", ""),
                "fields": getattr(note, "raw_fields", {}),
                "deck_names": list(getattr(note, "deck_names", ())),
                "source_families": list(getattr(note, "source_families", ())),
                "tags": [
                    {
                        "value": tag,
                        "classification": policy.classify(tag),
                        "locked": policy.classify(tag)
                        not in {
                            "pipeline_owned",
                            "approved_optional",
                        },
                    }
                    for tag in display_tags
                ],
                "current_tags": list(current_tags),
                "tag_hash": tag_hash(current_tags),
            }
        ),
    }


def _gap_payload(
    card: GapCard,
    evidence: list[SourceEvidence],
) -> dict[str, Any]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    return {
        "concept_id": card.concept_id,
        "text": card.text,
        "extra": card.extra,
        "revision": card.revision,
        "selected": card.selected,
        "validation_state": card.validation_state,
        "evidence_ids": list(card.evidence_ids),
        "citations": [
            _evidence_payload(evidence_by_id[evidence_id])
            for evidence_id in card.evidence_ids
            if evidence_id in evidence_by_id
        ],
        "source_refs": [_source_reference_payload(reference) for reference in card.source_refs],
        "initial_tags": list(card.initial_tags),
        "provenance": card.provenance,
    }


def _evidence_payload(evidence: SourceEvidence) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "concept_id": evidence.concept_id,
        "support": evidence.support.value,
        "statement": evidence.statement,
        "source_refs": [_source_reference_payload(reference) for reference in evidence.source_refs],
        "content_hash": evidence.content_hash,
    }


def _source_reference_payload(
    reference: SourceReference,
) -> dict[str, Any]:
    return {
        "source_kind": reference.source_kind.value,
        "revision_id": reference.revision_id,
        "locator": reference.locator,
        "content_hash": reference.content_hash,
    }


def _unresolved_payload(
    request: Request,
    job_id: UUID,
    candidates: list[Candidate],
    gaps: list[GapCard],
    evidence: list[SourceEvidence],
) -> list[dict[str, Any]]:
    pipeline = getattr(request.app.state, "anki_curation_pipeline", None)
    artifacts = getattr(pipeline, "artifacts", None)
    if artifacts is not None:
        gap_artifact = next(
            (
                item
                for item in reversed(_repository(request).list_stage_artifacts(job_id))
                if item.stage is CurationStage.GAPS
            ),
            None,
        )
        if gap_artifact is not None:
            try:
                payload = artifacts.read(gap_artifact)
                unresolved = payload.get("unresolved", [])
                if isinstance(unresolved, list):
                    return [dict(item) for item in unresolved if isinstance(item, dict)]
            except (OSError, ValueError):
                pass
    resolved = {candidate.best_concept_id for candidate in candidates if candidate.selected} | {
        card.concept_id for card in gaps
    }
    return [
        {
            "concept_id": item.concept_id,
            "status": item.support.value,
            "reason": item.statement,
        }
        for item in evidence
        if item.concept_id not in resolved
    ]


def _tag_policy_payload(policy: object) -> dict[str, Any]:
    if not isinstance(policy, TagPolicy):
        return {
            "version": None,
            "editable": [],
            "protected": [],
        }
    return {
        "version": policy.version,
        "editable": [
            *policy.pipeline_owned_roots,
            *policy.approved_optional_roots,
        ],
        "protected": list(policy.source_managed_roots),
    }


def _tag_patch(patch: TagPatchContract) -> TagPatch:
    return TagPatch(
        note_id=patch.note_id,
        before=patch.before,
        after=patch.after,
        add_tags=patch.add_tags,
        remove_tags=patch.remove_tags,
        expected_tag_hash=patch.expected_tag_hash,
        tag_policy_version=patch.tag_policy_version,
    )


async def _current_notes(
    gateway: ApplyGateway,
    note_ids: list[int],
) -> dict[int, CurrentCollectionNote]:
    if not note_ids:
        return {}
    try:
        raw_notes = await gateway.notes_info(note_ids)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Current Anki note details could not be read",
        ) from exc
    parsed: dict[int, CurrentCollectionNote] = {}
    try:
        for raw in raw_notes:
            note_id = int(raw["noteId"])
            fields = {
                str(name): (str(value.get("value", "")) if isinstance(value, dict) else str(value))
                for name, value in cast(
                    dict[str, Any],
                    raw["fields"],
                ).items()
            }
            tags = tuple(str(tag) for tag in raw["tags"])
            parsed[note_id] = CurrentCollectionNote(
                note_id=note_id,
                fields=fields,
                tags=tags,
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Anki returned incomplete note details",
        ) from exc
    missing = set(note_ids) - set(parsed)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Reviewed notes are no longer present in Anki: "
                + ", ".join(str(note_id) for note_id in sorted(missing))
            ),
        )
    return parsed


def _latest_tag_patches(
    patches: list[TagPatch],
) -> dict[int, TagPatch]:
    return {patch.note_id: patch for patch in patches}


def _gap_proposal(
    card: GapCard,
    job: CurationJob,
    available_evidence_ids: set[str],
) -> GapCardProposal:
    validate_gap_card_fields(card.text.strip(), card.extra.strip())
    if not card.evidence_ids or not card.source_refs:
        raise GapValidationError(f"Generated card {card.concept_id} has no source citations")
    missing = set(card.evidence_ids) - available_evidence_ids
    if missing:
        raise GapValidationError(f"Generated card {card.concept_id} cites missing evidence")
    provider = ProviderName(str(card.provenance.get("provider", job.provider)))
    model = str(card.provenance.get("model", job.model)).strip()
    prompt_version = str(card.provenance.get("prompt_version", job.gap_prompt_version)).strip()
    confidence = float(card.provenance.get("confidence", 0.0))
    fields = {"Text": card.text.strip(), "Extra": card.extra.strip()}
    content_hash = hashlib.sha256(
        json.dumps(
            {"note_type": "Cloze", "fields": fields},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return GapCardProposal(
        concept_id=card.concept_id,
        note_type="Cloze",
        fields=fields,
        source_refs=card.source_refs,
        evidence_ids=card.evidence_ids,
        initial_tags=card.initial_tags,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        confidence=confidence,
        content_hash=content_hash,
        provenance=dict(card.provenance),
    )


def _envelope_summary(envelope: Any) -> dict[str, int]:
    created = 0
    added = 0
    removed = 0
    retagged: set[int] = set()
    for operation in envelope.operations:
        if isinstance(operation, AddNotesOperation):
            created += len(operation.notes)
        elif isinstance(operation, AddTagsOperation):
            added += len(operation.note_ids)
            retagged.update(operation.note_ids)
        elif isinstance(operation, RemoveTagsOperation):
            removed += len(operation.note_ids)
            retagged.update(operation.note_ids)
    return {
        "notes_created": created,
        "existing_notes_retagged": len(retagged),
        "tags_added": added,
        "tags_removed": removed,
    }


def _advance_apply_state(
    repository: AnkiCurationRepository,
    job_id: UUID,
    result: ApplyResult,
) -> None:
    job = repository.require_job(job_id)
    if result.state is ApplyState.FAILED_BEFORE_APPLY:
        return
    if job.state is CurationState.ENVELOPE_PENDING:
        job = repository.transition(
            job_id,
            CurationState.ENVELOPE_PENDING,
            CurationState.APPLYING_LOCAL,
        )
    if result.state in {
        ApplyState.APPLIED_LOCAL_SYNC_RETRYABLE,
        ApplyState.APPLIED_LOCAL_SYNC_BLOCKED,
    }:
        if job.state is CurationState.APPLYING_LOCAL:
            repository.transition(
                job_id,
                CurationState.APPLYING_LOCAL,
                CurationState.SYNCING,
            )
        return
    if result.state is not ApplyState.COMPLETE:
        return
    job = repository.require_job(job_id)
    if job.state is CurationState.APPLYING_LOCAL:
        job = repository.transition(
            job_id,
            CurationState.APPLYING_LOCAL,
            CurationState.SYNCING,
        )
    if job.state is CurationState.SYNCING:
        job = repository.transition(
            job_id,
            CurationState.SYNCING,
            CurationState.VERIFYING,
        )
    if job.state is CurationState.VERIFYING:
        repository.transition(
            job_id,
            CurationState.VERIFYING,
            CurationState.COMPLETE,
        )


def _apply_payload(job: CurationJob, result: ApplyResult) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "envelope_id": str(result.envelope_id),
        "state": job.state.value,
        "apply_state": result.state.value,
        "created_note_ids": list(result.created_note_ids),
        "differences": list(result.differences),
        "safe_error": result.safe_error,
        "recovery": _recovery_payload(result.state, result),
    }


def _recovery_payload(
    state: ApplyState,
    result: ApplyResult | None = None,
) -> dict[str, str]:
    if state is ApplyState.COMPLETE:
        return {
            "kind": "complete",
            "message": "Local changes, sync, and verification completed.",
        }
    if state is ApplyState.FAILED_BEFORE_APPLY:
        return {
            "kind": "no_changes",
            "message": "No local Anki changes were made.",
        }
    if state is ApplyState.APPLIED_LOCAL_SYNC_RETRYABLE:
        return {
            "kind": "retry_sync",
            "message": ("Local changes were made. Cloud sync should be retried."),
        }
    if state is ApplyState.APPLIED_LOCAL_SYNC_BLOCKED:
        return {
            "kind": "sync_blocked",
            "message": ("Local changes were made. Sync needs manual attention."),
        }
    if result is not None and result.differences:
        return {
            "kind": "verification_mismatch",
            "message": "Verification found a partial mismatch.",
        }
    if state is ApplyState.APPLY_PARTIAL:
        return {
            "kind": "manual_attention",
            "message": "Local changes may be partial and need manual attention.",
        }
    return {
        "kind": "pending",
        "message": "No Anki changes have been applied yet.",
    }


def _review_counts(
    repository: AnkiCurationRepository,
    job_id: UUID,
) -> dict[str, int]:
    candidates = repository.list_candidates(job_id)
    gaps = repository.list_gap_cards(job_id)
    return {
        "pass_1_matches": sum(item.retrieval_pass.value == "pass_1" for item in candidates),
        "recovered_in_pass_2": sum(
            item.retrieval_pass.value == "pass_2_rescue" for item in candidates
        ),
        "generated_cards": len(gaps),
        "selected_existing_notes": sum(item.selected for item in candidates),
        "selected_generated_cards": sum(item.selected for item in gaps),
    }


def _require_job(
    repository: AnkiCurationRepository,
    job_id: UUID,
) -> CurationJob:
    try:
        return repository.require_job(job_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Curation job was not found",
        ) from exc
