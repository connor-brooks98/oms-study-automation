import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from oms_hub.anki.card_centric_contracts import (
    CardConcept,
    CardConceptLedger,
    serialize_card_centric_ledger,
)
from oms_hub.anki.correction_contracts import (
    A11HistoryEntry,
    A11HistorySnapshot,
    DeckSizingPolicy,
    DuplicateIdentity,
    EvidenceQuality,
    FactForbiddenClozeMap,
    GeneratedCardIdentity,
    GeneratedFactResolution,
    GeneratedOutputSet,
    GeneratedResolutionKind,
    MarginalValueReason,
    OrphanArtifactAdoptionEvidence,
    PinnedLectureMetadata,
    PromptSnapshotIdentity,
    ResolvedStageModelIdentity,
    SelectionMetadata,
    SelectionTier,
)
from oms_hub.anki.domain import CurationStage


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def test_deck_sizing_and_selection_metadata_enforce_quality_first_boundaries() -> None:
    assert DeckSizingPolicy().model_dump() == {
        "correction_contract_version": 1,
        "warning_floor": 60,
        "ordinary_target": 65,
        "soft_cap": 70,
        "counts_are_quotas": False,
        "padding_allowed": False,
    }
    with pytest.raises(ValidationError, match="66-70"):
        SelectionMetadata(
            identity="card-66",
            selected_position=66,
            tier=SelectionTier.T2,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
        )
    with pytest.raises(ValidationError, match="above 70"):
        SelectionMetadata(
            identity="card-71",
            selected_position=71,
            tier=SelectionTier.T1,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
            mandatory=True,
        )
    marginal = SelectionMetadata(
        identity="card-66",
        selected_position=66,
        tier=SelectionTier.T2,
        evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
        marginal_value_reason=MarginalValueReason.ONLY_VALID_REQUIRED_FACT,
    )
    assert marginal.marginal_value_reason is MarginalValueReason.ONLY_VALID_REQUIRED_FACT


def test_fact_scope_split_sequence_and_terminal_resolutions_are_conserved() -> None:
    cloze_map = FactForbiddenClozeMap(
        targets_by_fact_id={"C01:M1": ("porphobilinogen",), "C01:M2": ("lead",)}
    )
    assert cloze_map.targets_by_fact_id["C01:M1"] == ("porphobilinogen",)
    outputs = GeneratedOutputSet(
        canonical_all_generated=(
            GeneratedCardIdentity(
                card_id="G01",
                fact_id="C01:M1",
                split=True,
                split_index=1,
            ),
            GeneratedCardIdentity(
                card_id="G02",
                fact_id="C01:M1",
                split=True,
                split_index=2,
            ),
        ),
        selected_generated_card_ids=("G01",),
        resolutions=(
            GeneratedFactResolution(
                fact_id="C01:M1",
                kind=GeneratedResolutionKind.GENERATED,
                generated_card_ids=("G01", "G02"),
            ),
            GeneratedFactResolution(
                fact_id="C01:M2",
                kind=GeneratedResolutionKind.DUPLICATE_OF_EXISTING,
                duplicate_of=DuplicateIdentity(existing_note_id=42),
            ),
            GeneratedFactResolution(
                fact_id="C01:M3",
                kind=GeneratedResolutionKind.UNRESOLVED,
                unresolved_reason="No grounded atomic card can be produced.",
            ),
        ),
    )
    assert outputs.selected_generated_card_ids == ("G01",)
    assert outputs.resolutions[1].kind is GeneratedResolutionKind.DUPLICATE_OF_EXISTING

    with pytest.raises(ValidationError, match="sequential"):
        GeneratedOutputSet(
            canonical_all_generated=(
                GeneratedCardIdentity(
                    card_id="G01",
                    fact_id="C01:M1",
                    split=True,
                    split_index=2,
                ),
            ),
            selected_generated_card_ids=(),
            resolutions=(
                GeneratedFactResolution(
                    fact_id="C01:M1",
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=("G01",),
                ),
            ),
        )


def test_replay_history_and_orphan_adoption_contracts_validate_exact_identity() -> None:
    prompt = PromptSnapshotIdentity(
        prompt_id="card-centric-classifier",
        prompt_version="v2",
        content_sha256="a" * 64,
    )
    model_payload = {
        "stage": CurationStage.CARD_CLASSIFY.value,
        "provider": "anthropic",
        "model": "configured-model",
        "prompts": [prompt.model_dump(mode="json")],
        "generation_parameters": {"batch_size": 30},
    }
    identity = ResolvedStageModelIdentity(
        stage=CurationStage.CARD_CLASSIFY,
        provider="anthropic",
        model="configured-model",
        prompts=(prompt,),
        generation_parameters={"batch_size": 30},
        identity_sha256=_sha(model_payload),
    )
    assert identity.generation_parameters["batch_size"] == 30

    lecture_payload = {
        "lecture_id": 12,
        "title": "Heme synthesis",
        "metadata": {"exam": 1},
    }
    lecture = PinnedLectureMetadata(
        lecture_id=12,
        title="Heme synthesis",
        metadata={"exam": 1},
        metadata_sha256=_sha(lecture_payload),
    )
    assert lecture.title == "Heme synthesis"

    entry = A11HistoryEntry(
        job_id=UUID("11111111-1111-4111-8111-111111111111"),
        review_revision=2,
        yes_rate=0.75,
        reviewed_at=datetime(2026, 8, 8, tzinfo=UTC),
    )
    history_payload = [entry.model_dump(mode="json")]
    history = A11HistorySnapshot(entries=(entry,), snapshot_sha256=_sha(history_payload))
    assert len(history.entries) == 1

    evidence = OrphanArtifactAdoptionEvidence(
        job_id=entry.job_id,
        stage=CurationStage.CARD_CLASSIFY,
        stage_input_sha256="b" * 64,
        artifact_kind="card_centric_classifier",
        artifact_schema_version=2,
        content_sha256="c" * 64,
        complete_write_marker="atomic-rename+fsync",
        conflicting_committed_artifact=False,
    )
    assert not evidence.conflicting_committed_artifact
    with pytest.raises(ValidationError, match="False"):
        OrphanArtifactAdoptionEvidence(
            **{
                **evidence.model_dump(),
                "conflicting_committed_artifact": True,
            }
        )


def test_v1_ledger_serialization_remains_unchanged_by_additive_contracts() -> None:
    ledger = CardConceptLedger(
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Heme synthesis starts in mitochondria.",
                primary_entity="Heme synthesis",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
        lecture_entity_count=1,
    )
    serialized = serialize_card_centric_ledger(
        ledger,
        pipeline_contract_version="card_centric_v1",
    )

    assert "suggested_fact_count" not in serialized["concepts"][0]  # type: ignore[index]
    assert "split_index" not in serialized["concepts"][0]  # type: ignore[index]
