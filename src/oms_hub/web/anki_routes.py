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

from oms_hub.anki.ankiconnect import AnkiConnectError
from oms_hub.anki.apply import ApplyCoordinator, ApplyGateway, ApplyResult
from oms_hub.anki.card_centric import CardCentricValidationError, resolve_card_centric_scope
from oms_hub.anki.card_centric_contracts import CardConceptLedger
from oms_hub.anki.card_centric_fixture import FixtureUnavailable
from oms_hub.anki.card_centric_fixture_service import fixture_for, validate_fixture
from oms_hub.anki.card_centric_review import V3ReviewSnapshot
from oms_hub.anki.contracts import (
    ActionEnvelopeV2,
    AddNotesOperation,
    AddTagsOperation,
    ContractModel,
    CreateCurationJobRequest,
    RemoveTagsOperation,
    TagPatchContract,
)
from oms_hub.anki.convergence import ConvergenceState
from oms_hub.anki.domain import (
    ApplyState,
    Candidate,
    CurationJob,
    CurationStage,
    CurationState,
    GapCard,
    GapCardEdit,
    PipelineContractVersion,
    RetrievalPass,
    ReviewChangeSet,
    SourceEvidence,
    SourceReference,
    TagPatch,
)
from oms_hub.anki.envelope import (
    CurrentCollectionNote,
    EnvelopeBuilder,
    EnvelopeBuildError,
    rebind_add_only_envelope,
)
from oms_hub.anki.gaps import (
    GapCardProposal,
    GapValidationError,
    validate_gap_card_fields,
)
from oms_hub.anki.maintenance import LocalIndexRefreshError
from oms_hub.anki.paths import LectureIdentity, target_deck, target_tag
from oms_hub.anki.reconciliation import (
    CardCentricReconciliationInput,
    GeneratedResolution,
    ReconciliationInput,
    reconcile,
    reconcile_card_centric,
)
from oms_hub.anki.repository import (
    AnkiCurationRepository,
    InvalidCurationTransition,
    is_semantic_dedupe_retry_hold,
)
from oms_hub.anki.sources import NotebookSummaryParser, SummaryMalformedError
from oms_hub.anki.stages import revision_fingerprint
from oms_hub.anki.tag_policy import TagPolicy, TagPolicyError, tag_hash
from oms_hub.ingestion.domain import UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.domain import LLMTask, ProviderName
from oms_hub.llm.repository import LLMSettingsRepository
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import expanded_path
from oms_hub.study_generation.repository import GenerationRepository

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
GENERATED_NOTE_TYPE = (
    "AnKingOverhaul (OMS_II_Extra/JCBrooks) (OMS II Fall 2026 / jcbrooks)"
)


def _same_unique_identity_set(
    provided: tuple[object, ...],
    frozen: tuple[object, ...],
) -> bool:
    """Permit display/storage order changes without permitting identity drift."""
    return (
        len(provided) == len(frozen)
        and len(provided) == len(set(provided))
        and len(frozen) == len(set(frozen))
        and set(provided) == set(frozen)
    )


def _outline_ready_for_curation(
    outline: object,
    revisions: IngestionRepository,
) -> bool:
    """Check imported-outline links before exposing a lecture as curation-ready."""
    if not getattr(outline, "current", False):
        return False
    path = getattr(outline, "path", None)
    outline_sha256 = getattr(outline, "sha256", None)
    if not isinstance(path, Path) or not path.is_file():
        return False
    if getattr(outline, "provenance_kind", None) != "imported_notebooklm":
        return True
    immutable = getattr(outline, "immutable_path", None)
    slide_id = getattr(outline, "slide_revision_id", None)
    transcript_id = getattr(outline, "transcript_revision_id", None)
    slide_sha256 = getattr(outline, "slide_sha256", None)
    slide_source_sha256 = getattr(outline, "slide_source_sha256", None)
    transcript_sha256 = getattr(outline, "transcript_sha256", None)
    import_id = getattr(outline, "import_id", None)
    if (
        not isinstance(immutable, Path)
        or not immutable.is_file()
        or not isinstance(slide_id, int)
        or not isinstance(transcript_id, int)
        or not isinstance(slide_sha256, str)
        or not isinstance(slide_source_sha256, str)
        or not isinstance(transcript_sha256, str)
        or not isinstance(import_id, str)
        or not isinstance(outline_sha256, str)
    ):
        return False
    try:
        if (
            hashlib.sha256(immutable.read_bytes()).hexdigest() != outline_sha256
            or hashlib.sha256(path.read_bytes()).hexdigest() != outline_sha256
        ):
            return False
        slide = revisions.get_study_revision(slide_id)
        transcript = revisions.get_study_revision(transcript_id)
    except (KeyError, OSError):
        return False
    base_conditions = (
        slide.current
        and transcript.current
        and slide.kind is UploadKind.SLIDES
        and transcript.kind is UploadKind.TRANSCRIPTS
        and slide.source_sha256 == slide_source_sha256
        and slide.derived_sha256 == slide_sha256
        and transcript.derived_sha256 == transcript_sha256
        and transcript.provenance_kind == "imported_cleaned"
        and transcript.import_id == import_id
    )
    adopted_or_referenced = (
        slide.provenance_kind == "imported_derived"
        or revisions.has_imported_derived_audit(slide.id)
    )
    return base_conditions and (
        not adopted_or_referenced or revisions.imported_derived_audit_matches(slide)
    )


class GapCardEditRequest(ContractModel):
    card_id: Annotated[str, Field(max_length=100)] = ""
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
    overflow_acknowledgement: dict[str, Any] | None = None

    def to_domain(self) -> ReviewChangeSet:
        return ReviewChangeSet(
            expected_revision=self.expected_revision,
            reviewer=self.reviewer,
            candidate_selections=dict(self.candidate_selections),
            gap_edits=tuple(
                GapCardEdit(
                    card_id=edit.card_id,
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
    overflow_acknowledgement: dict[str, Any] | None = None


class OverflowAcknowledgementRequest(ContractModel):
    review_revision: Annotated[int, Field(ge=0)]
    selected_existing_note_ids: tuple[Annotated[int, Field(gt=0)], ...]
    selected_generated_card_ids: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...]


class FixtureValidationRequest(ContractModel):
    provider: Literal["openai", "gemini", "anthropic", "openrouter"]
    model: Annotated[str, Field(min_length=1, max_length=200)]


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


def _deny_rehearsal_apply(request: Request) -> None:
    if getattr(request.app.state, "anki_rehearsal_mode", "off") != "off":
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Anki apply and sync are hard-disabled in rehearsal mode",
        )


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
        "lecture_groups": context["lecture_groups"],
        "defaults": context["defaults"],
        "provider_models": context["provider_models"],
        "prompt_catalog": context["prompt_catalog"],
        "indexed_decks": context["indexed_decks"],
        "tag_policy": context["tag_policy"],
    }


@router.get("/api/anki/jobs")
def list_anki_jobs(
    request: Request,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return {"jobs": [_job_payload(job) for job in _repository(request).list_jobs(limit=limit)]}


@router.post("/api/anki/fixture-validation")
async def run_card_centric_fixture(
    request: Request, payload: FixtureValidationRequest
) -> dict[str, Any]:
    classifier = getattr(request.app.state, "card_centric_fixture_classifier", None)
    if classifier is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fixture classifier is not configured",
        )
    try:
        fixture = fixture_for(
            request.app.state.settings.anki_fixture_artifact_path,
            request.app.state.settings.anki_card_centric_fixture_sha256,
        )
        result = await validate_fixture(
            classifier, provider=payload.provider, model=payload.model, fixture=fixture
        )
    except FixtureUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    record = {
        "fixture_version": result.metrics["fixture_version"],
        "fixture_sha256": result.metrics["fixture_sha256"],
        "provider": payload.provider,
        "model": payload.model,
        "passed": result.passed,
        "metrics": result.metrics,
    }
    _repository(request).save_fixture_validation(payload.provider, payload.model, record)
    return record


