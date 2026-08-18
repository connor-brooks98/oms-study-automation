import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

import oms_hub.anki.repository as anki_repository_module
from oms_hub.anki.audit import AuditCacheRecord
from oms_hub.anki.card_centric import CardCentricLedgerAttempt, s2_generation_parameters
from oms_hub.anki.contracts import (
    ActionEnvelopeV2,
    SyncOperation,
    canonical_payload_sha256,
)
from oms_hub.anki.correction_contracts import (
    EvidenceQuality,
    MarginalValueReason,
    SelectionMetadata,
    SelectionTier,
)
from oms_hub.anki.cost_estimator import FrozenRateTable, ModelRate
from oms_hub.anki.course_policy import CourseCurationPolicy, PolicyEmphasisColor
from oms_hub.anki.domain import (
    ApplyState,
    Candidate,
    CreateCurationJob,
    CurationStage,
    CurationState,
    EnvelopeDraft,
    EnvelopeOperationDraft,
    EvidenceSupport,
    GapCard,
    GapCardEdit,
    PipelineContractVersion,
    ResolvedClassifierExecution,
    ResolvedModelConfiguration,
    ResolvedStageModel,
    RetrievalPass,
    ReviewChangeSet,
    SourceEvidence,
    SourceKind,
    SourceReference,
    StageArtifact,
    StageUsage,
    TagPatch,
)
from oms_hub.anki.envelope import EnvelopeBuilder
from oms_hub.anki.judgment import JudgmentCacheRecord
from oms_hub.anki.models import (
    AnkiCurationJobModel,
    AnkiEnvelopeModel,
    AnkiEnvelopeOperationModel,
    AnkiReviewedReconciliationModel,
)
from oms_hub.anki.pipeline import pipeline_stages
from oms_hub.anki.provider_attempts import (
    ProviderAttemptEvent,
    ProviderAttemptIdentity,
    ProviderAttemptIndeterminate,
    ProviderEventEvidence,
)
from oms_hub.anki.reconciliation import (
    AuditResolution,
    CardCentricReconciliationInput,
    GeneratedResolution,
)
from oms_hub.anki.rehearsal.capture import CaptureAnkiCurationRepository
from oms_hub.anki.rehearsal.process import ProcessRehearsal
from oms_hub.anki.repository import (
    AnkiCurationRepository,
    InvalidCurationTransition,
)
from oms_hub.anki.tag_policy import TagPolicy
from oms_hub.db import Database
from oms_hub.llm.domain import ProviderName
from oms_hub.migrations import migrate_database
from oms_hub.models import LectureModel


def _record_card_ledger_attempt(
    repository: AnkiCurationRepository,
    job_id: UUID,
    attempt: CardCentricLedgerAttempt,
) -> None:
    stage = repository.get_stage(job_id, CurationStage.CARD_LEDGER)
    assert stage is not None
    job = repository.require_job(job_id)
    repository.record_card_ledger_attempt(
        job_id,
        attempt,
        expected_stage_attempt=stage.attempt_count,
        lease_owner=job.lease_owner,
    )


def _provider_evidence(
    identity: ProviderAttemptIdentity,
    event: str,
) -> ProviderEventEvidence:
    response = "b" * 64 if event == "response_received" else None
    return ProviderEventEvidence(
        event=ProviderAttemptEvent(
            identity=identity,
            event=event,  # type: ignore[arg-type]
            request_sha256="a" * 64,
            request_id="request-1" if response else None,
            response_sha256=response,
        ),
        provider="anthropic",
        model="claude-sonnet-5",
        instruction_sha256="c" * 64,
        input_sha256="d" * 64,
        output_schema_sha256="e" * 64,
        generation_parameters={"temperature": None},
        generation_parameters_sha256="f" * 64,
        cache_prefix_sha256=None,
        request_id="request-1" if response else None,
        response_text="{}" if response else None,
    )


_OPEN_DATABASES: list[Database] = []


@pytest.fixture(autouse=True)
def _close_databases() -> None:
    yield
    while _OPEN_DATABASES:
        _OPEN_DATABASES.pop().close()


def _prepared_repository(tmp_path: Path) -> tuple[AnkiCurationRepository, int]:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    _OPEN_DATABASES.append(database)
    database.migrate()
    with database.session() as session:
        lecture = LectureModel(
            subject="Heme Lymph",
            exam_number=1,
            lecture_number=4,
            topic="Anemia I",
            lecturer="Professor",
        )
        session.add(lecture)
        session.flush()
        lecture_id = lecture.id
    return AnkiCurationRepository(database), lecture_id


def _job_request(
    lecture_id: int,
    *,
    snapshot: str = "snapshot-1",
    model: str = "claude-sonnet-5",
) -> CreateCurationJob:
    return CreateCurationJob(
        lecture_id=lecture_id,
        block_id="heme-block-1",
        source_revision_ids=(101, 102),
        deck_allowlist=("AnKing Step Deck",),
        tag_allowlist=("#AK_Step2_v12::Hematology",),
        instruction_text="Focus on red-highlighted material.",
        target_deck="OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I",
        target_tag=("AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"),
        index_snapshot_id=snapshot,
        lcl_prompt_version="lcl-v1",
        judgment_rubric_version="judgment-v1",
        gap_prompt_version="gap-v1",
        provider="anthropic",
        model=model,
        summary_outline_id=91,
        summary_outline_sha256="b" * 64,
    )


@pytest.mark.parametrize(
    ("version", "pin", "error"),
    (
        ("card_centric_v2", "valid", "job policy pin"),
        ("card_centric_v3", "dangling", "job policy pin"),
    ),
)
def test_current_v28_rejects_invalid_policy_pin_in_isolation(
    tmp_path: Path, version: str, pin: str, error: str
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    policy = CourseCurationPolicy(
        policy_id="policy",
        revision=1,
        course_id="course",
        professor_label="professor",
        scope_instruction="scope",
        emphasis_mode="colored_text",
        emphasis_colors=(PolicyEmphasisColor(rgb="FF0000", label="red"),),
        missing_emphasis_fallback="block",
        tag_scope_mode="hard_filter",
        classification_strictness="strict",
        generation_style_profile="style",
        ordinary_cost_limit_microusd=1,
        hard_stop_cost_limit_microusd=1,
    )
    repository.create_policy_revision(policy)
    with repository.database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE anki_curation_jobs SET pipeline_contract_version = :version, "
                "policy_sha256 = :sha WHERE id = :id"
            ),
            {
                "version": version,
                "sha": policy.policy_sha256 if pin == "valid" else "a" * 64,
                "id": str(job.id),
            },
        )
    with pytest.raises(RuntimeError, match=error):
        migrate_database(repository.database)


