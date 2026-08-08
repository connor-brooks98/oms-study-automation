from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from oms_hub.anki.domain import (
    CreateCurationJob,
    CurationStage,
    CurationState,
    PipelineContractVersion,
    ResolvedModelConfiguration,
)
from oms_hub.anki.models import AnkiCurationJobModel, AnkiReviewedReconciliationModel
from oms_hub.anki.pipeline import StageArtifactStore, StageProduct
from oms_hub.anki.repository import AnkiCurationRepository
from tests.anki.test_web import SHA, FakeGateway
from tests.anki.test_web import prepared_app as web_prepared_app


@pytest.fixture
def review_surface_app(tmp_path: Path) -> tuple[TestClient, Any, int, int, FakeGateway]:
    yield from web_prepared_app.__wrapped__(tmp_path)


def _save_committed_artifact(
    app: Any,
    job_id: UUID,
    stage: CurationStage,
    kind: str,
    payload: dict[str, Any],
) -> None:
    repository: AnkiCurationRepository = app.state.anki_repository
    job = repository.require_job(job_id)
    artifacts: StageArtifactStore = app.state.anki_curation_pipeline.artifacts
    artifact = artifacts.write(
        job_id,
        stage,
        StageProduct(kind=kind, payload=payload),
        input_sha256=SHA,
        pipeline_contract_version=job.pipeline_contract_version,
        model_config_sha256=job.model_config_sha256,
    )
    repository.save_stage_artifact(job_id, artifact)


def _ready_v2_review_job(app: Any, lecture_id: int, revision_id: int) -> UUID:
    repository: AnkiCurationRepository = app.state.anki_repository
    job = repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id="heme-block-1",
            source_revision_ids=(revision_id,),
            source_revision_hashes={revision_id: SHA},
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=("#AK_Step2_v12::Hematology",),
            instruction_text="",
            target_deck="OMS::Heme::Lecture 4",
            target_tag="AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_4",
            index_snapshot_id="snapshot-test",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet-5",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=ResolvedModelConfiguration.card_centric_v2_default(
                "anthropic", "claude-sonnet-5"
            ),
            semantic_generation="33a3b975-0e93-41e6-8a44-ec255c7e1269",
            companion_generation="snapshot-test",
        )
    )
    with app.state.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(job.id))
        assert stored is not None
        stored.state = CurationState.READY_FOR_REVIEW.value
    return job.id


def _save_reviewed_selection(
    app: Any,
    job_id: UUID,
    *,
    review_revision: int = 0,
    selected_existing_note_ids: list[int],
    acknowledgement: dict[str, Any] | None = None,
) -> None:
    with app.state.database.session() as session:
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=str(job_id),
                review_revision=review_revision,
                payload_json=json.dumps(
                    {
                        "can_render_envelope": True,
                        "selection": {
                            "selected_existing_note_ids": selected_existing_note_ids,
                            "selected_generated_card_ids": [],
                            "overflow_acknowledgement": acknowledgement,
                        },
                    }
                ),
            )
        )


def _save_surface_artifacts(
    app: Any,
    job_id: UUID,
    *,
    selection_metadata: list[dict[str, Any]],
) -> None:
    _save_committed_artifact(
        app,
        job_id,
        CurationStage.CARD_EVIDENCE_AUDIT,
        "card_centric_evidence_audit",
        {
            "evidence_poor_concept_ids": ["C02"],
            "matched_slide_passage_ids": {"C01": ["SLD:01:0001"], "C02": []},
            "matched_slide_char_counts": {"C01": 182, "C02": 0},
            "threshold_chars": 50,
            "total_concepts": 2,
        },
    )
    _save_committed_artifact(
        app,
        job_id,
        CurationStage.CARD_COVERAGE,
        "card_centric_coverage",
        {
            "coverage": {
                "C01": {"evidence": [{"note_id": 42, "evidence_quality": "primary_source"}]}
            }
        },
    )
    _save_committed_artifact(
        app,
        job_id,
        CurationStage.CARD_SELECTION,
        "card_centric_selection",
        {
            "selected_existing_note_ids": [42],
            "selected_generated_card_ids": [],
            "minimum_target": 60,
            "target": 65,
            "cap": 70,
            "selection_metadata": selection_metadata,
        },
    )
    _save_committed_artifact(
        app,
        job_id,
        CurationStage.DEDUPE,
        "card_centric_dedupe",
        {
            "resolutions": [
                {
                    "card_id": "CC-duplicate",
                    "concept_id": "C02",
                    "fact_id": "C02-M1",
                    "status": "duplicate_of_existing",
                    "reason": "semantic duplicate of selected existing card",
                    "duplicate_of_existing_note_id": 42,
                    "duplicate_of_generated_card_id": None,
                }
            ]
        },
    )