@router.post(
    "/api/anki/jobs",
    status_code=status.HTTP_201_CREATED,
)
async def create_anki_job(
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
    refreshed = False
    maintainer = getattr(request.app.state, "anki_index_maintainer", None)
    if maintainer is not None:
        try:
            await maintainer.refresh()
        except (LocalIndexRefreshError, RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Anki sync and local index refresh did not complete",
            ) from exc
        refreshed = True
    snapshot_id = companion.snapshot_id()
    if snapshot_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The companion index has no active snapshot",
        )
    if not refreshed and payload.index_snapshot_id != snapshot_id:
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
    alignment = companion.semantic_alignment(
        note_ids=semantic.manifest.note_ids,
        content_hashes=semantic.manifest.content_hashes,
    )
    minimum_coverage = request.app.state.settings.anki_semantic_min_coverage
    if alignment.coverage < minimum_coverage:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{alignment.coverage:.3%} semantic coverage is below "
                f"the required {minimum_coverage:.3%}; refresh the local "
                "Anki index before starting curation"
            ),
        )

    revisions = IngestionRepository(
        request.app.state.database,
        artifact_v2_root=expanded_path(request.app.state.settings.data_dir) / "artifacts" / "v2",
        study_root=expanded_path(request.app.state.settings.study_root),
        icloud_root=(
            expanded_path(request.app.state.settings.icloud_staging_root)
            if request.app.state.settings.icloud_staging_root is not None
            else None
        ),
    )
    hashes: dict[int, str] = {}
    selected_kinds: set[UploadKind] = set()
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
        if not revision.immutable_source_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Source revision {revision_id} file is unavailable",
            )
        if (
            revision.provenance_kind == "imported_derived"
            or revisions.has_imported_derived_audit(revision.id)
        ) and not revisions.imported_derived_audit_matches(revision):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Source revision {revision_id} imported-derived audit is not ready",
            )
        selected_kinds.add(revision.kind)
        hashes[revision_id] = revision_fingerprint(revision)

    required_kinds = {UploadKind.SLIDES, UploadKind.TRANSCRIPTS}
    if not required_kinds.issubset(selected_kinds):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Curation requires the current lecture slides, transcript, and NotebookLM outline"
            ),
        )

    outlines = GenerationRepository(request.app.state.database)
    outline = outlines.current_outline(payload.lecture_id)
    if outline is None or not _outline_ready_for_curation(outline, revisions):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Curation requires a complete current NotebookLM outline PDF",
        )
    if payload.summary_outline_id not in {None, outline.id} or (
        payload.summary_outline_sha256 not in {None, outline.sha256}
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The selected NotebookLM outline is stale; reload and try again",
        )
    try:
        NotebookSummaryParser().parse(outline)
    except SummaryMalformedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"summary_malformed: {exc}",
        ) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"NotebookLM outline could not be validated: {exc}",
        ) from exc

    llm_settings = cast(
        LLMSettingsRepository,
        request.app.state.llm_settings,
    )
    resolved_model = (
        payload.model if payload.model else llm_settings.assignment(LLMTask.ANKI_CURATION).model
    )
    resolved_tag_allowlist = payload.tag_allowlist
    if (
        payload.pipeline_contract_version
        in {
            PipelineContractVersion.CARD_CENTRIC_V1.value,
            PipelineContractVersion.CARD_CENTRIC_V2.value,
        }
        and not resolved_tag_allowlist
    ):
        lecture = CatalogRepository(request.app.state.database).get_lecture(payload.lecture_id)
        if lecture is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Lecture was not found",
            )
        try:
            resolved_tag_allowlist = resolve_card_centric_scope(
                tag_allowlist=(), subject=lecture.subject, topic=lecture.topic
            )
        except CardCentricValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
    try:
        domain = replace(
            payload.to_domain(model=resolved_model),
            index_snapshot_id=snapshot_id,
            tag_allowlist=resolved_tag_allowlist,
            source_revision_hashes=hashes,
            semantic_generation=str(semantic.manifest.generation),
            companion_generation=snapshot_id,
            summary_outline_id=outline.id,
            summary_outline_sha256=outline.sha256,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    assert domain.resolved_model_config is not None
    if domain.pipeline_contract_version in {
        PipelineContractVersion.CARD_CENTRIC_V1,
        PipelineContractVersion.CARD_CENTRIC_V2,
    } and (
        domain.resolved_model_config.classify_s4.provider,
        domain.resolved_model_config.classify_s4.model,
    ) != (
        domain.resolved_model_config.ledger_s2.provider,
        domain.resolved_model_config.ledger_s2.model,
    ):
        record = _repository(request).fixture_validation(
            domain.resolved_model_config.classify_s4.provider,
            domain.resolved_model_config.classify_s4.model,
        )
        try:
            current_fixture = fixture_for(
                request.app.state.settings.anki_fixture_artifact_path,
                request.app.state.settings.anki_card_centric_fixture_sha256,
            )
        except FixtureUnavailable:
            current_fixture = None
        if (
            not record
            or not record.get("passed")
            or current_fixture is None
            or record.get("fixture_version") != current_fixture.version
            or record.get("fixture_sha256") != current_fixture.sha256
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="S4/S6 cheaper model is unvalidated for the current Lecture07 fixture",
            )
    try:
        job = _repository(request).create_job(domain)
        if job.pipeline_contract_version in {
            PipelineContractVersion.CARD_CENTRIC_V1,
            PipelineContractVersion.CARD_CENTRIC_V2,
        }:
            _repository(request).save_card_centric_profile(job.resolved_model_config)
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
        "reconciliation": _reconciliation_summary(request, job_id),
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


@router.post("/api/anki/jobs/{job_id}/retry")
def retry_anki_job(request: Request, job_id: UUID) -> dict[str, Any]:
    try:
        job = _repository(request).retry_job(job_id)
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


@router.post("/api/anki/jobs/{job_id}/remove")
def remove_failed_anki_job(
    request: Request,
    job_id: UUID,
) -> dict[str, Any]:
    try:
        _repository(request).remove_failed_job(job_id)
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
    return {"job_id": str(job_id), "removed": True}


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
    if job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3:
        committed = _reconciliation_summary(request, job_id) or {}
        snapshot_payload = committed.get("snapshot")
        try:
            snapshot = V3ReviewSnapshot.model_validate(snapshot_payload)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="v3 review requires a committed R11 snapshot",
            ) from exc
        r11_artifact_sha256 = committed.get("r11_artifact_sha256") or committed.get(
            "artifact_sha256"
        )
        if not (
            isinstance(r11_artifact_sha256, str)
            and len(r11_artifact_sha256) == 64
            and isinstance(committed.get("cost_ledger_sha256"), str)
            and len(cast(str, committed["cost_ledger_sha256"])) == 64
            and snapshot.snapshot_sha256
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="v3 review requires a committed R11 identity",
            )
        reviewed = _review_reconciliation_summary(committed, gaps, candidates)
        return {
            "job": _job_payload(job),
            "convergence": _convergence_summary(request, job_id),
            "reconciliation": _v3_reconciliation_payload(reviewed, job),
            "review_surface": _review_surface_payload(request, job, reviewed),
            "groups": {
                "pass_1_matches": [
                    _candidate_payload(request, candidate, include_note=False)
                    for candidate in candidates
                    if candidate.retrieval_pass.value == "pass_1"
                ],
                "recovered_in_pass_2": [
                    _candidate_payload(request, candidate, include_note=False)
                    for candidate in candidates
                    if candidate.retrieval_pass
                    in {RetrievalPass.PASS_2_RESCUE, RetrievalPass.CONVERGENCE}
                ],
                "generated_cards": [_gap_payload(card, evidence) for card in gaps],
                "unresolved": _unresolved_payload(request, job_id, candidates, gaps, evidence),
            },
            "concepts": _concept_review_groups(candidates, gaps),
            "evidence": [_evidence_payload(item) for item in evidence],
            "tag_policy": _tag_policy_payload(_tag_policy(request)),
            "can_edit": job.state is CurationState.READY_FOR_REVIEW,
            "can_build_envelope": False,
            "envelope_reason": (
                "card_centric_v3 review is approval-only; Anki apply is not available"
            ),
        }
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
        if candidate.retrieval_pass in {RetrievalPass.PASS_2_RESCUE, RetrievalPass.CONVERGENCE}
    ]
    unresolved = _unresolved_payload(request, job_id, candidates, gaps, evidence)
    reconciliation = _reconciliation_summary(request, job_id)
    if reconciliation is not None:
        reconciliation = _review_reconciliation_summary(
            reconciliation,
            gaps,
            candidates,
        )
    semantic_dedupe_hold = is_semantic_dedupe_retry_hold(job)
    reconciliation_allows_review = not semantic_dedupe_hold and (
        reconciliation is None or bool(reconciliation.get("can_render_envelope", False))
    )
    return {
        "job": _job_payload(job),
        "convergence": _convergence_summary(request, job_id),
        "reconciliation": reconciliation,
        "review_surface": _review_surface_payload(request, job, reconciliation),
        "groups": {
            "pass_1_matches": pass_1,
            "recovered_in_pass_2": pass_2,
            "generated_cards": [_gap_payload(card, evidence) for card in gaps],
            "unresolved": unresolved,
        },
        "concepts": _concept_review_groups(
            candidates,
            gaps,
            _card_ledger_concept_ids(request, job_id),
        ),
        "evidence": [_evidence_payload(item) for item in evidence],
        "tag_policy": _tag_policy_payload(_tag_policy(request)),
        "can_edit": job.state is CurationState.READY_FOR_REVIEW and not semantic_dedupe_hold,
        "can_build_envelope": (
            job.state is CurationState.READY_FOR_REVIEW
            and reconciliation_allows_review
        ),
        "envelope_reason": None,
    }