def test_v3_job_creation_requires_explicit_offline_replay_pin(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    request = replace(
        _job_request(lecture_id),
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        policy_sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="offline-replay-only"):
        repository.create_job(request)
    with repository.database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM anki_curation_jobs")).scalar_one() == 0


def test_capture_repository_is_the_only_live_v3_creation_boundary(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    policy = CourseCurationPolicy(
        policy_id="policy",
        revision=1,
        course_id="course",
        professor_label="professor",
        scope_instruction="scope",
        emphasis_mode="colored_text",
        emphasis_colors=(PolicyEmphasisColor(rgb="FF0000", label="red"),),
        missing_emphasis_fallback="block",
        tag_scope_mode="hard_filter",
        classification_strictness="strict",
        generation_style_profile="style",
        ordinary_cost_limit_microusd=500_000,
        hard_stop_cost_limit_microusd=10_000_000,
    )
    repository.create_policy_revision(policy)
    route = ResolvedStageModel("openrouter", "model", thinking_mode="disabled")
    config = ResolvedModelConfiguration(
        "v3",
        route,
        route,
        route,
        route,
        scope_r3=route,
        cheap_classify_r7=route,
        thorough_classify_r7=route,
        generation_r9=route,
    )
    table = FrozenRateTable(
        (ModelRate("model", 1, 1, 1, 1, 1),), datetime(2026, 8, 17, tzinfo=UTC), "fixture"
    )
    request = replace(
        _job_request(lecture_id),
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        policy_sha256=policy.policy_sha256,
        resolved_model_config=config,
        rate_table_document=table.document(),
        source_revision_hashes={101: "b" * 64, 102: "c" * 64},
        companion_generation="companion",
        semantic_generation="semantic",
    )
    with pytest.raises(ValueError, match="offline-replay-only"):
        repository.create_job(request)

    live = CaptureAnkiCurationRepository(repository.database).create_job(request)
    replay = repository.create_job(replace(request, offline_replay_only=True))

    assert live.offline_replay_only is False
    assert replay.offline_replay_only is True
    assert live.configuration_sha256 == replay.configuration_sha256


@pytest.mark.parametrize("change", ("rate_table", "offline_replay"))
def test_non_v3_job_rejects_v3_only_replay_pins(change: str) -> None:
    values: dict[str, object] = {}
    if change == "rate_table":
        values["rate_table_document"] = FrozenRateTable(
            (ModelRate("model", 1, 1, 1, 1, 1),), datetime(2026, 8, 17, tzinfo=UTC), "fixture"
        ).document()
    else:
        values["offline_replay_only"] = True

    with pytest.raises(ValueError, match="v3-only"):
        replace(_job_request(1), **values)


def test_v3_job_creation_rejects_unavailable_policy_before_any_job_write(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    route = ResolvedStageModel("openai", "model", thinking_mode="disabled")
    config = ResolvedModelConfiguration(
        "fixture",
        route,
        route,
        route,
        route,
        scope_r3=route,
        cheap_classify_r7=route,
        thorough_classify_r7=route,
        generation_r9=route,
    )
    table = FrozenRateTable(
        (ModelRate("model", 1, 1, 1, 1, 1),), datetime(2026, 8, 17, tzinfo=UTC), "fixture"
    )
    request = replace(
        _job_request(lecture_id),
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        policy_sha256="a" * 64,
        offline_replay_only=True,
        resolved_model_config=config,
        rate_table_document=table.document(),
        source_revision_hashes={101: "b" * 64, 102: "c" * 64},
        companion_generation="companion",
        semantic_generation="semantic",
    )

    with pytest.raises(KeyError, match="pinned course policy"):
        repository.create_job(request)
    with repository.database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM anki_curation_jobs")).scalar_one() == 0


def test_provider_attempt_events_are_fenced_ordered_and_exactly_idempotent(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    stage = repository.start_stage(job.id, CurationStage.PREFLIGHT)
    identity = ProviderAttemptIdentity(
        job_id=job.id,
        stage=CurationStage.PREFLIGHT,
        stage_attempt=stage.attempt_count,
        mode="canonical",
        call_index=1,
        batch_index=0,
        batch_note_ids=(11, 12),
        kind="primary",
    )
    for event in ("begun", "dispatched", "response_received", "accepted"):
        evidence = _provider_evidence(identity, event)
        repository.record_provider_attempt_event(evidence, lease_owner=None)
        repository.record_provider_attempt_event(evidence, lease_owner=None)
    rows = repository.list_provider_attempt_events(job.id)
    assert [row["event"] for row in rows] == [
        "begun",
        "dispatched",
        "response_received",
        "accepted",
    ]
    assert rows[0]["batch_note_ids"] == [11, 12]


def test_dispatched_provider_attempt_without_terminal_evidence_blocks_retry(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    stage = repository.start_stage(job.id, CurationStage.PREFLIGHT)
    identity = ProviderAttemptIdentity(
        job_id=job.id,
        stage=CurationStage.PREFLIGHT,
        stage_attempt=stage.attempt_count,
        mode="canonical",
        call_index=1,
        batch_index=None,
        batch_note_ids=(),
        kind="primary",
    )
    repository.record_provider_attempt_event(
        _provider_evidence(identity, "begun"), lease_owner=None
    )
    repository.record_provider_attempt_event(
        _provider_evidence(identity, "dispatched"), lease_owner=None
    )
    with pytest.raises(ProviderAttemptIndeterminate):
        repository.require_no_indeterminate_provider_attempt(job.id, CurationStage.PREFLIGHT)


def test_begun_only_provider_attempt_is_retryable(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    stage = repository.start_stage(job.id, CurationStage.PREFLIGHT)
    identity = ProviderAttemptIdentity(
        job_id=job.id,
        stage=CurationStage.PREFLIGHT,
        stage_attempt=stage.attempt_count,
        mode="canonical",
        call_index=1,
        batch_index=0,
        batch_note_ids=(),
        kind="primary",
    )

    repository.record_provider_attempt_event(
        _provider_evidence(identity, "begun"), lease_owner=None
    )

    repository.require_no_indeterminate_provider_attempt(job.id, CurationStage.PREFLIGHT)


def test_response_received_in_ordinary_ledger_requires_manual_recovery(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    stage = repository.start_stage(job.id, CurationStage.PREFLIGHT)
    identity = ProviderAttemptIdentity(
        job_id=job.id,
        stage=CurationStage.PREFLIGHT,
        stage_attempt=stage.attempt_count,
        mode="canonical",
        call_index=2,
        batch_index=1,
        batch_note_ids=(12,),
        kind="primary",
        subcall_ordinal=4,
    )
    for event in ("begun", "dispatched"):
        repository.record_provider_attempt_event(
            _provider_evidence(identity, event), lease_owner=None
        )
    response_text = '{"replay":true}'
    response = _provider_evidence(identity, "response_received")
    response = replace(
        response,
        event=replace(
            response.event,
            response_sha256=hashlib.sha256(response_text.encode()).hexdigest(),
        ),
        response_text=response_text,
    )
    repository.record_provider_attempt_event(response, lease_owner=None)
    with pytest.raises(ProviderAttemptIndeterminate, match="redacted and cannot authorize replay"):
        repository.require_no_indeterminate_provider_attempt(job.id, CurationStage.PREFLIGHT)
    assert repository.list_provider_attempt_events(job.id)[-1]["subcall_ordinal"] == 4


def test_card_centric_profile_persists_for_the_local_study_hub_user(tmp_path: Path) -> None:
    repository, _ = _prepared_repository(tmp_path)
    profile = ResolvedModelConfiguration(
        profile="custom",
        ledger_s2=ResolvedStageModel("anthropic", "sonnet"),
        classify_s4=ResolvedStageModel("anthropic", "haiku", "disabled", "fixture-v1"),
        residual_s6=ResolvedStageModel("anthropic", "haiku", "disabled", "fixture-v1"),
        gap_fill_s7=ResolvedStageModel("anthropic", "sonnet"),
        classifier_execution=ResolvedClassifierExecution(
            fast_concurrency=6,
            thorough_batch_size=31,
            thorough_concurrency=3,
            thinking_budget_tokens=2048,
        ),
    )

    repository.save_card_centric_profile(profile)

    assert repository.card_centric_profile() == profile


def test_repository_pins_new_v2_execution_defaults_but_preserves_legacy_documents(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    configured = replace(
        ResolvedModelConfiguration.card_centric_v2_default("anthropic", "claude-sonnet-5"),
        classifier_execution=None,
    )
    legacy_json = json.dumps(configured.canonical_document(), sort_keys=True, separators=(",", ":"))

    legacy = repository._resolved_model_config(legacy_json, "anthropic", "claude-sonnet-5")
    assert legacy.classifier_execution is None
    assert legacy.resolved_classifier_execution() == ResolvedClassifierExecution()
    assert (
        json.dumps(legacy.canonical_document(), sort_keys=True, separators=(",", ":"))
        == legacy_json
    )

    created = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=configured,
        )
    )
    assert created.resolved_model_config.classifier_execution == ResolvedClassifierExecution()
    assert "classifier_execution" in created.resolved_model_config.canonical_document()


def test_repository_rejects_an_unapproved_v2_fast_classifier_provider(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    approved = ResolvedModelConfiguration.card_centric_v2_default("anthropic", "claude-sonnet-5")
    redirected = replace(
        approved,
        fast_classify_s4b=ResolvedStageModel(
            "openrouter", "openai/gpt-4o-mini", thinking_mode="disabled"
        ),
    )

    with pytest.raises(ValueError, match="S4b requires an approved provider"):
        repository.create_job(
            replace(
                _job_request(lecture_id),
                pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                resolved_model_config=redirected,
            )
        )


def test_review_deselection_removes_the_sole_selected_covering_card(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    note_ids = tuple(range(1, 11))
    repository.replace_candidates(
        job.id,
        tuple(
            Candidate(
                note_id=note_id,
                content_hash=f"{note_id:064x}",
                best_concept_id="C01",
                provenance={"card_centric_v2": {"selection_eligible": True}},
                scores={},
                predicted_band="LIKELY_YES",
                verdict="keep",
                confidence=1.0,
                reason="fixture",
                context_trap=False,
                recall_direction="card_centric_v2",
                mnemonic_classification="none",
                dedupe_disposition="eligible",
                selected=True,
                retrieval_pass=RetrievalPass.PASS_1,
            )
            for note_id in note_ids
        ),
    )
    snapshot = CardCentricReconciliationInput(
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=note_ids,
        classifications=tuple(AuditResolution(nid=note_id, verdict="keep") for note_id in note_ids),
        eligible_yes_nids=note_ids,
        selected_nids=note_ids,
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        covered_concept_ids_by_nid={1: ("C01",)},
    )

    saved = repository.save_review(
        job.id,
        ReviewChangeSet(expected_revision=0, candidate_selections={1: False}),
        card_centric_snapshot=snapshot.model_dump(mode="json"),
    )
    reviewed = repository.reviewed_reconciliation(job.id, saved.revision)

    assert reviewed is not None
    assert reviewed["snapshot"]["coverage"] == {"C01": "uncovered"}
    assert "A4" in {item["assertion_id"] for item in reviewed["failed"]}
    assert reviewed["can_render_envelope"] is False


def test_card_centric_review_uses_current_generated_text(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    note_ids = tuple(range(1, 11))
    repository.replace_candidates(
        job.id,
        tuple(
            Candidate(
                note_id=note_id,
                content_hash=f"{note_id:064x}",
                best_concept_id="C01",
                provenance={"card_centric_v2": {"selection_eligible": True}},
                scores={},
                predicted_band="LIKELY_YES",
                verdict="keep",
                confidence=1.0,
                reason="fixture",
                context_trap=False,
                recall_direction="card_centric_v2",
                mnemonic_classification="none",
                dedupe_disposition="eligible",
                selected=True,
                retrieval_pass=RetrievalPass.PASS_1,
            )
            for note_id in note_ids
        ),
    )
    repository.save_gap_cards(
        job.id,
        (
            GapCard(
                card_id="G1",
                concept_id="C01",
                text="The safe answer is {{c1::ferritin}}.",
                extra="Original explanation.",
                selected=True,
            ),
        ),
    )
    canonical = GeneratedResolution(
        card_id="G1",
        fact_id="C01-M1",
        text="The safe answer is {{c1::ferritin}}.",
        extra="Original explanation.",
        split=True,
    )
    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=("C01-M1",),
        uncovered_after_s5=("C01",),
        residual_ran_for=("C01",),
        generated_cards=(canonical,),
        canonical_generated_cards=(canonical,),
        unresolved_fact_ids=(),
        expected_scoped_nids=note_ids,
        classifications=tuple(AuditResolution(nid=note_id, verdict="keep") for note_id in note_ids),
        eligible_yes_nids=note_ids,
        selected_nids=note_ids,
        selected_generated_card_ids=("G1",),
        generated_card_ids=("G1",),
        source_passage_ids=(),
        forbidden_cloze_targets=("iron deficiency",),
        prompt_sync_stale=False,
        untagged_rate=0,
        generated_concept_id_by_card_id={"G1": "C01"},
    )

    safe = repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=0,
            gap_edits=(
                GapCardEdit(
                    card_id="G1",
                    concept_id="C01",
                    text="The safe answer is {{c1::transferrin saturation}}.",
                    extra="Current safe explanation.",
                    selected=True,
                ),
            ),
        ),
        card_centric_snapshot=snapshot.model_dump(mode="json"),
    )
    safe_reviewed = repository.reviewed_reconciliation(job.id, safe.revision)

    assert safe_reviewed is not None
    assert safe_reviewed["can_render_envelope"] is True
    assert safe_reviewed["snapshot"]["generated_cards"] == [
        {
            "card_id": "G1",
            "fact_id": "C01-M1",
            "text": "The safe answer is {{c1::transferrin saturation}}.",
            "extra": "Current safe explanation.",
            "split": True,
        }
    ]
    assert safe_reviewed["snapshot"]["canonical_generated_cards"][0]["text"] == canonical.text

    forbidden = repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=safe.revision,
            gap_edits=(
                GapCardEdit(
                    card_id="G1",
                    concept_id="C01",
                    text="The diagnosis is {{c1::iron deficiency}}.",
                    extra="Forbidden current explanation.",
                    selected=True,
                ),
            ),
        ),
        card_centric_snapshot=snapshot.model_dump(mode="json"),
    )
    forbidden_reviewed = repository.reviewed_reconciliation(job.id, forbidden.revision)

    assert forbidden_reviewed is not None
    assert forbidden_reviewed["snapshot"]["generated_cards"][0]["text"] == (
        "The diagnosis is {{c1::iron deficiency}}."
    )
    assert forbidden_reviewed["snapshot"]["canonical_generated_cards"][0]["text"] == canonical.text
    assert "A5" in {item["assertion_id"] for item in forbidden_reviewed["failed"]}
    assert forbidden_reviewed["can_render_envelope"] is False


def test_card_centric_overflow_acknowledgement_is_exact_and_server_signed(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        )
    )
    selected = tuple(range(1, 72))
    acknowledgement = repository.issue_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=selected,
        selected_generated_ids=("G1",),
        mandatory_note_ids=selected,
        mandatory_generated_ids=(),
        cap=70,
    )

    assert repository.validate_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=selected,
        selected_generated_ids=("G1",),
        cap=70,
        document=acknowledgement,
    )
    assert not repository.validate_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=selected[:-1],
        selected_generated_ids=("G1",),
        cap=70,
        document=acknowledgement,
    )
    with pytest.raises(ValueError, match="exact mandatory overflow set"):
        repository.issue_card_centric_overflow_acknowledgement(
            job.id,
            review_revision=job.review_revision,
            selected_note_ids=selected[:-1],
            selected_generated_ids=("G1",),
            mandatory_note_ids=selected,
            mandatory_generated_ids=(),
            cap=70,
        )