def _review_surface(
    review_surface_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> tuple[TestClient, Any, UUID]:
    client, app, lecture_id, revision_id, _ = review_surface_app
    app.state.anki_curation_pipeline = SimpleNamespace(
        artifacts=StageArtifactStore(app.state.settings.data_dir / "review-surface-artifacts")
    )
    return client, app, _ready_v2_review_job(app, lecture_id, revision_id)


def test_review_api_uses_current_s9_selection_for_below_floor_warning(
    review_surface_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, job_id = _review_surface(review_surface_app)
    _save_surface_artifacts(
        app,
        job_id,
        selection_metadata=[
            {
                "identity": "note:1",
                "selected_position": 1,
                "tier": "T3",
                "evidence_quality": "primary_source",
            }
        ],
    )
    _save_reviewed_selection(
        app,
        job_id,
        selected_existing_note_ids=list(range(1, 60)),
    )

    response = client.get(f"/api/anki/jobs/{job_id}/review")

    assert response.status_code == 200
    payload = response.json()
    assert {"job", "convergence", "reconciliation", "groups", "concepts", "evidence"} <= set(
        payload
    )
    surface = payload["review_surface"]
    assert surface["evidence_quality"] == [
        {"identity": "note:1", "evidence_quality": "primary_source"},
        {
            "identity": "note:42",
            "concept_id": "C01",
            "evidence_quality": "primary_source",
        },
    ]
    assert surface["s2b_diagnostic"] == {
        "evidence_poor_concept_ids": ["C02"],
        "matched_slide_passage_ids": {"C01": ["SLD:01:0001"], "C02": []},
        "matched_slide_char_counts": {"C01": 182, "C02": 0},
        "threshold_chars": 50,
        "total_concepts": 2,
    }
    assert surface["duplicate_resolutions"] == [
        {
            "status": "duplicate_of_existing",
            "card_id": "CC-duplicate",
            "concept_id": "C02",
            "fact_id": "C02-M1",
            "reason": "semantic duplicate of selected existing card",
            "duplicate_of_existing_note_id": 42,
            "duplicate_of_generated_card_id": None,
        }
    ]
    selection = surface["selection"]
    assert selection["selected_existing_note_ids"] == list(range(1, 60))
    assert selection["selected_count"] == 59
    assert selection["below_warning_floor"] is True
    assert selection["overflow_acknowledgement"] == {
        "signed": False,
        "state": "pending",
        "provenance": None,
    }


def test_review_api_surfaces_marginal_reasons_only_for_consistent_66_to_70_selection(
    review_surface_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, job_id = _review_surface(review_surface_app)
    _save_surface_artifacts(
        app,
        job_id,
        selection_metadata=[
            {
                "identity": "note:66",
                "selected_position": 66,
                "tier": "T2",
                "evidence_quality": "summary_grounded",
                "marginal_value_reason": "only_valid_required_fact",
            },
            {
                "identity": "note:70",
                "selected_position": 70,
                "tier": "T4",
                "evidence_quality": "fast_pass",
                "marginal_value_reason": "validated_necessary_split",
            },
        ],
    )
    _save_reviewed_selection(
        app,
        job_id,
        selected_existing_note_ids=list(range(1, 71)),
    )

    response = client.get(f"/api/anki/jobs/{job_id}/review")

    surface = response.json()["review_surface"]
    assert surface["evidence_quality"] == [
        {"identity": "note:42", "concept_id": "C01", "evidence_quality": "primary_source"},
        {"identity": "note:66", "evidence_quality": "summary_grounded"},
        {"identity": "note:70", "evidence_quality": "fast_pass"},
    ]
    selection = surface["selection"]
    assert selection["selected_count"] == 70
    assert selection["below_warning_floor"] is False
    assert [item["marginal_value_reason"] for item in selection["selection_metadata"]] == [
        "only_valid_required_fact",
        "validated_necessary_split",
    ]


def test_review_api_omits_metadata_for_deselected_marginal_and_overflow_identities(
    review_surface_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, job_id = _review_surface(review_surface_app)
    _save_surface_artifacts(
        app,
        job_id,
        selection_metadata=[
            {
                "identity": "note:70",
                "selected_position": 70,
                "tier": "T4",
                "evidence_quality": "summary_grounded",
                "marginal_value_reason": "only_valid_required_fact",
            },
            {
                "identity": "note:71",
                "selected_position": 71,
                "tier": "T1",
                "evidence_quality": "primary_source",
                "mandatory": True,
                "overflow_reason": "Only valid identity covering required fact C02-M1.",
                "manual_acknowledgement_required": True,
            },
        ],
    )
    _save_reviewed_selection(
        app,
        job_id,
        selected_existing_note_ids=list(range(1, 70)),
    )

    surface = client.get(f"/api/anki/jobs/{job_id}/review").json()["review_surface"]

    assert surface["selection"]["selected_count"] == 69
    assert surface["selection"]["selection_metadata"] == []
    assert surface["evidence_quality"] == [
        {"identity": "note:42", "concept_id": "C01", "evidence_quality": "primary_source"}
    ]


def test_review_api_surfaces_server_validated_overflow_acknowledgement(
    review_surface_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, job_id = _review_surface(review_surface_app)
    _save_surface_artifacts(
        app,
        job_id,
        selection_metadata=[
            {
                "identity": "note:71",
                "selected_position": 71,
                "tier": "T1",
                "evidence_quality": "primary_source",
                "mandatory": True,
                "overflow_reason": "Only valid identity covering required fact C02-M1.",
                "manual_acknowledgement_required": True,
            }
        ],
    )
    repository: AnkiCurationRepository = app.state.anki_repository
    selected = tuple(range(1, 72))
    acknowledgement = repository.issue_card_centric_overflow_acknowledgement(
        job_id,
        review_revision=0,
        selected_note_ids=selected,
        selected_generated_ids=(),
        mandatory_note_ids=selected,
        mandatory_generated_ids=(),
        cap=70,
    )
    _save_reviewed_selection(
        app,
        job_id,
        selected_existing_note_ids=list(selected),
        acknowledgement=acknowledgement,
    )

    selection = client.get(f"/api/anki/jobs/{job_id}/review").json()["review_surface"]["selection"]

    assert selection["selected_count"] == 71
    assert selection["acknowledgement_satisfied"] is True
    assert selection["overflow_acknowledgement"]["signed"] is True
    assert selection["overflow_acknowledgement"]["state"] == "signed"
    assert "token" not in selection["overflow_acknowledgement"]["provenance"]
    assert "signature" not in selection["overflow_acknowledgement"]["provenance"]


@pytest.mark.parametrize("invalid_kind", ["forged", "stale"])
def test_review_api_marks_forged_or_stale_overflow_acknowledgement_pending(
    review_surface_app: tuple[TestClient, Any, int, int, FakeGateway],
    invalid_kind: str,
) -> None:
    client, app, job_id = _review_surface(review_surface_app)
    _save_surface_artifacts(
        app,
        job_id,
        selection_metadata=[
            {
                "identity": "note:71",
                "selected_position": 71,
                "tier": "T1",
                "evidence_quality": "primary_source",
                "mandatory": True,
                "overflow_reason": "Only valid identity covering required fact C02-M1.",
                "manual_acknowledgement_required": True,
            }
        ],
    )
    repository: AnkiCurationRepository = app.state.anki_repository
    selected = tuple(range(1, 72))
    acknowledgement = repository.issue_card_centric_overflow_acknowledgement(
        job_id,
        review_revision=0,
        selected_note_ids=selected,
        selected_generated_ids=(),
        mandatory_note_ids=selected,
        mandatory_generated_ids=(),
        cap=70,
    )
    if invalid_kind == "forged":
        acknowledgement = {**acknowledgement, "signature": "forged"}
    else:
        with app.state.database.session() as session:
            stored = session.get(AnkiCurationJobModel, str(job_id))
            assert stored is not None
            stored.review_revision = 1
        _save_reviewed_selection(
            app,
            job_id,
            review_revision=1,
            selected_existing_note_ids=list(selected),
            acknowledgement=acknowledgement,
        )
    if invalid_kind == "forged":
        _save_reviewed_selection(
            app,
            job_id,
            selected_existing_note_ids=list(selected),
            acknowledgement=acknowledgement,
        )

    payload = client.get(f"/api/anki/jobs/{job_id}/review").json()
    selection = payload["review_surface"]["selection"]
    assert selection["selected_count"] == 71
    assert selection["overflow_acknowledgement"] == {
        "signed": False,
        "state": "pending",
        "provenance": None,
    }
    assert selection["acknowledgement_satisfied"] is False


def test_review_template_and_static_copy_keep_sizing_quality_first() -> None:
    root = Path(__file__).parents[2]
    template = (root / "src/oms_hub/web/templates/anki_review.html").read_text(encoding="utf-8")
    script = (root / "src/oms_hub/web/static/anki.js").read_text(encoding="utf-8")

    assert "70 is a soft cap—not a quota to fill" in template
    assert "no card should be added merely to reach a count" in template
    assert "data-review-s2b-diagnostic" in template
    assert "S2b evidence diagnostic (diagnostic only)" in script
    assert "do not add weak, redundant, or ungrounded cards" in script
    assert "signed acknowledgement" in script
    assert "hard cap" not in template.casefold()
    assert "padding" not in template.casefold()