@router.put("/api/anki/jobs/{job_id}/review")
async def save_anki_review(
    request: Request,
    job_id: UUID,
    payload: ReviewChangeSetRequest,
) -> dict[str, Any]:
    repository = _repository(request)
    job = _require_job(repository, job_id)
    if is_semantic_dedupe_retry_hold(job):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Semantic dedupe must be retried before this job can be reviewed",
        )
    if job.state is not CurationState.READY_FOR_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This review is already frozen or is not ready",
        )
    change_set = payload.to_domain()
    committed: dict[str, Any] = {}
    if job.pipeline_contract_version in {
        PipelineContractVersion.CARD_CENTRIC_V1,
        PipelineContractVersion.CARD_CENTRIC_V2,
        PipelineContractVersion.CARD_CENTRIC_V3,
    }:
        committed = _reconciliation_summary(request, job_id) or {}
    if job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3:
        snapshot = committed.get("snapshot")
        try:
            v3_snapshot = V3ReviewSnapshot.model_validate(snapshot)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="v3 review requires a committed R11 snapshot",
            ) from exc
        r11_artifact_sha256 = committed.get("r11_artifact_sha256") or committed.get(
            "artifact_sha256"
        )
        if not (
            isinstance(r11_artifact_sha256, str)
            and len(r11_artifact_sha256) == 64
            and isinstance(committed.get("cost_ledger_sha256"), str)
            and len(cast(str, committed["cost_ledger_sha256"])) == 64
            and v3_snapshot.snapshot_sha256
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="v3 review requires a committed R11 identity",
            )
        if change_set.tag_patches:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="v3 review is approval-only and does not support tag patches",
            )
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
        snapshot = committed.get("snapshot") if committed else None
        saved = repository.save_review(
            job_id,
            change_set,
            card_centric_snapshot=snapshot if isinstance(snapshot, dict) else None,
            v3_review_artifact_sha256=(
                committed.get("r11_artifact_sha256") or committed.get("artifact_sha256")
                if job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3
                else None
            ),
            v3_cost_ledger_sha256=(
                committed.get("cost_ledger_sha256")
                if job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3
                else None
            ),
        )
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


@router.post("/api/anki/jobs/{job_id}/overflow-acknowledgement")
def issue_anki_overflow_acknowledgement(
    request: Request,
    job_id: UUID,
    payload: OverflowAcknowledgementRequest,
) -> dict[str, Any]:
    """Issue the one-time review document; clients never supply its signature."""
    repository = _repository(request)
    job = _require_job(repository, job_id)
    if job.pipeline_contract_version not in {
        PipelineContractVersion.CARD_CENTRIC_V1,
        PipelineContractVersion.CARD_CENTRIC_V2,
    }:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not a card-centric job")
    committed = _reconciliation_summary(request, job_id) or {}
    selection = cast(dict[str, Any], committed.get("selection", {}))
    cap = int(selection.get("cap", 70))
    mandatory_notes = tuple(selection.get("mandatory_note_ids", []))
    mandatory_generated = tuple(selection.get("mandatory_generated_card_ids", []))
    selected_notes = tuple(selection.get("selected_existing_note_ids", []))
    selected_generated = tuple(selection.get("selected_generated_card_ids", []))
    if job.pipeline_contract_version == PipelineContractVersion.CARD_CENTRIC_V2:
        try:
            snapshot = CardCentricReconciliationInput.model_validate(committed["snapshot"])
            if not (
                _same_unique_identity_set(
                    payload.selected_existing_note_ids, snapshot.selected_nids
                )
                and _same_unique_identity_set(
                    payload.selected_generated_card_ids,
                    snapshot.selected_generated_card_ids,
                )
            ):
                raise ValueError("request selection does not match the frozen identities")
            cap = snapshot.cap
            overflow = tuple(
                item
                for item in snapshot.selection_metadata
                if item.selected_position > snapshot.cap
            )
            mandatory_notes = tuple(
                int(item.identity.removeprefix("existing:"))
                for item in overflow
                if item.identity.startswith("existing:")
            )
            mandatory_generated = tuple(
                item.identity.removeprefix("generated:")
                for item in overflow
                if item.identity.startswith("generated:")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="persisted V2 selection cannot prove overflow scope",
            ) from exc
    elif (
        tuple(payload.selected_existing_note_ids) != selected_notes
        or tuple(payload.selected_generated_card_ids) != selected_generated
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="overflow acknowledgement must bind the frozen full selection",
        )
    try:
        document = repository.issue_card_centric_overflow_acknowledgement(
            job_id,
            review_revision=payload.review_revision,
            selected_note_ids=payload.selected_existing_note_ids,
            selected_generated_ids=payload.selected_generated_card_ids,
            mandatory_note_ids=mandatory_notes,
            mandatory_generated_ids=mandatory_generated,
            cap=cap,
        )
        repository.persist_card_centric_overflow_acknowledgement(
            job_id, review_revision=payload.review_revision, document=document
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return document


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
    if is_semantic_dedupe_retry_hold(job):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Semantic dedupe must be retried before building an apply plan",
        )
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
    if job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3:
        # Phase G deliberately exposes only the R12 approval seam.  In
        # particular, do not query Anki while a v3 review awaits approval.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "card_centric_v3 requires explicit revision-matching approval; "
                "no Anki action is available"
            ),
        )
    gap_cards = repository.list_gap_cards(job_id)
    reconciliation = _reconciliation_summary(request, job_id)
    if reconciliation is not None and job.pipeline_contract_version in {
        PipelineContractVersion.CARD_CENTRIC_V1,
        PipelineContractVersion.CARD_CENTRIC_V2,
    }:
        selection = cast(dict[str, Any], reconciliation.get("selection", {}))
        selected_notes = tuple(
            candidate.note_id
            for candidate in repository.list_candidates(job_id)
            if candidate.selected
        )
        selected_generated = tuple(card.card_id for card in gap_cards if card.selected)
        cap = int(selection.get("cap", 70))
        if len(selected_notes) + len(selected_generated) > cap and not (
            payload.overflow_acknowledgement
            and repository.validate_card_centric_overflow_acknowledgement(
                job_id,
                review_revision=job.review_revision,
                selected_note_ids=selected_notes,
                selected_generated_ids=selected_generated,
                cap=cap,
                document=payload.overflow_acknowledgement,
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="selection overflow acknowledgement is missing, stale, or forged",
            )
    if reconciliation is not None and job.pipeline_contract_version not in {
        PipelineContractVersion.CARD_CENTRIC_V1,
        PipelineContractVersion.CARD_CENTRIC_V2,
    }:
        reconciliation = _review_reconciliation_summary(
            reconciliation,
            gap_cards,
            repository.list_candidates(job_id),
            overflow_acknowledgement=payload.overflow_acknowledgement,
        )
    if reconciliation is not None and not reconciliation.get(
        "can_render_envelope",
        False,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Reconciliation failed; the apply plan is withheld",
        )
    if reconciliation is None and job.gap_prompt_version != "gap-v1":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A committed reconciliation report is required",
        )
    candidates = repository.list_candidates(job_id)
    patches = _latest_tag_patches(repository.list_tag_patches(job_id))
    affected = {candidate.note_id for candidate in candidates if candidate.selected} | set(patches)
    current = await _current_notes(_gateway(request), sorted(affected))
    evidence_ids = {item.evidence_id for item in repository.list_source_evidence(job_id)}
    try:
        proposals = tuple(
            _gap_proposal(card, job, evidence_ids) for card in gap_cards if card.selected
        )
        builder = EnvelopeBuilder(_tag_policy(request))
        changeset = ReviewChangeSet(
            expected_revision=job.review_revision,
            candidate_selections={
                candidate.note_id: candidate.selected for candidate in candidates
            },
            tag_patches=tuple(patches.values()),
        )
        envelope_id = uuid5(job.id, f"review:{job.review_revision}")
        envelope = (
            builder.build_v2(
                changeset,
                current,
                envelope_id=envelope_id,
                snapshot_id=job.index_snapshot_id,
                target_deck=job.target_deck,
                target_tag=job.target_tag,
                generated_cards=proposals,
                job_id=job.id,
                pipeline_contract_version=cast(
                    Literal["card_centric_v1", "card_centric_v2"],
                    job.pipeline_contract_version.value,
                ),
                model_config_sha256=job.model_config_sha256,
                resolved_model_config=job.resolved_model_config.canonical_document(),
                reconciliation_contract_version=str(
                    reconciliation.get("contract_version", "card_centric_s9_v1")
                )
                if reconciliation
                else "card_centric_s9_v1",
                review_revision=job.review_revision,
                overflow_acknowledgement_provenance=(
                    reconciliation.get("selection", {}).get("overflow_acknowledgement")
                    or {"required": False}
                )
                if reconciliation
                else {"required": False},
            )
            if job.pipeline_contract_version
            in {PipelineContractVersion.CARD_CENTRIC_V1, PipelineContractVersion.CARD_CENTRIC_V2}
            else builder.build(
                changeset,
                current,
                envelope_id=envelope_id,
                snapshot_id=job.index_snapshot_id,
                target_deck=job.target_deck,
                target_tag=job.target_tag,
                generated_cards=proposals,
            )
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
        "reconciliation": reconciliation,
    }