def test_v2_overflow_acknowledgement_rejects_nonmandatory_generated_cards(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    selected = tuple(range(1, 72))

    with pytest.raises(ValueError, match="current reviewed reconciliation is unavailable"):
        repository.issue_card_centric_overflow_acknowledgement(
            job.id,
            review_revision=job.review_revision,
            selected_note_ids=selected,
            selected_generated_ids=("G1",),
            mandatory_note_ids=selected,
            mandatory_generated_ids=(),
            cap=70,
        )


def test_v2_mixed_overflow_acknowledgement_binds_full_selection_and_overflow_slice(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    selected = (2, 1, *range(3, 72))
    storage_order = tuple(sorted(selected))
    metadata = tuple(
        SelectionMetadata(
            identity=f"existing:{note_id}",
            selected_position=position,
            tier=SelectionTier.T1,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
            mandatory=position == 71,
            marginal_value_reason=(
                MarginalValueReason.ONLY_VALID_REQUIRED_FACT if 66 <= position <= 70 else None
            ),
            overflow_reason="required coverage" if position == 71 else None,
            manual_acknowledgement_required=position == 71,
        )
        for position, note_id in enumerate(selected, start=1)
    )
    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        terminal_resolutions=(),
        terminal_resolutions_provided=True,
        unresolved_fact_ids=(),
        expected_scoped_nids=selected,
        classifications=tuple(AuditResolution(nid=note_id, verdict="keep") for note_id in selected),
        eligible_yes_nids=selected,
        selected_nids=selected,
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=(71,),
        covered_concept_ids_by_nid={note_id: ("C01",) for note_id in selected},
        selection_metadata=metadata,
        selection_order=tuple(item.identity for item in metadata),
        selected_count=71,
        below_warning_floor=False,
    )
    with repository.database.session() as session:
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=str(job.id),
                review_revision=job.review_revision,
                payload_json=json.dumps({"snapshot": snapshot.model_dump(mode="json")}),
            )
        )
    document = repository.issue_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=storage_order,
        selected_generated_ids=(),
        mandatory_note_ids=(71,),
        mandatory_generated_ids=(),
        cap=70,
    )
    with pytest.raises(ValueError, match="frozen full selection"):
        repository.issue_card_centric_overflow_acknowledgement(
            job.id,
            review_revision=job.review_revision,
            selected_note_ids=(storage_order[0], storage_order[0], *storage_order[2:]),
            selected_generated_ids=(),
            mandatory_note_ids=(71,),
            mandatory_generated_ids=(),
            cap=70,
        )

    assert document["mandatory_count"] == 1
    assert repository.validate_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=storage_order,
        selected_generated_ids=(),
        cap=70,
        document=document,
    )
    assert not repository.validate_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=storage_order[:-1],
        selected_generated_ids=(),
        cap=70,
        document=document,
    )
    assert not repository.validate_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision + 1,
        selected_note_ids=storage_order,
        selected_generated_ids=(),
        cap=70,
        document=document,
    )
    assert not repository.validate_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=storage_order,
        selected_generated_ids=(),
        cap=70,
        document={**document, "signature": "forged"},
    )
    assert not repository.validate_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=(storage_order[0], storage_order[0], *storage_order[2:]),
        selected_generated_ids=(),
        cap=70,
        document=document,
    )
    assert not repository.validate_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=storage_order,
        selected_generated_ids=(),
        cap=69,
        document=document,
    )
    repository.persist_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        document=document,
    )
    assert (
        repository.reviewed_reconciliation(job.id, job.review_revision)["can_render_envelope"]
        is True
    )
    with repository.database.session() as session:
        reviewed = session.scalar(
            select(AnkiReviewedReconciliationModel).where(
                AnkiReviewedReconciliationModel.job_id == str(job.id),
                AnkiReviewedReconciliationModel.review_revision == job.review_revision,
            )
        )
        assert reviewed is not None
        payload = json.loads(reviewed.payload_json)
        order = payload["snapshot"]["selection_order"]
        payload["snapshot"]["selection_order"] = [order[1], order[0], *order[2:]]
        reviewed.payload_json = json.dumps(payload)
    assert not repository.validate_card_centric_overflow_acknowledgement(
        job.id,
        review_revision=job.review_revision,
        selected_note_ids=storage_order,
        selected_generated_ids=(),
        cap=70,
        document=document,
    )

    nonmandatory = snapshot.model_copy(
        update={
            "selection_metadata": (
                *metadata[:-1],
                metadata[-1].model_copy(update={"mandatory": False}),
            )
        }
    )
    bad_job = repository.create_job(
        replace(
            _job_request(lecture_id, snapshot="snapshot-2"),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    with repository.database.session() as session:
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=str(bad_job.id),
                review_revision=bad_job.review_revision,
                payload_json=json.dumps({"snapshot": nonmandatory.model_dump(mode="json")}),
            )
        )
    with pytest.raises(ValueError, match="cards above 70 require mandatory"):
        repository.issue_card_centric_overflow_acknowledgement(
            bad_job.id,
            review_revision=bad_job.review_revision,
            selected_note_ids=storage_order,
            selected_generated_ids=(),
            mandatory_note_ids=(71,),
            mandatory_generated_ids=(),
            cap=70,
        )


def test_review_preserves_only_s9_documented_t6_selection(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    note_ids = tuple(range(1, 11))
    repository.replace_candidates(
        job.id,
        tuple(
            Candidate(
                note_id=note_id,
                content_hash=f"{note_id:064x}",
                best_concept_id="C01",
                provenance={"card_centric_v2": {"selection_eligible": note_id not in {1, 2}}},
                scores={},
                predicted_band="LIKELY_YES",
                verdict="keep",
                confidence=1.0,
                reason="fixture",
                context_trap=False,
                recall_direction="card_centric_v2",
                mnemonic_classification="none",
                dedupe_disposition="eligible",
                selected=True,
                retrieval_pass=RetrievalPass.PASS_1,
            )
            for note_id in note_ids
        ),
    )
    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=note_ids,
        classifications=tuple(AuditResolution(nid=note_id, verdict="keep") for note_id in note_ids),
        eligible_yes_nids=tuple(note_id for note_id in note_ids if note_id not in {1, 2}),
        selected_nids=note_ids,
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        covered_concept_ids_by_nid={1: ("C01",)},
        t6_selected_nids=(1,),
    )

    saved = repository.save_review(
        job.id,
        ReviewChangeSet(expected_revision=0, candidate_selections={1: True}),
        card_centric_snapshot=snapshot.model_dump(mode="json"),
    )

    assert repository.list_candidates(job.id)[0].selected is True
    with pytest.raises(ValueError, match="undocumented ineligible"):
        repository.save_review(
            job.id,
            ReviewChangeSet(expected_revision=saved.revision, candidate_selections={2: True}),
            card_centric_snapshot=snapshot.model_dump(mode="json"),
        )


def _v2_envelope(
    *,
    job_id: UUID,
    pipeline_contract_version: str = "card_centric_v1",
    model_config_sha256: str = "a" * 64,
    review_revision: int = 0,
) -> ActionEnvelopeV2:
    v1 = EnvelopeBuilder(
        TagPolicy(
            pipeline_owned_roots=("OMS",),
            approved_optional_roots=("AnkiHub_Optional::LMU_OMS_II",),
            source_managed_roots=("#Pathoma",),
            version="tags-v1",
        )
    ).build(
        ReviewChangeSet(expected_revision=0),
        {},
        envelope_id=UUID("5dc4f15e-df92-4a32-964e-026b5d518a80"),
        snapshot_id="snapshot-1",
        target_deck="OMS::Heme::Lecture 3",
        target_tag="AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_3",
    )
    payload = v1.model_dump(mode="json")
    payload.update(
        {
            "contract_version": 2,
            "job_id": str(job_id),
            "pipeline_contract_version": pipeline_contract_version,
            "model_config_sha256": model_config_sha256,
            "reconciliation_contract_version": "reconciliation-v1",
            "review_revision": review_revision,
            "overflow_acknowledgement_provenance": {"reviewer": "local"},
        }
    )
    v2 = ActionEnvelopeV2.model_validate(payload)
    return v2.model_copy(update={"payload_sha256": canonical_payload_sha256(v2)})


def _envelope_row_counts(repository: AnkiCurationRepository) -> tuple[int, int]:
    with repository.database.session() as session:
        return (
            session.scalar(select(func.count()).select_from(AnkiEnvelopeModel)) or 0,
            session.scalar(select(func.count()).select_from(AnkiEnvelopeOperationModel)) or 0,
        )


def test_create_job_snapshots_all_mutable_inputs(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)

    job = repository.create_job(_job_request(lecture_id))

    assert job.lecture_id == lecture_id
    assert job.state is CurationState.QUEUED
    assert job.instruction_text == "Focus on red-highlighted material."
    assert len(job.instruction_sha256) == 64
    assert job.block_id == "heme-block-1"
    assert job.source_revision_ids == (101, 102)
    assert job.summary_outline_id == 91
    assert job.summary_outline_sha256 == "b" * 64
    assert job.deck_allowlist == ("AnKing Step Deck",)
    assert job.tag_allowlist == ("#AK_Step2_v12::Hematology",)
    assert job.apply_state is ApplyState.PENDING
    assert job.target_deck.endswith("::Lec4_Anemia_I")
    assert job.target_tag.endswith("::Lec4_Anemia_I")
    assert job.index_snapshot_id == "snapshot-1"
    assert job.lcl_prompt_version == "lcl-v1"
    assert job.judgment_rubric_version == "judgment-v1"
    assert job.gap_prompt_version == "gap-v1"


def test_capture_ready_state_proof_rejects_persisted_review_and_envelope_artifacts(
    tmp_path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    with repository.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(job.id))
        assert stored is not None
        stored.state = CurationState.READY_FOR_REVIEW.value
    ProcessRehearsal._validate_capture_ready_for_review_state(object(), repository, job.id)
    with repository.database.session() as session:
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=str(job.id), review_revision=0, payload_json="{}"
            )
        )
    with pytest.raises(RuntimeError, match="review or envelope artifact"):
        ProcessRehearsal._validate_capture_ready_for_review_state(object(), repository, job.id)
    with repository.database.session() as session:
        session.query(AnkiReviewedReconciliationModel).delete()
        session.add(
            AnkiEnvelopeModel(
                id=str(UUID(int=81)),
                job_id=str(job.id),
                payload_json="{}",
                payload_sha256="a" * 64,
                snapshot_id="snapshot-1",
                state="pending",
            )
        )
    with pytest.raises(RuntimeError, match="review or envelope artifact"):
        ProcessRehearsal._validate_capture_ready_for_review_state(object(), repository, job.id)


def test_claim_next_job_claims_oldest_queued_job_once(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    first = repository.create_job(_job_request(lecture_id, snapshot="snapshot-1"))
    second = repository.create_job(_job_request(lecture_id, snapshot="snapshot-2"))
    now = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)

    claimed_first = repository.claim_next_job(now)
    claimed_second = repository.claim_next_job(now)

    assert claimed_first is not None
    assert claimed_first.id == first.id
    assert claimed_first.state is CurationState.PREFLIGHT
    assert claimed_first.attempts == 1
    assert claimed_second is not None
    assert claimed_second.id == second.id
    assert repository.claim_next_job(now) is None


def test_transition_requires_expected_state_and_allowed_edge(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))

    with pytest.raises(InvalidCurationTransition):
        repository.transition(
            job.id,
            CurationState.QUEUED,
            CurationState.JUDGING_PASS_1,
        )

    claimed = repository.claim_next_job(datetime.now(UTC))
    assert claimed is not None
    retrieved = repository.transition(
        claimed.id,
        CurationState.PREFLIGHT,
        CurationState.BUILDING_LCL,
    )
    assert retrieved.state is CurationState.BUILDING_LCL

    with pytest.raises(InvalidCurationTransition):
        repository.transition(
            claimed.id,
            CurationState.PREFLIGHT,
            CurationState.BUILDING_LCL,
        )