@router.post("/api/anki/jobs/{job_id}/envelope/rebind")
async def rebind_anki_envelope(
    request: Request,
    job_id: UUID,
    payload: EnvelopeRequest,
) -> dict[str, Any]:
    _deny_rehearsal_apply(request)
    repository = _repository(request)
    job = _require_job(repository, job_id)
    if (
        job.state is not CurationState.ENVELOPE_PENDING
        or job.apply_state is not ApplyState.FAILED_BEFORE_APPLY
        or job.review_revision != payload.review_revision
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only the unchanged failed-before-apply review can be rebound",
        )
    stored = repository.get_job_envelope(job_id)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The frozen apply plan is unavailable",
        )
    envelope = repository.get_envelope(stored.id)
    if not isinstance(envelope, ActionEnvelopeV2):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a V2 apply plan can be rebound",
        )
    gateway = _gateway(request)
    try:
        await gateway.sync()
    except AnkiConnectError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Anki sync did not complete; no mutation performed",
        ) from exc
    current = await _current_notes(gateway, sorted(envelope.touched_note_hashes))
    try:
        rebound = rebind_add_only_envelope(envelope, current)
        rebound_stored = repository.rebind_failed_before_apply_envelope(
            job_id,
            rebound,
            expected_payload_sha256=stored.payload_sha256,
        )
    except (EnvelopeBuildError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return {
        "job_id": str(job_id),
        "envelope_id": str(rebound_stored.id),
        "payload_sha256": rebound_stored.payload_sha256,
        "summary": _envelope_summary(rebound),
    }


@router.post("/api/anki/jobs/{job_id}/apply")
async def apply_anki_envelope(
    request: Request,
    job_id: UUID,
    payload: ApplyConfirmationRequest,
) -> dict[str, Any]:
    _deny_rehearsal_apply(request)
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
    resumable_partial = (
        job.state is CurationState.APPLYING_LOCAL
        and job.apply_state is ApplyState.APPLY_PARTIAL
    )
    if job.state not in {CurationState.ENVELOPE_PENDING, CurationState.COMPLETE} and not (
        resumable_partial
    ):
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
    _deny_rehearsal_apply(request)
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
    anki_repository = _repository(request)
    revisions = IngestionRepository(
        request.app.state.database,
        artifact_v2_root=expanded_path(request.app.state.settings.data_dir) / "artifacts" / "v2",
        study_root=expanded_path(request.app.state.settings.study_root),
        icloud_root=(
            expanded_path(request.app.state.settings.icloud_staging_root)
            if request.app.state.settings.icloud_staging_root is not None
            else None
        ),
    )
    outlines = GenerationRepository(request.app.state.database)
    lectures: list[dict[str, Any]] = []
    for lecture in catalog.list_lectures():
        current = revisions.list_current_revisions(lecture.id)
        outline = outlines.current_outline(lecture.id)
        current_kinds = {revision.kind for revision in current}
        slides_ready = UploadKind.SLIDES in current_kinds and all(
            not (
                revision.provenance_kind == "imported_derived"
                or revisions.has_imported_derived_audit(revision.id)
            )
            or revisions.imported_derived_audit_matches(revision)
            for revision in current
            if revision.kind is UploadKind.SLIDES
        )
        outline_available = outline is not None and _outline_ready_for_curation(outline, revisions)
        identity = LectureIdentity(
            course=lecture.subject,
            exam_number=lecture.exam_number,
            lecture_number=lecture.lecture_number,
            topic=lecture.topic,
        )
        lectures.append(
            {
                "id": lecture.id,
                "subject": lecture.subject,
                "exam_number": lecture.exam_number,
                "lecture_number": lecture.lecture_number,
                "topic": lecture.topic,
                "target_deck": target_deck(identity),
                "target_tag": target_tag(identity),
                "revisions": [
                    {
                        "id": revision.id,
                        "kind": revision.kind.value,
                        "source_sha256": revision.source_sha256,
                    }
                    for revision in current
                ],
                "outline": (
                    {
                        "id": outline.id,
                        "kind": "summary",
                        "sha256": outline.sha256,
                    }
                    if outline_available and outline is not None
                    else None
                ),
                "source_ready": (
                    slides_ready and UploadKind.TRANSCRIPTS in current_kinds and outline_available
                ),
                "source_status": {
                    "slides": slides_ready,
                    "transcripts": UploadKind.TRANSCRIPTS in current_kinds,
                    "summary": outline_available,
                },
            }
        )
    lecture_groups: list[dict[str, Any]] = []
    groups_by_course: dict[str, dict[str, Any]] = {}
    exams_by_course: dict[str, dict[int, dict[str, Any]]] = {}
    for lecture_payload in lectures:
        course = str(lecture_payload["subject"])
        course_group = groups_by_course.get(course)
        if course_group is None:
            course_group = {"course": course, "exams": []}
            groups_by_course[course] = course_group
            exams_by_course[course] = {}
            lecture_groups.append(course_group)
        exam_number = int(lecture_payload["exam_number"])
        exam_group = exams_by_course[course].get(exam_number)
        if exam_group is None:
            exam_group = {
                "exam_number": exam_number,
                "lectures": [],
            }
            exams_by_course[course][exam_number] = exam_group
            cast(list[dict[str, Any]], course_group["exams"]).append(exam_group)
        cast(list[dict[str, Any]], exam_group["lectures"]).append(lecture_payload)
    settings = request.app.state.settings
    llm_settings = cast(
        LLMSettingsRepository,
        request.app.state.llm_settings,
    )
    provider_preferences = llm_settings.list()
    active_provider = llm_settings.assignment(LLMTask.ANKI_CURATION)
    saved_profile = anki_repository.card_centric_profile()
    provider_models = {
        preference.provider.value: preference.model for preference in provider_preferences
    }
    companion = getattr(request.app.state, "anki_companion_index", None)
    snapshot_id = companion.snapshot_id() if companion is not None else None
    catalog = request.app.state.anki_prompt_catalog.catalog()
    catalog_payload = catalog.payload()
    indexed_decks = list(companion.list_deck_names()) if companion is not None else []
    fixture_available = False
    fixture_status = (
        "The private Lecture07 fixture is not installed and SHA-256 pinned, so "
        "fixture validation is unavailable."
    )
    try:
        fixture_for(
            settings.anki_fixture_artifact_path,
            settings.anki_card_centric_fixture_sha256,
        )
    except FixtureUnavailable:
        # The artifact is deliberately external to this repository.  Do not make
        # a missing or changed fixture look runnable in the curation UI.
        pass
    else:
        fixture_available = True
        fixture_status = "Immutable Lecture07 fixture is installed and SHA-256 pinned."

    def preferred_prompt(role: str, preferred: str) -> str:
        choices = cast(dict[str, list[dict[str, str]]], catalog_payload["choices"])[role]
        ids = [choice["id"] for choice in choices]
        return preferred if preferred in ids else (ids[0] if ids else "")

    return {
        "anki_enabled": (getattr(request.app.state, "anki_runtime", None) is not None),
        "lectures": lectures,
        "lecture_groups": lecture_groups,
        "defaults": {
            "provider": active_provider.provider.value,
            "model": active_provider.model,
            "lcl_prompt_version": preferred_prompt("lcl", "lecture-concept-ledger"),
            "judgment_rubric_version": preferred_prompt("coverage", "coverage-rubric"),
            "gap_prompt_version": preferred_prompt("gap_cards", "gap-card-generation"),
            "index_snapshot_id": snapshot_id,
            "semantic_model": settings.anki_semantic_model,
            "card_centric_profile": (
                saved_profile.canonical_document() if saved_profile is not None else None
            ),
        },
        "provider_models": provider_models,
        "prompt_catalog": catalog_payload,
        "indexed_decks": indexed_decks,
        "tag_policy": _tag_policy_payload(getattr(request.app.state, "anki_tag_policy", None)),
        "fixture_available": fixture_available,
        "fixture_status": fixture_status,
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
        "summary_outline_id": job.summary_outline_id,
        "summary_outline_sha256": job.summary_outline_sha256,
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
        "can_retry": job.state is CurationState.FAILED or is_semantic_dedupe_retry_hold(job),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _concept_review_groups(
    candidates: list[Candidate],
    gaps: list[GapCard],
    ledger_concept_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Small concept-first projection; selection/edit state remains canonical in rows."""
    groups: dict[str, dict[str, Any]] = {
        concept_id: {
            "concept_id": concept_id,
            "yes": [],
            "maybe": [],
            "flagged": [],
            "generated": [],
            "uncovered": True,
        }
        for concept_id in ledger_concept_ids
    }
    for candidate in candidates:
        audit = candidate.provenance.get("card_centric", {})
        verdict = str(audit.get("verdict", candidate.verdict)).upper()
        concept_ids = tuple(audit.get("covered_concept_ids", ())) or (candidate.best_concept_id,)
        summary = {
            "note_id": candidate.note_id,
            "reason": candidate.reason,
            "selected": candidate.selected,
        }
        for concept_id in concept_ids:
            item = groups.setdefault(
                concept_id,
                {
                    "concept_id": concept_id,
                    "yes": [],
                    "maybe": [],
                    "flagged": [],
                    "generated": [],
                    "uncovered": True,
                },
            )
            if audit.get("flags"):
                item["flagged"].append(summary)
            elif verdict == "YES":
                item["yes"].append(summary)
                item["uncovered"] = False
            elif verdict == "MAYBE":
                item["maybe"].append(summary)
    for gap in gaps:
        item = groups.setdefault(
            gap.concept_id,
            {
                "concept_id": gap.concept_id,
                "yes": [],
                "maybe": [],
                "flagged": [],
                "generated": [],
            },
        )
        item["generated"].append(
            {
                "card_id": gap.card_id,
                "selected": gap.selected,
                "validation_state": gap.validation_state,
            }
        )
        if gap.selected and gap.validation_state == "generated":
            item["uncovered"] = False
    for item in groups.values():
        for key in ("yes", "maybe", "flagged", "generated"):
            item[key].sort(key=lambda value: str(value.get("note_id", value.get("card_id", ""))))
    return [groups[key] for key in sorted(groups)]


def _card_ledger_concept_ids(request: Request, job_id: UUID) -> tuple[str, ...]:
    pipeline = getattr(request.app.state, "anki_curation_pipeline", None)
    artifacts = getattr(pipeline, "artifacts", None)
    if artifacts is None:
        return ()
    artifact = next(
        (
            item
            for item in reversed(_repository(request).list_stage_artifacts(job_id))
            if item.stage is CurationStage.CARD_LEDGER
        ),
        None,
    )
    if artifact is None:
        return ()
    try:
        ledger = CardConceptLedger.model_validate(artifacts.read(artifact).get("ledger"))
    except (OSError, TypeError, ValueError):
        return ()
    return tuple(concept.concept_id for concept in ledger.concepts)


def _candidate_payload(
    request: Request,
    candidate: Candidate,
    *,
    current_note: CurrentCollectionNote | None = None,
    reviewed_patch: TagPatch | None = None,
    include_note: bool = True,
) -> dict[str, Any]:
    companion = getattr(request.app.state, "anki_companion_index", None) if include_note else None
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
        "card_id": card.card_id,
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


_EVIDENCE_QUALITY_VALUES = frozenset({"primary_source", "summary_grounded", "fast_pass"})


def _v3_reconciliation_payload(
    reconciliation: dict[str, Any] | None, job: CurationJob
) -> dict[str, Any]:
    """Return only the v3 review identity; full R11 evidence stays in the repository."""
    value = reconciliation or {}
    snapshot = (
        cast(dict[str, Any], value.get("snapshot"))
        if isinstance(value.get("snapshot"), dict)
        else {}
    )
    existing = snapshot.get("existing_candidates", [])
    generated = snapshot.get("generated_cards", [])
    return {
        "contract_version": "card_centric_v3_r11",
        "review_revision": job.review_revision,
        "approval_only": True,
        "approval_state": "ready" if value.get("can_render_envelope") is True else "blocked",
        "r11_artifact_sha256": value.get("r11_artifact_sha256") or value.get("artifact_sha256"),
        "r11_snapshot_sha256": value.get("r11_snapshot_sha256") or snapshot.get("snapshot_sha256"),
        "cost_ledger_sha256": value.get("cost_ledger_sha256"),
        "existing_note_ids": [
            item.get("note_id")
            for item in existing
            if isinstance(item, dict) and isinstance(item.get("note_id"), int)
        ],
        "generated_card_ids": [
            item.get("card_id")
            for item in generated
            if isinstance(item, dict) and isinstance(item.get("card_id"), str)
        ],
        "selected_existing_note_ids": snapshot.get("selected_existing_note_ids", []),
        "selected_generated_card_ids": snapshot.get("selected_generated_card_ids", []),
    }


def _review_surface_payload(
    request: Request,
    job: CurationJob,
    reconciliation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Expose committed v2 review evidence without changing its outcome semantics."""
    if job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3:
        snapshot = (reconciliation or {}).get("snapshot")
        if not isinstance(snapshot, dict):
            return {"v3": {"approval_only": True, "reason": "R11 snapshot is unavailable"}}
        evidence = snapshot.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}

        def document(value: object) -> dict[str, Any]:
            return cast(dict[str, Any], value) if isinstance(value, dict) else {}

        scope = document(evidence.get("scope"))
        fidelity = document(evidence.get("r2_fidelity"))
        retrieval = document(evidence.get("retrieval"))
        calibration = document(evidence.get("calibration"))
        classification = document(evidence.get("classification"))
        enforcement = document(evidence.get("policy_enforcement"))
        gap_confirmation = document(evidence.get("gap_confirmation"))
        generation = document(evidence.get("generation"))
        calibration_records = calibration.get("records", [])
        calibration_records = calibration_records if isinstance(calibration_records, list) else []
        classification_calls = classification.get("calls", [])
        classification_calls = (
            classification_calls if isinstance(classification_calls, list) else []
        )
        escalations = classification.get("escalations", [])
        escalations = escalations if isinstance(escalations, list) else []
        v3_dedupe = document(evidence.get("dedupe"))
        dedupe_rows = v3_dedupe.get("resolutions", [])
        dedupe_rows = dedupe_rows if isinstance(dedupe_rows, list) else []
        generated_cards = snapshot.get("generated_cards", [])
        generated_cards = generated_cards if isinstance(generated_cards, list) else []
        scope_fact_ids = [
            fact.get("fact_id")
            for concept in scope.get("concepts", [])
            if isinstance(concept, dict)
            for fact in concept.get("facts", [])
            if isinstance(fact, dict) and isinstance(fact.get("fact_id"), str)
        ]
        generated_grounding = sorted(
            (
                {
                    "card_id": item["card_id"],
                    "fact_id": item["fact_id"],
                    "evidence_ids": item.get("evidence_ids", []),
                }
                for item in generated_cards
                if isinstance(item, dict)
                and isinstance(item.get("card_id"), str)
                and isinstance(item.get("fact_id"), str)
            ),
            key=lambda item: (str(item["fact_id"]), str(item["card_id"])),
        )
        unresolved: list[dict[str, object]] = []
        r8_rows = gap_confirmation.get("records", [])
        for item in r8_rows if isinstance(r8_rows, list) else []:
            if (
                isinstance(item, dict)
                and isinstance(item.get("fact_id"), str)
                and item.get("state")
                not in {"covered_initial", "covered_residual", "confirmed_missing"}
            ):
                unresolved.append(
                    {
                        "source": "r8",
                        "fact_id": item["fact_id"],
                        "state": item.get("state"),
                        "reason": item.get("reason"),
                    }
                )
        generation_rows = generation.get("resolutions", [])
        for source, rows in (("r9", generation_rows), ("r10", dedupe_rows)):
            for item in rows if isinstance(rows, list) else []:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("fact_id"), str)
                    and item.get("status") == "unresolved"
                ):
                    unresolved.append(
                        {
                            "source": source,
                            "fact_id": item["fact_id"],
                            "state": item["status"],
                            "reason": item.get("reason"),
                        }
                    )
        fact_ids = set(scope_fact_ids)
        for finding in (reconciliation or {}).get("findings", []):
            if not isinstance(finding, str):
                continue
            fact_id, separator, _reason = finding.partition(":")
            unresolved.append(
                {
                    "source": "reconciliation",
                    "fact_id": fact_id if separator and fact_id in fact_ids else None,
                    "state": "finding",
                    "reason": finding,
                }
            )
        unresolved = list(
            {
                (item["source"], item["fact_id"], item["state"], item["reason"]): item
                for item in unresolved
            }.values()
        )
        unresolved.sort(
            key=lambda item: (
                {"r8": 0, "r9": 1, "r10": 2, "reconciliation": 3}[str(item["source"])],
                str(item["fact_id"] or ""),
                str(item["state"] or ""),
                str(item["reason"] or ""),
            )
        )
        return {
            "v3": {
                "approval_only": True,
                "reason": "card_centric_v3 review is approval-only; Anki apply is unavailable",
                "phase_g_safety": evidence.get("phase_g_safety", {}),
                "policy": {
                    "sha256": snapshot.get("policy_sha256"),
                    "enforcement": {
                        "present": bool(enforcement),
                        "tier": enforcement.get("policy_enforcement", {}).get("tier")
                        if isinstance(enforcement.get("policy_enforcement"), dict)
                        else None,
                    },
                },
                "scope": {
                    "sha256": snapshot.get("scope_sha256"),
                    "source_bundle_sha256": scope.get("source_bundle_sha256"),
                    "degraded_mode": scope.get("degraded_mode"),
                    "fact_ids": scope_fact_ids,
                    "sources": [
                        {
                            key: item.get(key)
                            for key in (
                                "evidence_id",
                                "source_id",
                                "revision_id",
                                "source_kind",
                                "locator",
                            )
                        }
                        for item in scope.get("evidence", [])
                        if isinstance(item, dict)
                    ],
                },
                "retrieval": {
                    "effective_tag_mode": retrieval.get("effective_tag_mode"),
                    "exact_only_fact_ids": [
                        item.get("fact_id")
                        for item in calibration_records
                        if isinstance(item, dict) and item.get("exact_only") is True
                    ],
                    "polluted_fact_ids": [
                        item.get("fact_id")
                        for item in calibration_records
                        if isinstance(item, dict)
                        and any(
                            diagnostic.get("polluted") is True
                            for diagnostic in item.get("query_diagnostics", [])
                            if isinstance(diagnostic, dict)
                        )
                    ],
                },
                "evidence": {
                    "grounding": fidelity.get("fidelity", {}).get("grounding")
                    if isinstance(fidelity.get("fidelity"), dict)
                    else None,
                    "degraded_mode": scope.get("degraded_mode"),
                    "generated_grounding": generated_grounding,
                },
                "classification": {
                    "tiers": sorted(
                        {
                            item.get("tier")
                            for item in classification_calls
                            if isinstance(item, dict) and isinstance(item.get("tier"), str)
                        }
                    ),
                    "escalations": [
                        {key: item.get(key) for key in ("bundle_id", "reason", "reasons")}
                        for item in escalations
                        if isinstance(item, dict)
                    ],
                },
                "selected_existing_note_ids": snapshot.get("selected_existing_note_ids", []),
                "selected_generated_card_ids": snapshot.get("selected_generated_card_ids", []),
                "cost": {
                    "calls": [
                        {
                            key: item.get(key)
                            for key in (
                                "stage",
                                "call_id",
                                "modality",
                                "model",
                                "predicted",
                                "reserved",
                                "observed",
                                "observed_estimated",
                            )
                        }
                        for item in evidence.get("cost_ledger", [])
                        if isinstance(item, dict)
                    ]
                },
                "resolution": {
                    "duplicate_fact_ids": [
                        item.get("fact_id")
                        for item in dedupe_rows
                        if isinstance(item, dict)
                        and item.get("status")
                        in {"duplicate_of_existing", "duplicate_of_generated"}
                    ],
                    "unresolved": unresolved,
                },
            }
        }
    evidence_audit = _committed_stage_payload(request, job.id, CurationStage.CARD_EVIDENCE_AUDIT)
    coverage = _committed_stage_payload(request, job.id, CurationStage.CARD_COVERAGE)
    selection = _committed_stage_payload(request, job.id, CurationStage.CARD_SELECTION)
    dedupe = _committed_stage_payload(request, job.id, CurationStage.DEDUPE)
    current_metadata, metadata_state = _current_selection_metadata(selection, reconciliation)
    return {
        "evidence_quality": _review_evidence_quality(coverage, current_metadata),
        "s2b_diagnostic": _s2b_diagnostic(evidence_audit),
        "selection": _selection_review_surface(
            _repository(request),
            job,
            selection,
            reconciliation,
            current_metadata,
            metadata_state,
        ),
        "duplicate_resolutions": _duplicate_resolutions(dedupe),
    }