def test_recovery_releases_interrupted_pre_review_jobs_in_place(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    interrupted = repository.create_job(_job_request(lecture_id, snapshot="snapshot-1"))
    envelope_pending = repository.create_job(_job_request(lecture_id, snapshot="snapshot-2"))
    claimed = repository.claim_next_job(datetime.now(UTC))
    assert claimed is not None
    repository.transition(
        envelope_pending.id,
        CurationState.QUEUED,
        CurationState.PREFLIGHT,
    )
    for current, target in [
        (CurationState.PREFLIGHT, CurationState.BUILDING_LCL),
        (CurationState.BUILDING_LCL, CurationState.RETRIEVING_PASS_1),
        (CurationState.RETRIEVING_PASS_1, CurationState.JUDGING_PASS_1),
        (
            CurationState.JUDGING_PASS_1,
            CurationState.LOCALIZING_MISSED_CONCEPTS,
        ),
        (
            CurationState.LOCALIZING_MISSED_CONCEPTS,
            CurationState.RETRIEVING_PASS_2,
        ),
        (CurationState.RETRIEVING_PASS_2, CurationState.JUDGING_PASS_2),
        (CurationState.JUDGING_PASS_2, CurationState.DEDUPING),
        (CurationState.DEDUPING, CurationState.GENERATING_GAPS),
        (CurationState.GENERATING_GAPS, CurationState.RECONCILING),
        (CurationState.RECONCILING, CurationState.READY_FOR_REVIEW),
        (CurationState.READY_FOR_REVIEW, CurationState.ENVELOPE_PENDING),
    ]:
        repository.transition(envelope_pending.id, current, target)

    assert repository.recover_interrupted_jobs() == 1
    recovered = repository.require_job(interrupted.id)
    assert recovered.state is CurationState.PREFLIGHT
    assert recovered.lease_owner is None
    assert recovered.lease_expires_at is None
    assert repository.require_job(envelope_pending.id).state is CurationState.ENVELOPE_PENDING


@pytest.mark.parametrize(
    "interrupted_state",
    (
        CurationState.CARD_AUDITING_EVIDENCE,
        CurationState.CARD_PREFILTERING,
        CurationState.CARD_FAST_CLASSIFYING,
    ),
)
def test_v2_new_lifecycle_states_are_claimable_and_recoverable(
    tmp_path: Path,
    interrupted_state: CurationState,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    current = CurationState.QUEUED
    for definition in pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V2):
        if current is not definition.state:
            repository.transition(job.id, current, definition.state)
            current = definition.state
        if current is interrupted_state:
            break

    claimed = repository.claim_next_job(datetime.now(UTC), worker_id="interrupted-worker")
    assert claimed is not None
    assert claimed.state is interrupted_state
    assert repository.recover_interrupted_jobs() == 1
    recovered = repository.require_job(job.id)
    assert recovered.state is interrupted_state
    assert recovered.lease_owner is None
    assert recovered.lease_expires_at is None


def test_stage_lifecycle_records_usage_and_safe_failure(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))

    running = repository.start_stage(
        job.id,
        CurationStage.LCL,
        provider="gemini",
        model="gemini-model",
    )
    completed = repository.finish_stage(
        job.id,
        CurationStage.LCL,
        StageUsage(
            request_id="request-1",
            input_tokens=100,
            output_tokens=20,
            cost_microusd=42,
        ),
        cache_hits=3,
    )
    failed = repository.start_stage(job.id, CurationStage.RETRIEVAL_PASS_1)
    failed = repository.fail_stage(
        job.id,
        CurationStage.RETRIEVAL_PASS_1,
        "index is unavailable",
        expected_state=CurationState.QUEUED,
        lease_owner=None,
    )

    assert running.attempt_count == 1
    assert completed.state == "complete"
    assert completed.request_id == "request-1"
    assert completed.input_tokens == 100
    assert completed.cache_hits == 3
    assert failed.state == "failed"
    assert failed.error == "index is unavailable"