def _committed_stage_payload(
    request: Request,
    job_id: UUID,
    stage: CurationStage,
) -> dict[str, Any] | None:
    pipeline = getattr(request.app.state, "anki_curation_pipeline", None)
    artifacts = getattr(pipeline, "artifacts", None)
    if artifacts is None:
        return None
    artifact = next(
        (
            item
            for item in reversed(_repository(request).list_stage_artifacts(job_id))
            if item.stage is stage
        ),
        None,
    )
    if artifact is None:
        return None
    try:
        payload = artifacts.read(artifact)
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _s2b_diagnostic(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    diagnostic: dict[str, Any] = {}
    for key in (
        "evidence_poor_concept_ids",
        "matched_slide_passage_ids",
        "matched_slide_char_counts",
        "threshold_chars",
        "total_concepts",
    ):
        value = payload.get(key)
        if value is not None:
            diagnostic[key] = value
    return diagnostic or None


def _canonical_selection_identity(value: object) -> str | None:
    """Normalize persisted selection identities across v2 and legacy forms."""
    if isinstance(value, int) and value > 0:
        return f"existing:{value}"
    if not isinstance(value, str):
        return None
    if value.isdigit() and int(value) > 0:
        return f"existing:{value}"
    if value.startswith("note:") and value[5:].isdigit() and int(value[5:]) > 0:
        return f"existing:{value[5:]}"
    if value.startswith("existing:") and value[9:].isdigit() and int(value[9:]) > 0:
        return f"existing:{value[9:]}"
    if value.startswith("generated:") and value[10:].strip():
        return f"generated:{value[10:]}"
    if value.startswith("card:") and value[5:].strip():
        return f"generated:{value[5:]}"
    return None


def _review_evidence_quality(
    coverage: dict[str, Any] | None,
    selection_metadata: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    coverage_items = (coverage or {}).get("coverage", {})
    if isinstance(coverage_items, dict):
        for concept_id, item in sorted(coverage_items.items()):
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list):
                continue
            for record in evidence:
                if not isinstance(record, dict):
                    continue
                quality = record.get("evidence_quality")
                if quality not in _EVIDENCE_QUALITY_VALUES:
                    continue
                identity = _canonical_selection_identity(record.get("note_id"))
                if identity is None:
                    continue
                values.append(
                    {
                        "identity": identity,
                        "concept_id": concept_id,
                        "evidence_quality": quality,
                    }
                )
    for item in selection_metadata:
        quality = item.get("evidence_quality")
        identity = _canonical_selection_identity(item.get("identity"))
        if quality not in _EVIDENCE_QUALITY_VALUES or identity is None:
            continue
        values.append({"identity": identity, "evidence_quality": quality})
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in values:
        key = (str(item["identity"]), str(item["evidence_quality"]))
        if key not in unique or "concept_id" in item:
            unique[key] = item
    return [unique[key] for key in sorted(unique)]


def _selection_review_surface(
    repository: AnkiCurationRepository,
    job: CurationJob,
    selection: dict[str, Any] | None,
    reconciliation: dict[str, Any] | None,
    current_metadata: list[dict[str, Any]],
    metadata_state: str,
) -> dict[str, Any] | None:
    if selection is None:
        return None
    reconciliation_selection = (reconciliation or {}).get("selection")
    current_selection = (
        reconciliation_selection if isinstance(reconciliation_selection, dict) else selection
    )
    existing = current_selection.get("selected_existing_note_ids", [])
    generated = current_selection.get("selected_generated_card_ids", [])
    selected_count = (
        len(existing) + len(generated)
        if isinstance(existing, list) and isinstance(generated, list)
        else None
    )
    minimum = current_selection.get("minimum_target", selection.get("minimum_target"))
    target = current_selection.get("target", selection.get("target"))
    cap = current_selection.get("cap", selection.get("cap"))
    acknowledgement = current_selection.get("overflow_acknowledgement")
    acknowledgement_state = _overflow_acknowledgement_state(
        repository,
        job,
        existing,
        generated,
        cap,
        acknowledgement,
    )
    result: dict[str, Any] = {
        "warning_floor": minimum,
        "ordinary_target": target,
        "soft_cap": cap,
        "selected_existing_note_ids": existing if isinstance(existing, list) else [],
        "selected_generated_card_ids": generated if isinstance(generated, list) else [],
        "selected_count": selected_count,
        "below_warning_floor": (
            selected_count < minimum
            if isinstance(selected_count, int) and isinstance(minimum, int)
            else None
        ),
        "selection_metadata": current_metadata,
        # Metadata is selection-relative.  A reviewed selection that cannot
        # carry a complete current ordering must not inherit S9's former
        # positions or marginal/overflow reasons.
        "selection_metadata_state": metadata_state,
        "overflow_acknowledgement": acknowledgement_state,
        "acknowledgement_satisfied": (
            selected_count is not None
            and isinstance(cap, int)
            and (selected_count <= cap or acknowledgement_state["signed"])
        ),
    }
    return result


def _current_selection_metadata(
    selection: dict[str, Any] | None,
    reconciliation: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], str]:
    if selection is None:
        return [], "unavailable"
    reconciliation_selection = (reconciliation or {}).get("selection")
    has_reviewed_selection = isinstance(reconciliation_selection, dict)
    current_selection: dict[str, Any] = (
        cast(dict[str, Any], reconciliation_selection) if has_reviewed_selection else selection
    )
    # Deliberately do not fall back from a reviewed selection to the original
    # S9 metadata.  The reviewer may have changed membership or order.
    metadata = current_selection.get("selection_metadata", [])
    existing = current_selection.get("selected_existing_note_ids", [])
    generated = current_selection.get("selected_generated_card_ids", [])
    if (
        not isinstance(metadata, list)
        or not isinstance(existing, list)
        or not isinstance(generated, list)
    ):
        return [], "unavailable"
    selected_identities = {
        *(_canonical_selection_identity(note_id) for note_id in existing),
        *(_canonical_selection_identity(f"generated:{card_id}") for card_id in generated),
    }
    selected_identities.discard(None)

    normalized: list[dict[str, Any]] = []
    for item in metadata:
        if not isinstance(item, dict):
            continue
        identity = _canonical_selection_identity(item.get("identity"))
        if identity not in selected_identities:
            continue
        normalized.append({**item, "identity": identity})
    identities = [item["identity"] for item in normalized]
    positions = [item.get("selected_position") for item in normalized]
    complete = (
        len(normalized) == len(selected_identities)
        and set(identities) == selected_identities
        and len(set(identities)) == len(identities)
        and all(isinstance(position, int) and position > 0 for position in positions)
        and len(set(positions)) == len(positions)
        and set(positions) == set(range(1, len(selected_identities) + 1))
    )
    if complete:
        return sorted(normalized, key=lambda item: int(item["selected_position"])), "complete"
    return [], "incomplete" if has_reviewed_selection else "unavailable"