def test_card_ledger_attempts_are_append_only_across_internal_and_manual_retries(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    parameter_hash = hashlib.sha256(parameters_json.encode()).hexdigest()

    def attempt(index: int, outcome: str) -> CardCentricLedgerAttempt:
        return CardCentricLedgerAttempt(
            call_index=index,  # type: ignore[arg-type]
            kind="primary" if index == 1 else "repair",
            outcome=outcome,  # type: ignore[arg-type]
            provider=ProviderName.ANTHROPIC,
            model="claude-sonnet-5",
            instruction_sha256="a" * 64,
            generation_parameters=parameters,
            generation_parameters_sha256=parameter_hash,
            request_id=f"request-{index}",
            input_tokens=10,
            output_tokens=5,
            cost_microusd=1,
            validation_error="importance conflicts" if outcome == "validation_failed" else None,
            invalid_response_sha256=(
                hashlib.sha256(b'{"importance":"low"}').hexdigest()
                if outcome == "validation_failed"
                else None
            ),
            invalid_response='{"importance":"low"}' if outcome == "validation_failed" else None,
        )

    repository.start_stage(job.id, CurationStage.CARD_LEDGER)
    _record_card_ledger_attempt(repository, job.id, attempt(1, "validation_failed"))
    _record_card_ledger_attempt(repository, job.id, attempt(2, "accepted"))
    _record_card_ledger_attempt(repository, job.id, attempt(1, "validation_failed"))
    repository.finish_stage(job.id, CurationStage.CARD_LEDGER)
    repository.start_stage(job.id, CurationStage.CARD_LEDGER)
    _record_card_ledger_attempt(repository, job.id, attempt(1, "accepted"))

    rows = repository.list_card_ledger_attempts(job.id)
    assert [(row["stage_attempt"], row["call_index"], row["outcome"]) for row in rows] == [
        (1, 1, "validation_failed"),
        (1, 2, "accepted"),
        (2, 1, "accepted"),
    ]
    assert rows[0]["invalid_response_sha256"] == hashlib.sha256(b'{"importance":"low"}').hexdigest()
    assert rows[0]["invalid_response"] == '{"importance":"low"}'
    assert rows[0]["generation_parameters_sha256"] == parameter_hash

    conflicting = replace(
        attempt(1, "validation_failed"),
        invalid_response='{"importance":"medium"}',
        invalid_response_sha256=hashlib.sha256(b'{"importance":"medium"}').hexdigest(),
    )
    with pytest.raises(ValueError, match="identity was reused"):
        _record_card_ledger_attempt(repository, job.id, conflicting)
    assert repository.list_card_ledger_attempts(job.id)[0]["invalid_response"] == (
        '{"importance":"low"}'
    )


def test_card_ledger_attempt_fence_rejects_expiry_and_reclaimed_worker_without_mutation(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    started = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    assert repository.claim_next_job(started, worker_id="worker-a", lease_seconds=3) is not None
    first = repository.start_stage(
        job.id,
        CurationStage.CARD_LEDGER,
        lease_owner="worker-a",
        now=started,
    )
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    accepted = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="accepted",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="request-a",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error=None,
        invalid_response_sha256=None,
        invalid_response=None,
    )
    expired = started + timedelta(seconds=4)

    with pytest.raises(InvalidCurationTransition, match="lease expired"):
        repository.record_card_ledger_attempt(
            job.id,
            accepted,
            expected_stage_attempt=first.attempt_count,
            lease_owner="worker-a",
            now=expired,
        )
    assert repository.list_card_ledger_attempts(job.id) == []

    assert repository.claim_next_job(expired, worker_id="worker-b", lease_seconds=30) is not None
    second = repository.start_stage(
        job.id,
        CurationStage.CARD_LEDGER,
        lease_owner="worker-b",
        now=expired,
    )
    with pytest.raises(InvalidCurationTransition, match="no longer owns"):
        repository.record_card_ledger_attempt(
            job.id,
            accepted,
            expected_stage_attempt=first.attempt_count,
            lease_owner="worker-a",
            now=expired,
        )
    assert repository.list_card_ledger_attempts(job.id) == []

    repository.record_card_ledger_attempt(
        job.id,
        accepted,
        expected_stage_attempt=second.attempt_count,
        lease_owner="worker-b",
        now=expired,
    )
    # Same-attempt replay is safe only when every persisted field is identical.
    repository.record_card_ledger_attempt(
        job.id,
        accepted,
        expected_stage_attempt=second.attempt_count,
        lease_owner="worker-b",
        now=expired,
    )
    assert [row["stage_attempt"] for row in repository.list_card_ledger_attempts(job.id)] == [2]


def test_card_ledger_sqlite_fence_rechecks_after_stale_precheck_before_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale connection cannot append after a successor reclaims S2."""
    repository, lecture_id = _prepared_repository(tmp_path)
    stale_repository = AnkiCurationRepository(repository.database)
    successor_repository = AnkiCurationRepository(repository.database)
    job = repository.create_job(_job_request(lecture_id))
    started = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    assert (
        stale_repository.claim_next_job(started, worker_id="worker-a", lease_seconds=3) is not None
    )
    first = stale_repository.start_stage(
        job.id,
        CurationStage.CARD_LEDGER,
        lease_owner="worker-a",
        now=started,
    )
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    accepted = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="accepted",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="stale-request",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error=None,
        invalid_response_sha256=None,
        invalid_response=None,
    )
    precheck_entered = threading.Event()
    allow_append = threading.Event()
    original_validate = anki_repository_module._validate_card_ledger_attempt_for_write

    def pause_after_precheck(*args, **kwargs) -> None:
        original_validate(*args, **kwargs)
        precheck_entered.set()
        assert allow_append.wait(timeout=2)

    monkeypatch.setattr(
        anki_repository_module,
        "_validate_card_ledger_attempt_for_write",
        pause_after_precheck,
    )
    stale_errors: list[Exception] = []

    def stale_append() -> None:
        try:
            stale_repository.record_card_ledger_attempt(
                job.id,
                accepted,
                expected_stage_attempt=first.attempt_count,
                lease_owner="worker-a",
                now=started,
            )
        except Exception as error:  # exercised from the stale DB connection
            stale_errors.append(error)

    stale_thread = threading.Thread(target=stale_append)
    stale_thread.start()
    assert precheck_entered.wait(timeout=2)

    reclaimed_at = started + timedelta(seconds=4)
    assert (
        successor_repository.claim_next_job(reclaimed_at, worker_id="worker-b", lease_seconds=30)
        is not None
    )
    second = successor_repository.start_stage(
        job.id,
        CurationStage.CARD_LEDGER,
        lease_owner="worker-b",
        now=reclaimed_at,
    )
    assert second.attempt_count == 2

    allow_append.set()
    stale_thread.join(timeout=2)
    assert not stale_thread.is_alive()
    assert len(stale_errors) == 1
    assert isinstance(stale_errors[0], InvalidCurationTransition)
    assert repository.list_card_ledger_attempts(job.id) == []


def test_card_ledger_sqlite_fence_serializes_contention_and_replays_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    first_repository = AnkiCurationRepository(repository.database)
    second_repository = AnkiCurationRepository(repository.database)
    job = repository.create_job(_job_request(lecture_id))
    stage = repository.start_stage(job.id, CurationStage.CARD_LEDGER)
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    accepted = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="accepted",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="request-1",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error=None,
        invalid_response_sha256=None,
        invalid_response=None,
    )
    first_holds_fence = threading.Event()
    release_first = threading.Event()
    original_lease_check = AnkiCurationRepository._require_active_stage_lease

    def hold_first_fence(*args, **kwargs) -> None:
        original_lease_check(*args, **kwargs)
        if not first_holds_fence.is_set():
            first_holds_fence.set()
            assert release_first.wait(timeout=2)

    monkeypatch.setattr(
        AnkiCurationRepository,
        "_require_active_stage_lease",
        staticmethod(hold_first_fence),
    )
    errors: list[Exception] = []

    def append_from(repository_for_thread: AnkiCurationRepository) -> None:
        try:
            repository_for_thread.record_card_ledger_attempt(
                job.id,
                accepted,
                expected_stage_attempt=stage.attempt_count,
                lease_owner=None,
            )
        except Exception as error:  # exercised from independent DB connections
            errors.append(error)

    first_thread = threading.Thread(target=append_from, args=(first_repository,))
    second_thread = threading.Thread(target=append_from, args=(second_repository,))
    first_thread.start()
    assert first_holds_fence.wait(timeout=2)
    second_thread.start()
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert repository.list_card_ledger_attempts(job.id) == [
        {
            "stage": "card_ledger",
            "stage_attempt": 1,
            "call_index": 1,
            "kind": "primary",
            "outcome": "accepted",
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "instruction_sha256": "a" * 64,
            "generation_parameters": parameters,
            "generation_parameters_sha256": hashlib.sha256(parameters_json.encode()).hexdigest(),
            "request_id": "request-1",
            "input_tokens": 1,
            "output_tokens": 2,
            "cost_microusd": 3,
            "validation_error": None,
            "invalid_response_sha256": None,
            "invalid_response": None,
            "diagnostic_source": None,
            "http_status": None,
        }
    ]


def test_card_ledger_attempt_rejects_partial_extra_and_mismatched_parameter_documents(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    base = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="accepted",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="request-1",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error=None,
        invalid_response_sha256=None,
        invalid_response=None,
    )
    repository.start_stage(job.id, CurationStage.CARD_LEDGER)
    bad_documents = (
        {key: value for key, value in parameters.items() if key != "cache"},
        {**parameters, "unexpected": True},
        {**parameters, "temperature": {"value": 0, "transmission": "transmitted"}},
        {**parameters, "provider": "openai"},
    )
    for document in bad_documents:
        document_json = json.dumps(document, sort_keys=True, separators=(",", ":"))
        with pytest.raises(ValueError, match="generation parameters are invalid"):
            _record_card_ledger_attempt(
                repository,
                job.id,
                replace(
                    base,
                    generation_parameters=document,
                    generation_parameters_sha256=hashlib.sha256(document_json.encode()).hexdigest(),
                ),
            )
    assert repository.list_card_ledger_attempts(job.id) == []


def test_v24_card_ledger_evidence_survives_reopen_upgrade_without_rewriting(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    rows: list[tuple[str, str, str, str]] = []
    fixtures = (
        ("claude-opus-4-7suffix", "accepted"),
        ("claude-opus-4-8suffix", "validation_failed"),
        ("claude-sonnet-5suffix", "transport_failed"),
    )
    for index, (model, outcome) in enumerate(fixtures, start=1):
        job = repository.create_job(
            _job_request(lecture_id, snapshot=f"snapshot-{index}", model=model)
        )
        stage = repository.start_stage(job.id, CurationStage.CARD_LEDGER)
        current = s2_generation_parameters(ProviderName.ANTHROPIC, model)
        legacy = json.loads(json.dumps(current))
        legacy["temperature"] = {"value": 0, "transmission": "transmitted"}
        legacy_json = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
        legacy_sha256 = hashlib.sha256(legacy_json.encode()).hexdigest()
        invalid_response = '{"importance":"low"}' if outcome == "validation_failed" else None
        repository.record_card_ledger_attempt(
            job.id,
            CardCentricLedgerAttempt(
                call_index=1,
                kind="primary",
                outcome=outcome,  # type: ignore[arg-type]
                provider=ProviderName.ANTHROPIC,
                model=model,
                instruction_sha256="a" * 64,
                generation_parameters=current,
                generation_parameters_sha256=hashlib.sha256(
                    json.dumps(current, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                request_id=f"request-{index}",
                input_tokens=1 if outcome != "transport_failed" else 0,
                output_tokens=2 if outcome != "transport_failed" else 0,
                cost_microusd=3 if outcome != "transport_failed" else 0,
                validation_error=(
                    "importance conflicts"
                    if outcome == "validation_failed"
                    else "Anthropic rejected the request"
                    if outcome == "transport_failed"
                    else None
                ),
                invalid_response_sha256=(
                    hashlib.sha256(invalid_response.encode()).hexdigest()
                    if invalid_response is not None
                    else None
                ),
                invalid_response=invalid_response,
            ),
            expected_stage_attempt=stage.attempt_count,
            lease_owner=None,
        )
        rows.append((str(job.id), model, legacy_json, legacy_sha256))
    database_path = Path(str(repository.database.engine.url.database))
    with repository.database.engine.begin() as connection:
        for job_id, _, legacy_json, legacy_sha256 in rows:
            connection.execute(
                text(
                    "UPDATE anki_card_ledger_attempts SET generation_parameters_json = :document, "
                    "generation_parameters_sha256 = :sha256 WHERE job_id = :job_id"
                ),
                {
                    "document": legacy_json,
                    "sha256": legacy_sha256,
                    "job_id": job_id,
                },
            )
        connection.execute(text("UPDATE schema_version SET version = 24 WHERE id = 1"))
        connection.execute(
            text("ALTER TABLE anki_card_ledger_attempts DROP COLUMN diagnostic_source")
        )
        connection.execute(text("ALTER TABLE anki_card_ledger_attempts DROP COLUMN http_status"))
    repository.database.close()

    with Database(f"sqlite:///{database_path}") as reopened:
        reopened.migrate()
        with reopened.engine.connect() as connection:
            persisted_rows = connection.execute(
                text(
                    "SELECT model, outcome, generation_parameters_json, "
                    "generation_parameters_sha256, diagnostic_source, http_status "
                    "FROM anki_card_ledger_attempts ORDER BY model"
                )
            ).all()
            version = connection.execute(
                text("SELECT version FROM schema_version WHERE id = 1")
            ).scalar_one()

    assert persisted_rows == [
        (model, outcome, legacy_json, legacy_sha256, None, None)
        for _, model, legacy_json, legacy_sha256 in sorted(rows, key=lambda row: row[1])
        for candidate_model, outcome in fixtures
        if candidate_model == model
    ]
    assert version == 29


def test_card_ledger_transport_failure_persists_only_safe_diagnostics(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    stage = repository.start_stage(job.id, CurationStage.CARD_LEDGER)
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    repository.record_card_ledger_attempt(
        job.id,
        CardCentricLedgerAttempt(
            call_index=1,
            kind="primary",
            outcome="transport_failed",
            provider=ProviderName.ANTHROPIC,
            model="claude-sonnet-5",
            instruction_sha256="a" * 64,
            generation_parameters=parameters,
            generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
            request_id="safe-provider-request-42",
            input_tokens=0,
            output_tokens=0,
            cost_microusd=0,
            validation_error="Anthropic rejected the request",
            invalid_response_sha256=None,
            invalid_response=None,
            diagnostic_source="provider_request",
            http_status=400,
        ),
        expected_stage_attempt=stage.attempt_count,
        lease_owner=None,
    )

    assert repository.list_card_ledger_attempts(job.id) == [
        {
            "stage": "card_ledger",
            "stage_attempt": 1,
            "call_index": 1,
            "kind": "primary",
            "outcome": "transport_failed",
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "instruction_sha256": "a" * 64,
            "generation_parameters": parameters,
            "generation_parameters_sha256": hashlib.sha256(parameters_json.encode()).hexdigest(),
            "request_id": "safe-provider-request-42",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_microusd": 0,
            "validation_error": "Anthropic rejected the request",
            "invalid_response_sha256": None,
            "invalid_response": None,
            "diagnostic_source": "provider_request",
            "http_status": 400,
        }
    ]


def test_v24_startup_rejects_coherent_cross_row_transport_tamper(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    invalid = '{"importance":"low"}'
    primary = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="validation_failed",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="request-1",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error="importance conflicts",
        invalid_response_sha256=hashlib.sha256(invalid.encode()).hexdigest(),
        invalid_response=invalid,
    )
    repository.start_stage(
        job.id,
        CurationStage.CARD_LEDGER,
        provider="anthropic",
        model="claude-sonnet-5",
    )
    _record_card_ledger_attempt(repository, job.id, primary)
    _record_card_ledger_attempt(
        repository,
        job.id,
        replace(
            primary,
            call_index=2,
            kind="repair",
            outcome="accepted",
            request_id="request-2",
            validation_error=None,
            invalid_response_sha256=None,
            invalid_response=None,
        ),
    )
    tampered = s2_generation_parameters(ProviderName.OPENAI, "gpt-5.2")
    tampered_json = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    with repository.database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE anki_card_ledger_attempts SET "
                "provider = 'openai', model = 'gpt-5.2', "
                "generation_parameters_json = :parameters, "
                "generation_parameters_sha256 = :sha256"
            ),
            {
                "parameters": tampered_json,
                "sha256": hashlib.sha256(tampered_json.encode()).hexdigest(),
            },
        )

    with pytest.raises(RuntimeError, match="stage transport"):
        repository.database.migrate()


def test_card_ledger_repair_requires_validation_failed_primary_before_mutation(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    invalid = '{"importance":"low"}'
    failed_primary = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="validation_failed",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="request-1",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error="importance conflicts",
        invalid_response_sha256=hashlib.sha256(invalid.encode()).hexdigest(),
        invalid_response=invalid,
    )
    repair = replace(
        failed_primary,
        call_index=2,
        kind="repair",
        outcome="accepted",
        request_id="request-2",
        validation_error=None,
        invalid_response_sha256=None,
        invalid_response=None,
    )
    accepted_primary = replace(
        failed_primary,
        outcome="accepted",
        validation_error=None,
        invalid_response_sha256=None,
        invalid_response=None,
    )
    repository.start_stage(job.id, CurationStage.CARD_LEDGER)

    with pytest.raises(ValueError, match="persisted primary"):
        _record_card_ledger_attempt(repository, job.id, repair)
    assert repository.list_card_ledger_attempts(job.id) == []

    _record_card_ledger_attempt(repository, job.id, accepted_primary)
    with pytest.raises(ValueError, match="validation-failed primary"):
        _record_card_ledger_attempt(repository, job.id, repair)
    assert [row["call_index"] for row in repository.list_card_ledger_attempts(job.id)] == [1]

    repository.start_stage(job.id, CurationStage.CARD_LEDGER)
    _record_card_ledger_attempt(repository, job.id, failed_primary)
    mismatched_repair = replace(
        repair,
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        generation_parameters=s2_generation_parameters(ProviderName.OPENAI, "gpt-5.2"),
        generation_parameters_sha256=hashlib.sha256(
            json.dumps(
                s2_generation_parameters(ProviderName.OPENAI, "gpt-5.2"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )
    with pytest.raises(ValueError, match="match the primary transport identity"):
        _record_card_ledger_attempt(repository, job.id, mismatched_repair)


def test_card_ledger_invalid_primary_then_repair_is_valid_at_startup(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    invalid = '{"importance":"low"}'
    failed_primary = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="validation_failed",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="request-1",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error="importance conflicts",
        invalid_response_sha256=hashlib.sha256(invalid.encode()).hexdigest(),
        invalid_response=invalid,
    )
    repair = replace(
        failed_primary,
        call_index=2,
        kind="repair",
        outcome="accepted",
        request_id="request-2",
        validation_error=None,
        invalid_response_sha256=None,
        invalid_response=None,
    )
    repository.start_stage(job.id, CurationStage.CARD_LEDGER)
    _record_card_ledger_attempt(repository, job.id, failed_primary)
    _record_card_ledger_attempt(repository, job.id, repair)

    repository.database.migrate()
    assert [
        (row["call_index"], row["kind"], row["outcome"])
        for row in repository.list_card_ledger_attempts(job.id)
    ] == [(1, "primary", "validation_failed"), (2, "repair", "accepted")]


def test_capture_card_ledger_persists_only_invalid_response_hash_and_allows_repair(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    capture = CaptureAnkiCurationRepository(repository.database)
    job = repository.create_job(_job_request(lecture_id))
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    invalid = '{"importance":"low"}'
    primary = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="validation_failed",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="request-1",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error="importance conflicts",
        invalid_response_sha256=hashlib.sha256(invalid.encode()).hexdigest(),
        invalid_response=invalid,
    )
    repair = replace(
        primary,
        call_index=2,
        kind="repair",
        outcome="accepted",
        request_id="request-2",
        validation_error=None,
        invalid_response_sha256=None,
        invalid_response=None,
    )
    repository.start_stage(job.id, CurationStage.CARD_LEDGER)
    with pytest.raises(ValueError, match="outcome payload"):
        _record_card_ledger_attempt(repository, job.id, replace(primary, invalid_response=None))
    _record_card_ledger_attempt(capture, job.id, primary)
    _record_card_ledger_attempt(capture, job.id, repair)
    assert primary.invalid_response == invalid
    rows = capture.list_card_ledger_attempts(job.id)
    assert rows[0]["invalid_response"] is None
    assert rows[0]["invalid_response_sha256"] == primary.invalid_response_sha256
    assert rows[1]["outcome"] == "accepted"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda attempt: replace(attempt, call_index=2, kind="primary"),
        lambda attempt: replace(attempt, outcome="accepted", validation_error="error"),
        lambda attempt: replace(attempt, input_tokens=-1),
        lambda attempt: replace(attempt, invalid_response_sha256="0" * 64),
        lambda attempt: replace(attempt, generation_parameters_sha256="0" * 64),
    ],
)
def test_card_ledger_attempt_repository_rejects_states_current_startup_rejects(
    tmp_path: Path,
    mutate,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    invalid = '{"importance":"low"}'
    attempt = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="validation_failed",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="request-1",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error="importance conflicts",
        invalid_response_sha256=hashlib.sha256(invalid.encode()).hexdigest(),
        invalid_response=invalid,
    )
    repository.start_stage(job.id, CurationStage.CARD_LEDGER)

    with pytest.raises(ValueError):
        _record_card_ledger_attempt(repository, job.id, mutate(attempt))
    assert repository.list_card_ledger_attempts(job.id) == []


@pytest.mark.parametrize(
    "corruption",
    [
        "UPDATE anki_card_ledger_attempts SET kind = 'repair'",
        "UPDATE anki_card_ledger_attempts SET call_index = 2, kind = 'repair'",
        """
        INSERT INTO anki_card_ledger_attempts (
            job_id, stage, stage_attempt, call_index, kind, outcome, provider, model,
            instruction_sha256, generation_parameters_json, generation_parameters_sha256,
            request_id, input_tokens, output_tokens, cost_microusd, validation_error,
            invalid_response_sha256, invalid_response, created_at
        )
        SELECT
            job_id, stage, stage_attempt, 2, 'repair', 'accepted', provider, model,
            instruction_sha256, generation_parameters_json, generation_parameters_sha256,
            request_id, input_tokens, output_tokens, cost_microusd, NULL, NULL, NULL,
            created_at
        FROM anki_card_ledger_attempts
        """,
        "UPDATE anki_card_ledger_attempts SET generation_parameters_sha256 = "
        "'0' || substr(generation_parameters_sha256, 2)",
        "UPDATE anki_card_ledger_attempts SET invalid_response = 'unexpected'",
    ],
)
def test_v24_startup_rejects_tampered_persisted_card_ledger_attempt_rows(
    tmp_path: Path,
    corruption: str,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    parameters = s2_generation_parameters(ProviderName.ANTHROPIC, "claude-sonnet-5")
    parameters_json = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    accepted = CardCentricLedgerAttempt(
        call_index=1,
        kind="primary",
        outcome="accepted",
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        instruction_sha256="a" * 64,
        generation_parameters=parameters,
        generation_parameters_sha256=hashlib.sha256(parameters_json.encode()).hexdigest(),
        request_id="request-1",
        input_tokens=1,
        output_tokens=2,
        cost_microusd=3,
        validation_error=None,
        invalid_response_sha256=None,
        invalid_response=None,
    )
    repository.start_stage(job.id, CurationStage.CARD_LEDGER)
    _record_card_ledger_attempt(repository, job.id, accepted)
    with repository.database.engine.begin() as connection:
        connection.execute(text(corruption))

    with pytest.raises(RuntimeError, match="card-ledger attempt"):
        repository.database.migrate()


def test_failed_job_can_retry_its_failed_stage_without_losing_artifacts(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    claimed = repository.claim_next_job(
        now,
        worker_id="worker-1",
        lease_seconds=30,
    )
    assert claimed is not None
    repository.start_stage(
        job.id,
        CurationStage.PREFLIGHT,
        expected_state=CurationState.PREFLIGHT,
        lease_owner="worker-1",
        now=now,
    )
    repository.fail_stage(
        job.id,
        CurationStage.PREFLIGHT,
        "provider returned malformed output",
        expected_state=CurationState.PREFLIGHT,
        lease_owner="worker-1",
        now=now,
    )
    repository.fail_job(
        job.id,
        "worker-1",
        "provider returned malformed output",
        expected_state=CurationState.PREFLIGHT,
        now=now,
    )

    retried = repository.retry_job(job.id)

    assert retried.state is CurationState.PREFLIGHT
    assert retried.error is None
    assert retried.available_at is None
    claimed_again = repository.claim_next_job(
        now,
        worker_id="worker-2",
        lease_seconds=30,
    )
    assert claimed_again is not None
    assert claimed_again.id == job.id


def test_v3_pre_review_states_are_claimable_recoverable_and_retryable() -> None:
    definitions = pipeline_stages(PipelineContractVersion.CARD_CENTRIC_V3)
    pre_review = tuple(
        definition
        for definition in definitions
        if definition.stage is not CurationStage.V3_R12_APPLY
    )

    assert all(
        definition.state in anki_repository_module._CLAIMABLE_STATES
        and definition.state in anki_repository_module._INTERRUPTED_PRE_REVIEW_STATES
        and anki_repository_module._RETRY_STATE_BY_STAGE[definition.stage] is definition.state
        and definition.state in anki_repository_module.ALLOWED_TRANSITIONS[CurationState.FAILED]
        for definition in pre_review
    )
    assert CurationState.V3_R12_APPLY not in anki_repository_module._CLAIMABLE_STATES
    assert CurationState.V3_R12_APPLY not in anki_repository_module._INTERRUPTED_PRE_REVIEW_STATES
    assert CurationStage.V3_R12_APPLY not in anki_repository_module._RETRY_STATE_BY_STAGE
    assert (
        CurationState.V3_R12_APPLY
        not in anki_repository_module.ALLOWED_TRANSITIONS[CurationState.FAILED]
    )


def test_known_blank_scope_card_centric_failure_repairs_and_rewinds_from_source_index(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            tag_allowlist=(),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        )
    )
    assert job.tag_allowlist == ("heme",)
    expected_configuration_sha256 = job.configuration_sha256
    with repository.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(job.id))
        assert stored is not None
        stored.tag_allowlist_json = "[]"
        stored.configuration_sha256 = "0" * 64
    source_artifact = StageArtifact(
        artifact_id=f"source_index:{'a' * 64}",
        stage=CurationStage.SOURCE_INDEX,
        kind="card_centric_source_index",
        relative_path=f"{job.id}/source_index/{'a' * 64}.json",
        input_sha256="b" * 64,
        content_sha256="a" * 64,
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        model_config_sha256=job.model_config_sha256,
    )
    repository.save_stage_artifact(job.id, source_artifact)
    other_job = repository.create_job(
        replace(
            _job_request(lecture_id, snapshot="other-snapshot"),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        )
    )
    other_artifact = StageArtifact(
        artifact_id=f"source_index:{'c' * 64}",
        stage=CurationStage.SOURCE_INDEX,
        kind="card_centric_source_index",
        relative_path=f"{other_job.id}/source_index/{'c' * 64}.json",
        input_sha256="d" * 64,
        content_sha256="c" * 64,
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        model_config_sha256=other_job.model_config_sha256,
    )
    repository.save_stage_artifact(other_job.id, other_artifact)
    reference = SourceReference(
        source_kind=SourceKind.SLIDE,
        revision_id=101,
        locator="slide:1",
        content_hash="c" * 64,
    )
    repository.replace_candidates(
        job.id,
        (
            Candidate(
                note_id=7,
                content_hash="d" * 64,
                best_concept_id="iron",
                provenance={"card_centric": {}},
                scores={},
                predicted_band="YES",
                verdict="yes",
                confidence=1.0,
                reason="stale",
                context_trap=False,
                recall_direction="card_centric",
                mnemonic_classification="none",
                dedupe_disposition="eligible",
                selected=True,
                retrieval_pass=RetrievalPass.PASS_1,
            ),
        ),
    )
    repository.save_gap_cards(
        job.id,
        (
            GapCard(
                concept_id="iron",
                text="{{c1::Iron}}",
                extra="stale",
                source_refs=(reference,),
                evidence_ids=("slide-1",),
                provenance={"card_centric": {}},
                content_hash="e" * 64,
                card_id="11111111-1111-1111-1111-111111111111",
            ),
        ),
    )
    repository.replace_source_evidence(
        job.id,
        (
            SourceEvidence(
                evidence_id="slide-1",
                concept_id="iron",
                support=EvidenceSupport.SUPPORTED,
                statement="Iron is present.",
                source_refs=(reference,),
                content_hash="f" * 64,
            ),
        ),
    )
    with repository.database.session() as session:
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=str(job.id),
                review_revision=1,
                payload_json="{}",
            )
        )
    repository.transition(job.id, CurationState.QUEUED, CurationState.PREFLIGHT)
    repository.transition(job.id, CurationState.PREFLIGHT, CurationState.BUILDING_SOURCE_INDEX)
    repository.transition(
        job.id,
        CurationState.BUILDING_SOURCE_INDEX,
        CurationState.CARD_BUILDING_LEDGER,
    )
    repository.transition(
        job.id,
        CurationState.CARD_BUILDING_LEDGER,
        CurationState.CARD_SCOPING_TAGS,
    )
    repository.start_stage(job.id, CurationStage.CARD_TAG_SCOPE)
    repository.fail_stage(
        job.id,
        CurationStage.CARD_TAG_SCOPE,
        "tag scope has no resolved tokens",
        expected_state=CurationState.CARD_SCOPING_TAGS,
        lease_owner=None,
    )
    repository.transition(
        job.id,
        CurationState.CARD_SCOPING_TAGS,
        CurationState.FAILED,
        "tag scope has no resolved tokens",
    )

    repaired = repository.retry_job(job.id)

    assert repaired.state is CurationState.BUILDING_SOURCE_INDEX
    assert repaired.tag_allowlist == ("heme",)
    assert repaired.configuration_sha256 == expected_configuration_sha256
    assert repaired.error is None
    assert repository.list_stage_artifacts(job.id) == []
    assert repository.list_stage_artifacts(other_job.id) == [other_artifact]
    assert repository.require_job(other_job.id).state is CurationState.QUEUED
    assert repository.get_stage(job.id, CurationStage.CARD_TAG_SCOPE) is None
    assert repository.list_candidates(job.id) == []
    assert repository.list_gap_cards(job.id) == []
    assert repository.list_source_evidence(job.id) == []
    with repository.database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AnkiReviewedReconciliationModel)
                .where(AnkiReviewedReconciliationModel.job_id == str(job.id))
            )
            == 0
        )


@pytest.mark.parametrize(
    ("subject", "topic"),
    [
        ("Unknown", "Unknown"),
        ("Heme Neuro", "Anemia I"),
    ],
)
def test_blank_scope_repair_rejects_unresolved_metadata_without_mutation(
    tmp_path: Path,
    subject: str,
    topic: str,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            tag_allowlist=(),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        )
    )
    artifact = StageArtifact(
        artifact_id=f"source_index:{'a' * 64}",
        stage=CurationStage.SOURCE_INDEX,
        kind="card_centric_source_index",
        relative_path=f"{job.id}/source_index/{'a' * 64}.json",
        input_sha256="b" * 64,
        content_sha256="a" * 64,
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        model_config_sha256=job.model_config_sha256,
    )
    repository.save_stage_artifact(job.id, artifact)
    with repository.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(job.id))
        lecture = session.get(LectureModel, lecture_id)
        assert stored is not None and lecture is not None
        stored.tag_allowlist_json = "[]"
        stored.state = CurationState.FAILED.value
        stored.error = "tag scope has no resolved tokens"
        lecture.subject = subject
        lecture.topic = topic
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=str(job.id),
                review_revision=1,
                payload_json="{}",
            )
        )
    repository.start_stage(job.id, CurationStage.CARD_TAG_SCOPE)
    repository.fail_stage(
        job.id,
        CurationStage.CARD_TAG_SCOPE,
        "tag scope has no resolved tokens",
        expected_state=CurationState.FAILED,
        lease_owner=None,
    )

    with pytest.raises(ValueError, match="Could not resolve exactly one"):
        repository.retry_job(job.id)

    unchanged = repository.require_job(job.id)
    assert unchanged.state is CurationState.FAILED
    assert unchanged.tag_allowlist == ()
    assert unchanged.error == "tag scope has no resolved tokens"
    assert repository.list_stage_artifacts(job.id) == [artifact]
    assert repository.get_stage(job.id, CurationStage.CARD_TAG_SCOPE) is not None
    with repository.database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AnkiReviewedReconciliationModel)
                .where(AnkiReviewedReconciliationModel.job_id == str(job.id))
            )
            == 1
        )


def test_nonblank_card_scope_is_not_rewound_by_a_matching_legacy_error(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        )
    )
    artifact = StageArtifact(
        artifact_id=f"source_index:{'a' * 64}",
        stage=CurationStage.SOURCE_INDEX,
        kind="card_centric_source_index",
        relative_path=f"{job.id}/source_index/{'a' * 64}.json",
        input_sha256="b" * 64,
        content_sha256="a" * 64,
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        model_config_sha256=job.model_config_sha256,
    )
    repository.save_stage_artifact(job.id, artifact)
    repository.start_stage(job.id, CurationStage.CARD_TAG_SCOPE)
    repository.fail_stage(
        job.id,
        CurationStage.CARD_TAG_SCOPE,
        "tag scope has no resolved tokens",
        expected_state=CurationState.QUEUED,
        lease_owner=None,
    )
    with repository.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(job.id))
        assert stored is not None
        stored.state = CurationState.FAILED.value
        stored.error = "tag scope has no resolved tokens"

    retried = repository.retry_job(job.id)

    assert retried.state is CurationState.CARD_SCOPING_TAGS
    assert retried.tag_allowlist == ("#AK_Step2_v12::Hematology",)
    assert repository.list_stage_artifacts(job.id) == [artifact]
    assert repository.get_stage(job.id, CurationStage.CARD_TAG_SCOPE) is not None


def test_failed_job_can_be_removed_from_the_run_list(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    claimed = repository.claim_next_job(
        now,
        worker_id="worker-1",
        lease_seconds=30,
    )
    assert claimed is not None
    repository.start_stage(
        job.id,
        CurationStage.PREFLIGHT,
        expected_state=CurationState.PREFLIGHT,
        lease_owner="worker-1",
        now=now,
    )
    repository.fail_stage(
        job.id,
        CurationStage.PREFLIGHT,
        "malformed output",
        expected_state=CurationState.PREFLIGHT,
        lease_owner="worker-1",
        now=now,
    )
    repository.fail_job(
        job.id,
        "worker-1",
        "malformed output",
        expected_state=CurationState.PREFLIGHT,
        now=now,
    )

    removed = repository.remove_failed_job(job.id)

    assert removed.state.value == "removed"
    assert repository.list_jobs() == []


def test_nonfailed_job_cannot_be_removed_from_the_run_list(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))

    with pytest.raises(ValueError, match="failed"):
        repository.remove_failed_job(job.id)

    assert [listed.id for listed in repository.list_jobs()] == [job.id]


def test_source_evidence_and_stage_artifacts_round_trip(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    source_ref = SourceReference(
        source_kind=SourceKind.SLIDE,
        revision_id=101,
        locator="slide:7",
        content_hash="b" * 64,
    )
    evidence = SourceEvidence(
        evidence_id="evidence-1",
        concept_id="concept-anemia",
        support=EvidenceSupport.SUPPORTED,
        statement="Iron deficiency causes microcytic anemia.",
        source_refs=(source_ref,),
        content_hash="c" * 64,
    )
    artifact = StageArtifact(
        artifact_id=f"source_index:{'e' * 64}",
        stage=CurationStage.SOURCE_INDEX,
        kind="source-index-manifest",
        relative_path=f"{job.id}/source_index/{'e' * 64}.json",
        input_sha256="d" * 64,
        content_sha256="e" * 64,
        model_config_sha256=job.model_config_sha256,
        metadata={"passages": 12},
    )

    repository.replace_source_evidence(job.id, (evidence,))
    repository.save_stage_artifact(job.id, artifact)

    assert repository.list_source_evidence(job.id) == [evidence]
    assert repository.list_stage_artifacts(job.id) == [artifact]


@pytest.mark.parametrize(
    ("pipeline_contract_version", "model_config_sha256", "message"),
    [
        (PipelineContractVersion.CARD_CENTRIC_V1, None, "pipeline contract"),
        (PipelineContractVersion.RETRIEVAL_V4, "f" * 64, "model configuration"),
    ],
)
def test_stage_commit_rejects_artifact_provenance_mismatch_without_mutation(
    tmp_path: Path,
    pipeline_contract_version: PipelineContractVersion,
    model_config_sha256: str | None,
    message: str,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    repository.start_stage(job.id, CurationStage.PREFLIGHT)
    artifact = StageArtifact(
        artifact_id=f"preflight:{'c' * 64}",
        stage=CurationStage.PREFLIGHT,
        kind="preflight_report",
        relative_path=f"{job.id}/preflight/{'c' * 64}.json",
        input_sha256="a" * 64,
        content_sha256="c" * 64,
        pipeline_contract_version=pipeline_contract_version,
        model_config_sha256=model_config_sha256 or job.model_config_sha256,
    )

    with pytest.raises(ValueError, match=message):
        repository.commit_stage(
            job.id,
            expected_state=CurationState.QUEUED,
            target_state=CurationState.PREFLIGHT,
            stage=CurationStage.PREFLIGHT,
            artifact=artifact,
        )

    assert repository.list_stage_artifacts(job.id) == []
    assert repository.require_job(job.id).state is CurationState.QUEUED
    stage = repository.get_stage(job.id, CurationStage.PREFLIGHT)
    assert stage is not None and stage.state == "running"


def test_candidates_gaps_and_review_revision_are_persisted(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    repository.replace_candidates(
        job.id,
        [
            Candidate(
                note_id=1479430487028,
                content_hash="a" * 64,
                best_concept_id="concept-anemia",
                provenance={"lecture_tag": True},
                scores={"semantic": 0.91},
                predicted_band="auto_include",
                verdict="include",
                confidence=0.98,
                reason="Directly tests the lecture objective.",
                context_trap=False,
                recall_direction="forward",
                mnemonic_classification="none",
                dedupe_disposition="survivor",
                selected=True,
                retrieval_pass=RetrievalPass.PASS_1,
            )
        ],
    )
    repository.save_gap_cards(
        job.id,
        [
            GapCard(
                concept_id="concept-retic",
                text="{{c1::Reticulocytes}} rise after treatment.",
                extra="Tracks marrow response.",
            )
        ],
    )

    saved = repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=0,
            candidate_selections={1479430487028: False},
            gap_edits=(
                GapCardEdit(
                    concept_id="concept-retic",
                    text="{{c1::Reticulocyte count}} rises after treatment.",
                    extra="Tracks marrow response after iron replacement.",
                    selected=True,
                ),
            ),
        ),
    )

    assert saved.revision == 1
    assert repository.list_candidates(job.id)[0].selected is False
    stored_gap = repository.list_gap_cards(job.id)[0]
    assert stored_gap.revision == 2
    assert stored_gap.text.startswith("{{c1::Reticulocyte count}}")

    with pytest.raises(ValueError, match="review revision"):
        repository.save_review(
            job.id,
            ReviewChangeSet(expected_revision=0),
        )


def test_split_gap_cards_share_a_concept_and_are_edited_by_card_id(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    first_id = "00000000-0000-0000-0000-000000000101"
    second_id = "00000000-0000-0000-0000-000000000102"
    repository.save_gap_cards(
        job.id,
        (
            GapCard(
                concept_id="C01",
                text="Mechanism starts with {{c1::step one}}.",
                extra="First atomic card.",
                card_id=first_id,
                provenance={"fact_id": "C01-M1", "split": True},
            ),
            GapCard(
                concept_id="C01",
                text="Mechanism ends with {{c1::step two}}.",
                extra="Second atomic card.",
                card_id=second_id,
                provenance={"fact_id": "C01-M1", "split": True},
            ),
        ),
    )

    repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=0,
            gap_edits=(
                GapCardEdit(
                    concept_id="C01",
                    card_id=second_id,
                    text="Mechanism ends with {{c1::the second step}}.",
                    extra="Edited second atomic card.",
                    selected=True,
                ),
            ),
        ),
    )

    stored = {card.card_id: card for card in repository.list_gap_cards(job.id)}
    assert set(stored) == {first_id, second_id}
    assert stored[first_id].revision == 1
    assert stored[second_id].revision == 2
    assert "the second step" in stored[second_id].text


def test_blank_card_id_edit_is_rejected_when_concept_has_multiple_cards(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    repository.save_gap_cards(
        job.id,
        (
            GapCard(
                concept_id="C01",
                text="Mechanism starts with {{c1::step one}}.",
                extra="First atomic card.",
                card_id="00000000-0000-0000-0000-000000000201",
                provenance={"fact_id": "C01-M1", "split": True},
            ),
            GapCard(
                concept_id="C01",
                text="Mechanism ends with {{c1::step two}}.",
                extra="Second atomic card.",
                card_id="00000000-0000-0000-0000-000000000202",
                provenance={"fact_id": "C01-M1", "split": True},
            ),
        ),
    )

    with pytest.raises(
        ValueError,
        match="gap card edit requires card_id when a concept has multiple cards",
    ):
        repository.save_review(
            job.id,
            ReviewChangeSet(
                expected_revision=0,
                gap_edits=(
                    GapCardEdit(
                        concept_id="C01",
                        card_id="",
                        text="Ambiguous edit without a card id.",
                        extra="Should be rejected.",
                        selected=True,
                    ),
                ),
            ),
        )


def test_blank_card_id_edit_still_works_for_a_single_gap_card(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    repository.save_gap_cards(
        job.id,
        [
            GapCard(
                concept_id="concept-solo",
                text="{{c1::Solo fact}} stands alone.",
                extra="Only one card for this concept.",
            )
        ],
    )

    saved = repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=0,
            gap_edits=(
                GapCardEdit(
                    concept_id="concept-solo",
                    card_id="",
                    text="{{c1::Solo fact}} stands alone, edited.",
                    extra="Only one card for this concept, edited.",
                    selected=True,
                ),
            ),
        ),
    )

    assert saved.revision == 1
    stored_gap = repository.list_gap_cards(job.id)[0]
    assert stored_gap.revision == 2
    assert stored_gap.text.endswith("edited.")


def test_coverage_judgment_cache_round_trips_immutable_record(
    tmp_path,
) -> None:
    repository, _ = _prepared_repository(tmp_path)
    record = JudgmentCacheRecord(
        cache_key="a" * 64,
        concept_content_hash="b" * 64,
        candidate_digest="c" * 64,
        prompt_version="judgment-v1",
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        result={
            "status": "missing",
            "supporting_note_ids": [],
            "missing_facts": ["Treatment response is absent."],
            "rationale": "No candidate covers treatment response.",
        },
        input_tokens=20,
        output_tokens=10,
        cost_microusd=5,
        created_at="2026-07-30T12:00:00+00:00",
    )

    repository.save_judgment_cache(record)
    repository.save_judgment_cache(record)

    assert repository.get_judgment_cache(record.cache_key) == record


def test_card_audit_cache_round_trips_immutable_record(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    record = AuditCacheRecord(
        cache_key="d" * 64,
        note_id=123,
        lecture_id=lecture_id,
        note_content_hash="a" * 64,
        source_digest="b" * 64,
        prompt_hash="123456789abc",
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        result={
            "nid": 123,
            "verdict": "keep",
            "primary_subject": "iron deficiency",
            "support": "both",
            "reason": "Supported by slides and transcript",
            "structure_issue": [],
        },
        input_tokens=100,
        output_tokens=20,
        cost_microusd=30,
        created_at="2026-07-31T12:00:00+00:00",
    )

    repository.save_audit_cache(record)
    repository.save_audit_cache(record)

    assert repository.get_audit_cache(record.cache_key) == record


def test_lecture_title_is_available_for_blind_audit_context(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)

    assert repository.lecture_title(lecture_id) == ("Heme Lymph Exam 1 Lecture 4: Anemia I")


def test_review_changes_and_tag_patches_are_append_only(
    tmp_path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    first_patch = TagPatch(
        note_id=42,
        before=("OMS::Old",),
        after=("OMS::New",),
        add_tags=("OMS::New",),
        remove_tags=("OMS::Old",),
        expected_tag_hash="a" * 64,
        tag_policy_version="tags-v1",
    )
    second_patch = TagPatch(
        note_id=42,
        before=("OMS::New",),
        after=("OMS::Final",),
        add_tags=("OMS::Final",),
        remove_tags=("OMS::New",),
        expected_tag_hash="b" * 64,
        tag_policy_version="tags-v1",
    )

    repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=0,
            reviewer="connor",
            tag_patches=(first_patch,),
        ),
    )
    repository.save_review(
        job.id,
        ReviewChangeSet(
            expected_revision=1,
            reviewer="connor",
            tag_patches=(second_patch,),
        ),
    )

    assert repository.list_tag_patches(job.id) == [
        first_patch,
        second_patch,
    ]
    changes = repository.list_review_changes(job.id)
    assert [change.revision for change in changes] == [1, 2]
    assert [change.prior_revision for change in changes] == [0, 1]
    assert all(change.reviewer == "connor" for change in changes)


def test_envelope_is_immutable_and_receipt_updates_delivery_state(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    draft = EnvelopeDraft(
        envelope_id="5dc4f15e-df92-4a32-964e-026b5d518a80",
        snapshot_id="snapshot-1",
        payload={"target_tag": "AnkiHub_Optional::LMU_OMS_II::HemeLymph"},
        operations=(
            EnvelopeOperationDraft(
                operation_id="3b9d1dbb-b57b-46f4-8346-fd45e0105042",
                operation_type="add_tags",
                payload={"note_ids": [1479430487028]},
            ),
        ),
    )

    stored = repository.create_envelope(job.id, draft)
    delivered = repository.record_receipt(
        stored.id,
        {"sync_status": "complete", "verified": True},
    )

    assert len(stored.payload_sha256) == 64
    assert stored.state == "pending"
    assert delivered.state == "complete"
    assert delivered.receipt_summary == {
        "sync_status": "complete",
        "verified": True,
    }
    with pytest.raises(ValueError, match="already has an envelope"):
        repository.create_envelope(job.id, draft)


@pytest.mark.parametrize(
    ("versions", "case"),
    [
        (None, "missing heartbeat"),
        ({}, "old heartbeat without the capability field"),
        (
            {"supported_envelope_contract_versions": (1,)},
            "explicit V1-only heartbeat",
        ),
    ],
)
def test_v2_envelope_creation_requires_persisted_agent_capability(
    tmp_path: Path,
    versions: dict[str, object] | None,
    case: str,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    before = repository.require_job(job.id)
    if versions is not None:
        repository.record_agent_heartbeat(
            agent_id="anki-agent",
            heartbeat_at="2026-08-05T18:00:00+00:00",
            versions=versions,
            active_snapshot_id="snapshot-1",
            health={"status": "ok"},
        )

    with pytest.raises(
        ValueError,
        match="envelope contract v2 unsupported; upgrade required; no mutation performed",
    ):
        repository.create_action_envelope(
            job.id,
            _v2_envelope(job_id=job.id),
            expected_review_revision=before.review_revision,
        )

    after = repository.require_job(job.id)
    assert _envelope_row_counts(repository) == (0, 0), case
    assert (after.state, after.review_revision, after.apply_state) == (
        before.state,
        before.review_revision,
        before.apply_state,
    )


def test_v2_envelope_creation_persists_when_agent_advertises_capability(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        )
    )
    repository.record_agent_heartbeat(
        agent_id="anki-agent",
        heartbeat_at="2026-08-05T18:00:00+00:00",
        versions={"supported_envelope_contract_versions": (1, 2)},
        active_snapshot_id="snapshot-1",
        health={"status": "ok"},
    )
    envelope = _v2_envelope(
        job_id=job.id,
        pipeline_contract_version=job.pipeline_contract_version.value,
        model_config_sha256=job.model_config_sha256,
        review_revision=job.review_revision,
    )

    stored = repository.create_action_envelope(job.id, envelope)

    assert stored.payload_sha256 == canonical_payload_sha256(envelope)
    assert repository.get_envelope(envelope.envelope_id) == envelope
    assert _envelope_row_counts(repository) == (1, len(envelope.operations))


def test_v2_envelope_persists_against_a_v2_job(tmp_path: Path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        )
    )
    repository.record_agent_heartbeat(
        agent_id="anki-agent",
        heartbeat_at="2026-08-05T18:00:00+00:00",
        versions={"supported_envelope_contract_versions": (1, 2)},
        active_snapshot_id="snapshot-1",
        health={"status": "ok"},
    )
    envelope = _v2_envelope(
        job_id=job.id,
        pipeline_contract_version="card_centric_v2",
        model_config_sha256=job.model_config_sha256,
        review_revision=job.review_revision,
    )

    stored = repository.create_action_envelope(job.id, envelope)

    assert stored.payload_sha256 == canonical_payload_sha256(envelope)
    assert repository.get_envelope(envelope.envelope_id) == envelope
    assert _envelope_row_counts(repository) == (1, len(envelope.operations))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pipeline_contract_version", "retrieval_v4", "pipeline contract"),
        ("model_config_sha256", "f" * 64, "model configuration"),
        ("review_revision", 1, "review revision"),
    ],
)
def test_v2_envelope_rejects_provenance_mismatches_without_mutation(
    tmp_path: Path,
    field: str,
    value: str | int,
    message: str,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(
        replace(
            _job_request(lecture_id),
            pipeline_contract_version=(
                PipelineContractVersion.RETRIEVAL_V4
                if field == "pipeline_contract_version"
                else PipelineContractVersion.CARD_CENTRIC_V1
            ),
        )
    )
    repository.record_agent_heartbeat(
        agent_id="anki-agent",
        heartbeat_at="2026-08-05T18:00:00+00:00",
        versions={"supported_envelope_contract_versions": (1, 2)},
        active_snapshot_id="snapshot-1",
        health={"status": "ok"},
    )
    envelope = _v2_envelope(
        job_id=job.id,
        pipeline_contract_version=(PipelineContractVersion.CARD_CENTRIC_V1.value),
        model_config_sha256=(
            str(value) if field == "model_config_sha256" else job.model_config_sha256
        ),
        review_revision=int(value) if field == "review_revision" else job.review_revision,
    )
    before = repository.require_job(job.id)

    with pytest.raises(ValueError, match=message):
        repository.create_action_envelope(job.id, envelope)

    after = repository.require_job(job.id)
    assert _envelope_row_counts(repository) == (0, 0)
    assert (after.state, after.review_revision, after.apply_state) == (
        before.state,
        before.review_revision,
        before.apply_state,
    )


def test_v2_envelope_cannot_be_replayed_to_another_matching_job(
    tmp_path: Path,
) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    request = replace(
        _job_request(lecture_id),
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
    )
    job_a = repository.create_job(request)
    job_b = repository.create_job(request)
    repository.record_agent_heartbeat(
        agent_id="anki-agent",
        heartbeat_at="2026-08-05T18:00:00+00:00",
        versions={"supported_envelope_contract_versions": (1, 2)},
        active_snapshot_id="snapshot-1",
        health={"status": "ok"},
    )
    envelope = _v2_envelope(
        job_id=job_a.id,
        pipeline_contract_version=job_a.pipeline_contract_version.value,
        model_config_sha256=job_a.model_config_sha256,
        review_revision=job_a.review_revision,
    )
    before = repository.require_job(job_b.id)

    with pytest.raises(ValueError, match="job ID"):
        repository.create_action_envelope(job_b.id, envelope)

    after = repository.require_job(job_b.id)
    assert _envelope_row_counts(repository) == (0, 0)
    assert (after.state, after.review_revision, after.apply_state) == (
        before.state,
        before.review_revision,
        before.apply_state,
    )


def test_action_envelope_operation_journal_is_durable(tmp_path) -> None:
    repository, lecture_id = _prepared_repository(tmp_path)
    job = repository.create_job(_job_request(lecture_id))
    envelope = EnvelopeBuilder(
        TagPolicy(
            pipeline_owned_roots=("OMS",),
            approved_optional_roots=("AnkiHub_Optional::LMU_OMS_II",),
            source_managed_roots=("#Pathoma",),
            version="tags-v1",
        )
    ).build(
        ReviewChangeSet(expected_revision=0),
        {},
        envelope_id=UUID("5dc4f15e-df92-4a32-964e-026b5d518a80"),
        snapshot_id="snapshot-1",
        target_deck="OMS::Heme::Lecture 3",
        target_tag="AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_3",
    )
    sync = next(
        operation for operation in envelope.operations if isinstance(operation, SyncOperation)
    )

    stored = repository.create_action_envelope(job.id, envelope)
    repository.begin_operation(envelope.envelope_id, sync.operation_id)
    repository.complete_operation(
        envelope.envelope_id,
        sync.operation_id,
        {"sync_status": "complete"},
    )
    repository.set_apply_state(
        envelope.envelope_id,
        ApplyState.APPLIED_LOCAL_SYNC_RETRYABLE,
        {"safe_error": "network unavailable"},
    )

    assert stored.payload_sha256 == envelope.payload_sha256
    assert repository.get_envelope(envelope.envelope_id) == envelope
    operation = repository.operation_record(
        envelope.envelope_id,
        sync.operation_id,
    )
    assert operation.state == "complete"
    assert operation.attempts == 1
    assert operation.result == {"sync_status": "complete"}
    assert repository.require_job(job.id).apply_state is ApplyState.APPLIED_LOCAL_SYNC_RETRYABLE