def _overflow_acknowledgement_state(
    repository: AnkiCurationRepository,
    job: CurationJob,
    existing: object,
    generated: object,
    cap: object,
    value: object,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(existing, list)
        or not isinstance(generated, list)
        or not isinstance(cap, int)
        or any(not isinstance(note_id, int) for note_id in existing)
        or any(not isinstance(card_id, str) for card_id in generated)
    ):
        return {"signed": False, "state": "pending", "provenance": None}
    try:
        signed = repository.validate_card_centric_overflow_acknowledgement(
            job.id,
            review_revision=job.review_revision,
            selected_note_ids=tuple(note_id for note_id in existing if isinstance(note_id, int)),
            selected_generated_ids=tuple(
                card_id for card_id in generated if isinstance(card_id, str)
            ),
            cap=cap,
            document=value,
        )
    except (TypeError, ValueError):
        signed = False
    if not signed:
        return {"signed": False, "state": "pending", "provenance": None}
    provenance = {
        key: value[key]
        for key in (
            "job_id",
            "review_revision",
            "selection_digest",
            "mandatory_count",
            "cap",
            "pipeline_contract_version",
            "model_config_sha256",
        )
        if key in value
    }
    return {"signed": True, "state": "signed", "provenance": provenance or None}


def _duplicate_resolutions(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    records = (payload or {}).get("resolutions", [])
    if not isinstance(records, list):
        return []
    duplicates = [
        {
            key: record[key]
            for key in (
                "status",
                "card_id",
                "concept_id",
                "fact_id",
                "reason",
                "duplicate_of_existing_note_id",
                "duplicate_of_generated_card_id",
            )
            if key in record
        }
        for record in records
        if isinstance(record, dict) and record.get("status") == "duplicate_of_existing"
    ]
    return sorted(duplicates, key=lambda item: str(item.get("card_id", item.get("fact_id", ""))))


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


def _convergence_summary(
    request: Request,
    job_id: UUID,
) -> dict[str, Any] | None:
    pipeline = getattr(request.app.state, "anki_curation_pipeline", None)
    artifacts = getattr(pipeline, "artifacts", None)
    if artifacts is None:
        return None
    convergence_stages = {
        CurationStage.CONVERGENCE_PASS_3,
        CurationStage.CONVERGENCE_PASS_4,
        CurationStage.CONVERGENCE_PASS_5,
    }
    artifact = next(
        (
            item
            for item in reversed(_repository(request).list_stage_artifacts(job_id))
            if item.stage in convergence_stages
        ),
        None,
    )
    if artifact is None:
        return None
    try:
        payload = artifacts.read(artifact)
        raw_states = payload.get("concepts")
        if not isinstance(raw_states, list) or not raw_states:
            return None
        states = tuple(ConvergenceState.model_validate(value) for value in raw_states)
    except (OSError, ValueError):
        return None
    manual_review = [state.concept_id for state in states if not state.converged]
    return {
        "passes_run": max(state.passes_run for state in states),
        "concepts_converged": sum(state.converged for state in states),
        "concepts_total": len(states),
        "needs_manual_review": bool(manual_review),
        "manual_review_concept_ids": manual_review,
    }


def _reconciliation_summary(
    request: Request,
    job_id: UUID,
) -> dict[str, Any] | None:
    repository = _repository(request)
    job = repository.require_job(job_id) if hasattr(repository, "require_job") else None
    if job is not None and job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3:
        reviewed = repository.reviewed_reconciliation(job_id, job.review_revision)
        if reviewed is not None or job.review_revision > 0:
            return reviewed
    elif job is not None and job.pipeline_contract_version in {
        PipelineContractVersion.CARD_CENTRIC_V1,
        PipelineContractVersion.CARD_CENTRIC_V2,
    }:
        reviewed = repository.reviewed_reconciliation(job_id, job.review_revision)
        if reviewed is not None:
            return reviewed
    pipeline = getattr(request.app.state, "anki_curation_pipeline", None)
    artifacts = getattr(pipeline, "artifacts", None)
    if artifacts is None:
        return None
    artifact = next(
        (
            item
            for item in reversed(_repository(request).list_stage_artifacts(job_id))
            if item.stage
            is (
                CurationStage.V3_R11_REVIEW
                if job is not None
                and job.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3
                else CurationStage.RECONCILIATION
            )
        ),
        None,
    )
    if artifact is None:
        return None
    try:
        payload = artifacts.read(artifact)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _review_reconciliation_summary(
    committed: dict[str, Any],
    cards: list[GapCard],
    candidates: list[Candidate] | None = None,
    *,
    overflow_acknowledgement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot_payload = committed.get("snapshot")
    if not isinstance(snapshot_payload, dict):
        return committed
    try:
        if "policy_sha256" in snapshot_payload and "scope_sha256" in snapshot_payload:
            # R11 is already reprojected atomically by the repository; unlike
            # legacy S9, do not reinterpret its fact-terminal closure here.
            reconciliation = committed.get("reconciliation")
            return (
                {**committed, **reconciliation} if isinstance(reconciliation, dict) else committed
            )
        if committed.get("contract_version") == "card_centric_s9_v1":
            snapshot = CardCentricReconciliationInput.model_validate(snapshot_payload)
            acknowledgement = overflow_acknowledgement or committed.get("selection", {}).get(
                "overflow_acknowledgement"
            )
            selected_existing = tuple(
                candidate.note_id for candidate in (candidates or []) if candidate.selected
            )
            selected_generated = tuple(card.card_id for card in cards if card.selected)
            selected_coverage = {
                concept_id
                for note_id in selected_existing
                for concept_id in snapshot.covered_concept_ids_by_nid.get(note_id, ())
            } | {
                snapshot.generated_concept_id_by_card_id[card_id]
                for card_id in selected_generated
                if card_id in snapshot.generated_concept_id_by_card_id
            }
            reviewed_coverage = {
                concept_id: (
                    "covered"
                    if concept_id in selected_coverage
                    else "intentional_gap"
                    if status == "intentional_gap"
                    else "uncovered"
                )
                for concept_id, status in snapshot.coverage.items()
            }
            selected_generated_resolutions = tuple(
                item for item in snapshot.generated_cards if item.card_id in set(selected_generated)
            )
            reviewed_snapshot = snapshot.model_copy(
                update={
                    "coverage": reviewed_coverage,
                    "selected_nids": selected_existing,
                    "selected_generated_card_ids": selected_generated,
                    "generated_cards": selected_generated_resolutions,
                    "overflow_acknowledgement": acknowledgement,
                }
            )
            report = reconcile_card_centric(reviewed_snapshot)
            return {
                **committed,
                **report.model_dump(mode="json"),
                "selection": {
                    **committed.get("selection", {}),
                    "selected_existing_note_ids": list(selected_existing),
                    "selected_generated_card_ids": list(selected_generated),
                    "overflow_acknowledgement": acknowledgement,
                },
                "snapshot": reviewed_snapshot.model_dump(mode="json"),
            }
        legacy_snapshot = ReconciliationInput.model_validate(snapshot_payload)
        selected_cards = tuple(
            GeneratedResolution(
                card_id=card.card_id,
                fact_id=str(card.provenance.get("fact_id", "")).strip(),
                text=card.text,
            )
            for card in cards
            if card.selected and card.card_id and str(card.provenance.get("fact_id", "")).strip()
        )
        legacy_reviewed_snapshot = legacy_snapshot.model_copy(
            update={"generated_cards": selected_cards}
        )
        report = reconcile(legacy_reviewed_snapshot)
    except (TypeError, ValueError):
        return {
            **committed,
            "failed": [
                {
                    "assertion_id": "A0",
                    "message": "The committed reconciliation snapshot is invalid",
                }
            ],
            "can_render_envelope": False,
        }
    return {
        **committed,
        **report.model_dump(mode="json"),
        "snapshot": legacy_reviewed_snapshot.model_dump(mode="json"),
    }


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
    note_type = GENERATED_NOTE_TYPE
    fields = {"Text": card.text.strip(), "Extra": card.extra.strip()}
    content_hash = hashlib.sha256(
        json.dumps(
            {"note_type": note_type, "fields": fields},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return GapCardProposal(
        concept_id=card.concept_id,
        note_type=note_type,
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
        "rejected_duplicates": list(result.rejected_duplicates),
        "differences": list(result.differences),
        "safe_error": result.safe_error,
        "recovery": _recovery_payload(result.state, result),
    }


def _recovery_payload(
    state: ApplyState,
    result: ApplyResult | None = None,
) -> dict[str, str]:
    if state is ApplyState.COMPLETE:
        if result is not None and result.rejected_duplicates:
            count = len(result.rejected_duplicates)
            return {
                "kind": "complete",
                "message": (
                    "Local changes, sync, and verification completed. "
                    f"Anki rejected {count} generated card"
                    f"{'s' if count != 1 else ''} as duplicate."
                ),
            }
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
            item.retrieval_pass in {RetrievalPass.PASS_2_RESCUE, RetrievalPass.CONVERGENCE}
            for item in candidates
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
