from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from selectolax.parser import HTMLParser

from oms_hub.anki.apply import ApplyCoordinator
from oms_hub.anki.card_centric import build_source_index
from oms_hub.anki.correction_contracts import (
    EvidenceQuality,
    GeneratedFactResolution,
    GeneratedResolutionKind,
    MarginalValueReason,
    SelectionMetadata,
    SelectionTier,
)
from oms_hub.anki.domain import (
    Candidate,
    CreateCurationJob,
    CurationStage,
    CurationState,
    EvidenceSupport,
    GapCard,
    PipelineContractVersion,
    ResolvedModelConfiguration,
    RetrievalPass,
    SourceEvidence,
    SourceKind,
    SourceReference,
    StageArtifact,
)
from oms_hub.anki.models import AnkiCurationJobModel, AnkiReviewedReconciliationModel
from oms_hub.anki.reconciliation import (
    AuditResolution,
    CardCentricReconciliationInput,
    GeneratedResolution,
)
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.runtime import AnkiPreflight
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import revision_fingerprint
from oms_hub.anki.tag_policy import TagPolicy, tag_hash
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.llm.domain import LLMTask, ProviderName
from oms_hub.models import (
    LectureModel,
    StudyRevisionModel,
    UploadBatchModel,
    UploadItemModel,
)
from oms_hub.study_generation.domain import GenerationKind
from oms_hub.study_generation.outline import OutlinePdfRenderer
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.web.anki_routes import (
    _concept_review_groups,
    _convergence_summary,
    _reconciliation_summary,
    _review_reconciliation_summary,
)

SHA = "a" * 64
TARGET_TAG = "AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_4"
AGENT_HOST = "anki-agent.test"
AGENT_TOKEN = "test-agent-token"


def _pinned_private_fixture(tmp_path: Path) -> tuple[Path, str]:
    """Build a structurally valid external fixture without checking it into Git."""
    passage = SourcePassage.create(
        revision_id=1,
        lecture_id=7,
        artifact_id="private-slides",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Lecture07 source evidence",
    )
    source = build_source_index(
        [passage], snapshot_id="private-fixture", source_revision_hashes={1: SHA}
    )
    cards = [
        {
            "note_id": 10_000 + index,
            "content_sha256": f"{index + 1:064x}",
            "text": f"real private card {index}",
            "extra": "",
            "tags": ["#AK::Heme"],
        }
        for index in range(124)
    ]
    payload: dict[str, Any] = {
        "fixture_version": "private-v1",
        "source_index": source.model_dump(mode="json"),
        "cards": cards,
        "baseline_verdicts": {str(card["note_id"]): "YES" for card in cards},
        "missed_concept_ids": [f"C{index:02d}" for index in range(1, 7)],
        "named_cases": {"real_missed_concepts": [card["note_id"] for card in cards[:6]]},
    }
    pin = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["sha256"] = pin
    path = tmp_path / "private-lecture07.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, pin


def test_concept_review_groups_keep_yes_maybe_flagged_and_generated_separate() -> None:
    candidate = Candidate(
        note_id=42,
        content_hash="a" * 64,
        best_concept_id="C01",
        provenance={"card_centric": {"verdict": "YES", "flags": []}},
        scores={},
        predicted_band="YES",
        verdict="yes",
        confidence=1.0,
        reason="grounded",
        context_trap=False,
        recall_direction="card_centric",
        mnemonic_classification="none",
        dedupe_disposition="eligible",
        selected=True,
    )
    gap = GapCard(
        concept_id="C01",
        text="{{c1::fact}}",
        extra="",
        content_hash="b" * 64,
        card_id="CC-1",
    )

    groups = _concept_review_groups([candidate], [gap])

    assert groups == [
        {
            "concept_id": "C01",
            "yes": [{"note_id": 42, "reason": "grounded", "selected": True}],
            "maybe": [],
            "flagged": [],
            "generated": [{"card_id": "CC-1", "selected": True, "validation_state": "valid"}],
            "uncovered": False,
        }
    ]


class AgentSecretStore:
    def get(self, key: str) -> str | None:
        return AGENT_TOKEN if key == "anki-agent-token" else None


class FakeGateway:
    def __init__(self) -> None:
        self.notes: dict[int, dict[str, Any]] = {
            42: {
                "noteId": 42,
                "modelName": "AnKingOverhaul",
                "fields": {
                    "Text": {
                        "value": "{{c1::Iron deficiency}} causes anemia.",
                        "order": 0,
                    },
                    "Extra": {"value": "Ferritin is low.", "order": 1},
                },
                "tags": ["#Pathoma::Hematology", "OMS::Old"],
                "cards": [1_042],
            }
        }
        self.next_note_id = 100
        self.sync_calls = 0
        self.fail_sync_calls: set[int] = set()
        self.created_note_ids: list[int] = []

    async def sync(self) -> None:
        self.sync_calls += 1
        if self.sync_calls in self.fail_sync_calls:
            from oms_hub.anki.ankiconnect import AnkiConnectUnavailable

            raise AnkiConnectUnavailable("simulated sync outage")

    async def notes_info(
        self,
        note_ids: Sequence[int],
    ) -> list[dict[str, Any]]:
        return [self.notes[note_id] for note_id in note_ids if note_id in self.notes]

    async def find_notes(self, query: str) -> list[int]:
        marker = query.removeprefix("tag:")
        return [note_id for note_id, note in self.notes.items() if marker in note["tags"]]

    async def add_tags(
        self,
        note_ids: Sequence[int],
        tags: Sequence[str],
    ) -> None:
        for note_id in note_ids:
            current = self.notes[note_id]["tags"]
            known = {tag.casefold() for tag in current}
            current.extend(tag for tag in tags if tag.casefold() not in known)

    async def remove_tags(
        self,
        note_ids: Sequence[int],
        tags: Sequence[str],
    ) -> None:
        removed = {tag.casefold() for tag in tags}
        for note_id in note_ids:
            self.notes[note_id]["tags"] = [
                tag for tag in self.notes[note_id]["tags"] if tag.casefold() not in removed
            ]

    async def add_notes(
        self,
        notes: Sequence[dict[str, Any]],
    ) -> list[int]:
        created: list[int] = []
        for note in notes:
            note_id = self.next_note_id
            self.next_note_id += 1
            self.notes[note_id] = {
                "noteId": note_id,
                "modelName": note["modelName"],
                "fields": {
                    name: {"value": value, "order": index}
                    for index, (name, value) in enumerate(note["fields"].items())
                },
                "tags": list(note["tags"]),
                "cards": [note_id + 1_000],
            }
            created.append(note_id)
            self.created_note_ids.append(note_id)
        return created


class FakeRuntime:
    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway

    async def ensure_running(self) -> AnkiPreflight:
        return AnkiPreflight(
            reachable=True,
            ankiconnect_version=6,
            active_profile="Disposable Test",
            collection_accessible=True,
            sync_available=True,
            blocking_reason=None,
        )


class FakeCompanionIndex:
    semantic_coverage = 1.0

    def snapshot_id(self) -> str:
        return "snapshot-test"

    def list_deck_names(self) -> tuple[str, ...]:
        return ("AnKing Step Deck", "Sketchy Pepper")

    def semantic_alignment(
        self,
        *,
        note_ids: Sequence[int],
        content_hashes: Sequence[str],
    ) -> object:
        del note_ids, content_hashes
        return SimpleNamespace(
            coverage=self.semantic_coverage,
            missing_or_stale_note_ids=(() if self.semantic_coverage >= 0.995 else (42,)),
        )

    def get_note(self, note_id: int) -> object | None:
        if note_id != 42:
            return None
        return SimpleNamespace(
            note_id=42,
            model_name="AnKingOverhaul",
            text="Iron deficiency causes anemia.",
            extra="Ferritin is low.",
            raw_fields={
                "Text": "{{c1::Iron deficiency}} causes anemia.",
                "Extra": "Ferritin is low.",
            },
            tags=("#Pathoma::Hematology", "OMS::Old"),
            deck_names=("AnKing Step Deck",),
            source_families=("pathoma",),
        )


class FakeSemanticStore:
    def load(self) -> object:
        return SimpleNamespace(
            manifest=SimpleNamespace(
                generation=UUID("33a3b975-0e93-41e6-8a44-ec255c7e1269"),
                note_ids=(42,),
                content_hashes=("b" * 64,),
            )
        )


@pytest.fixture
def prepared_app(tmp_path: Path) -> tuple[TestClient, Any, int, int, FakeGateway]:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            anki_agent_hostname=AGENT_HOST,
            anki_agent_token_key="anki-agent-token",
        )
    )
    slides_path = tmp_path / "slides.pptx"
    transcript_path = tmp_path / "transcript.txt"
    slides_path.write_bytes(b"test presentation fixture")
    transcript_path.write_text("Ferritin falls early.", encoding="utf-8")
    with app.state.database.session() as session:
        lecture = LectureModel(
            subject="Heme Lymph",
            exam_number=1,
            lecture_number=4,
            topic='Anemia "I"',
            lecturer="Professor",
        )
        session.add(lecture)
        session.flush()
        batch = UploadBatchModel(id="test-batch", kind="slides")
        session.add(batch)
        session.flush()
        session.add(
            UploadItemModel(
                id="test-slides",
                batch_id=batch.id,
                kind="slides",
                original_filename="slides.pptx",
                staged_path=str(slides_path),
                sha256=SHA,
                size_bytes=slides_path.stat().st_size,
                lecture_id=lecture.id,
            )
        )
        session.add(
            UploadItemModel(
                id="test-transcript",
                batch_id=batch.id,
                kind="transcripts",
                original_filename="transcript.txt",
                staged_path=str(transcript_path),
                sha256="c" * 64,
                size_bytes=transcript_path.stat().st_size,
                lecture_id=lecture.id,
            )
        )
        session.flush()
        revision = StudyRevisionModel(
            upload_item_id="test-slides",
            lecture_id=lecture.id,
            kind="slides",
            source_sha256=SHA,
            immutable_source_path=str(slides_path),
            state="accepted",
            current=True,
        )
        session.add(revision)
        session.flush()
        transcript = StudyRevisionModel(
            upload_item_id="test-transcript",
            lecture_id=lecture.id,
            kind="transcripts",
            source_sha256="c" * 64,
            immutable_source_path=str(transcript_path),
            state="accepted",
            current=True,
        )
        session.add(transcript)
        session.flush()
        lecture_id = lecture.id
        revision_id = revision.id

    summary_payload = OutlinePdfRenderer().render(
        "Lecture 4 Outline",
        "# CORE CONCEPTS\n- Iron deficiency [1]\n\n"
        "# DEPTH MAP\n- DEEP: iron absorption [2]\n\n"
        "# PROFESSOR EMPHASIS FLAGS\n- Repeated: ferritin falls early [3]",
    )
    summary_path = tmp_path / "outline.pdf"
    summary_path.write_bytes(summary_payload)
    generation = GenerationRepository(app.state.database)
    outline_job = generation.queue(lecture_id, GenerationKind.OUTLINE)
    generation.record_outline(
        lecture_id,
        outline_job.id,
        summary_path,
        hashlib.sha256(summary_payload).hexdigest(),
    )

    gateway = FakeGateway()
    runtime = FakeRuntime(gateway)
    policy = TagPolicy(
        pipeline_owned_roots=("OMS",),
        approved_optional_roots=("AnkiHub_Optional::LMU_OMS_II",),
        source_managed_roots=("#Pathoma", "#AK_Step", "AnkiHub_"),
        version="tags-v1",
    )
    app.state.anki_runtime = runtime
    app.state.anki_companion_index = FakeCompanionIndex()
    app.state.anki_semantic_store = FakeSemanticStore()
    app.state.anki_tag_policy = policy
    app.state.anki_apply_coordinator = ApplyCoordinator(
        app.state.anki_repository,
        gateway,
        runtime=runtime,
    )
    app.state.secrets = AgentSecretStore()
    client = TestClient(app)
    yield client, app, lecture_id, revision_id, gateway
    client.close()
    app.state.database.close()


def _create_payload(lecture_id: int, revision_id: int) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "lecture_id": lecture_id,
        "block_id": "heme-block-1",
        "source_revision_ids": [revision_id, revision_id + 1],
        "deck_allowlist": ["AnKing Step Deck"],
        "tag_allowlist": ["#AK_Step2_v12::Hematology"],
        "target_deck": "OMS::Heme::Lecture 4",
        "target_tag": TARGET_TAG,
        "index_snapshot_id": "snapshot-test",
        "instruction_text": "Prioritize the lecturer's comparisons.",
        "lcl_prompt_version": "lcl-v1",
        "judgment_rubric_version": "judgment-v1",
        "gap_prompt_version": "gap-v1",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
    }


def test_agent_heartbeat_persists_envelope_capabilities_end_to_end(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, _, _, _ = prepared_app
    headers = {
        "host": AGENT_HOST,
        "authorization": f"Bearer {AGENT_TOKEN}",
        "x-oms-agent-id": "anki-agent",
    }
    base = {
        "contract_version": 1,
        "agent_id": "anki-agent",
        "agent_version": "0.1.0",
        "anki_version": "25.02",
        "ankiconnect_version": 6,
        "active_snapshot_id": None,
        "health": "ok",
        "observed_at": "2026-08-05T18:00:00Z",
    }

    response = client.post(
        "/agent/v1/heartbeat",
        headers=headers,
        json={**base, "supported_envelope_contract_versions": [1, 2]},
    )

    assert response.status_code == 200
    assert app.state.anki_repository.agent_state().versions[
        "supported_envelope_contract_versions"
    ] == [1, 2]

    response = client.post("/agent/v1/heartbeat", headers=headers, json=base)

    assert response.status_code == 200
    assert app.state.anki_repository.agent_state().versions[
        "supported_envelope_contract_versions"
    ] == [1]


def _ready_job(
    app: Any,
    lecture_id: int,
    revision_id: int,
    *,
    pipeline_contract_version: PipelineContractVersion = PipelineContractVersion.RETRIEVAL_V4,
    resolved_model_config: ResolvedModelConfiguration | None = None,
) -> UUID:
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
            target_tag=TARGET_TAG,
            index_snapshot_id="snapshot-test",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet-5",
            pipeline_contract_version=pipeline_contract_version,
            resolved_model_config=resolved_model_config,
            semantic_generation="33a3b975-0e93-41e6-8a44-ec255c7e1269",
            companion_generation="snapshot-test",
        )
    )
    repository.replace_candidates(
        job.id,
        (
            Candidate(
                note_id=42,
                content_hash=SHA,
                best_concept_id="iron-deficiency",
                provenance={"query": "iron deficiency anemia"},
                scores={"boosted_score": 0.93, "rrf_score": 0.04},
                predicted_band="covered",
                verdict="include",
                confidence=0.96,
                reason="Directly tests the lecture concept.",
                context_trap=False,
                recall_direction="forward",
                mnemonic_classification="none",
                dedupe_disposition="unique",
                selected=True,
                retrieval_pass=RetrievalPass.PASS_1,
            ),
        ),
    )
    reference = SourceReference(
        source_kind=SourceKind.SLIDE,
        revision_id=revision_id,
        locator="slide 12",
        content_hash=SHA,
    )
    repository.replace_source_evidence(
        job.id,
        (
            SourceEvidence(
                evidence_id="slide-12",
                concept_id="iron-absorption",
                support=EvidenceSupport.SUPPORTED,
                statement="Iron absorption occurs in the duodenum.",
                source_refs=(reference,),
                content_hash=SHA,
            ),
        ),
    )
    repository.save_gap_cards(
        job.id,
        (
            GapCard(
                concept_id="iron-absorption",
                text="Iron is absorbed in the {{c1::duodenum}}.",
                extra="Lecture slide 12.",
                selected=True,
                validation_state="valid",
                source_refs=(reference,),
                evidence_ids=("slide-12",),
                provenance={
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "prompt_version": "gap-v1",
                    "confidence": 0.97,
                },
                initial_tags=("OMS::Generated",),
                content_hash="b" * 64,
            ),
        ),
    )
    with app.state.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(job.id))
        assert stored is not None
        stored.state = CurationState.READY_FOR_REVIEW.value
    return job.id


def test_v2_mixed_overflow_envelope_accepts_database_order_not_frozen_order(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, gateway = prepared_app
    repository: AnkiCurationRepository = app.state.anki_repository
    job_id = _ready_job(
        app,
        lecture_id,
        revision_id,
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        resolved_model_config=ResolvedModelConfiguration.card_centric_v2_default(
            "anthropic", "claude-sonnet-5"
        ),
    )
    frozen_existing = (2, 1, *range(3, 71))
    database_existing = tuple(range(1, 71))
    repository.replace_candidates(
        job_id,
        tuple(
            Candidate(
                note_id=note_id,
                content_hash=f"{note_id:064x}",
                best_concept_id="C01",
                provenance={"card_centric": {"covered_concept_ids": ["C01"]}},
                scores={},
                predicted_band="covered",
                verdict="keep",
                confidence=1,
                reason="selected fixture card",
                context_trap=False,
                recall_direction="forward",
                mnemonic_classification="none",
                dedupe_disposition="unique",
                selected=True,
            )
            for note_id in database_existing
        ),
    )
    for note_id in database_existing:
        gateway.notes[note_id] = {
            "noteId": note_id,
            "modelName": "AnKingOverhaul",
            "fields": {
                "Text": {"value": f"Fixture {{c1::note {note_id}}}", "order": 0},
                "Extra": {"value": "Frozen selection fixture.", "order": 1},
            },
            "tags": ["#Pathoma::Hematology"],
            "cards": [note_id + 1_000],
        }
    existing_gap = repository.list_gap_cards(job_id)[0]
    repository.save_gap_cards(
        job_id,
        (replace(existing_gap, card_id="G1", concept_id="C01"),),
    )
    generated = GeneratedResolution(
        card_id="G1",
        fact_id="C01-M1",
        text="The fixture finding is {{c1::present}}.",
    )
    identities = [
        *(f"existing:{note_id}" for note_id in frozen_existing),
        "generated:G1",
    ]
    metadata = tuple(
        SelectionMetadata(
            identity=identity,
            selected_position=position,
            tier=SelectionTier.T1,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
            mandatory=position == 71,
            marginal_value_reason=(
                MarginalValueReason.ONLY_VALID_REQUIRED_FACT if 66 <= position <= 70 else None
            ),
            overflow_reason="required fixture coverage" if position == 71 else None,
            manual_acknowledgement_required=position == 71,
        )
        for position, identity in enumerate(identities, start=1)
    )
    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=("C01-M1",),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(generated,),
        raw_generated_cards=(generated,),
        canonical_generated_cards=(generated,),
        terminal_resolutions=(
            GeneratedFactResolution(
                fact_id="C01-M1",
                kind=GeneratedResolutionKind.GENERATED,
                generated_card_ids=("G1",),
            ),
        ),
        terminal_resolutions_provided=True,
        unresolved_fact_ids=(),
        expected_scoped_nids=frozen_existing,
        classifications=tuple(
            AuditResolution(nid=note_id, verdict="keep") for note_id in frozen_existing
        ),
        eligible_yes_nids=frozen_existing,
        selected_nids=frozen_existing,
        selected_generated_card_ids=("G1",),
        generated_card_ids=("G1",),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_generated_card_ids=("G1",),
        covered_concept_ids_by_nid={note_id: ("C01",) for note_id in frozen_existing},
        generated_concept_id_by_card_id={"G1": "C01"},
        selection_metadata=metadata,
        selection_order=tuple(item.identity for item in metadata),
        selected_count=71,
        below_warning_floor=False,
    )
    with app.state.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(job_id))
        assert stored is not None
        stored.gap_prompt_version = "card-centric-gap-v2"
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=str(job_id),
                review_revision=0,
                payload_json=json.dumps(
                    {
                        "contract_version": "card_centric_s9_v1",
                        "snapshot": snapshot.model_dump(mode="json"),
                        "selection": {
                            "selected_existing_note_ids": list(frozen_existing),
                            "selected_generated_card_ids": ["G1"],
                            "mandatory_note_ids": [],
                            "mandatory_generated_card_ids": ["G1"],
                            "cap": 70,
                        },
                    }
                ),
            )
        )
    repository.record_agent_heartbeat(
        agent_id="anki-agent",
        heartbeat_at="2026-08-05T18:00:00+00:00",
        versions={"supported_envelope_contract_versions": (1, 2)},
        active_snapshot_id="snapshot-test",
        health={"status": "ok"},
    )

    tampered = client.post(
        f"/api/anki/jobs/{job_id}/overflow-acknowledgement",
        json={
            "review_revision": 0,
            "selected_existing_note_ids": [*database_existing[:-1], 999],
            "selected_generated_card_ids": ["G1"],
        },
    )
    assert tampered.status_code == 422

    issued = client.post(
        f"/api/anki/jobs/{job_id}/overflow-acknowledgement",
        json={
            "review_revision": 0,
            "selected_existing_note_ids": list(database_existing),
            "selected_generated_card_ids": ["G1"],
        },
    )
    assert issued.status_code == 200
    document = issued.json()
    built = client.post(
        f"/api/anki/jobs/{job_id}/envelope",
        json={"review_revision": 0, "overflow_acknowledgement": document},
    )

    assert built.status_code == 201
    assert repository.validate_card_centric_envelope_acknowledgement(
        UUID(built.json()["envelope_id"])
    )


def test_api_requires_dashboard_auth_on_public_host(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            public_hostname="study.example.com",
            cloudflare_access_issuer="https://study.cloudflareaccess.com",
            cloudflare_access_audience="audience",
            cloudflare_access_allowed_email="connor@example.com",
        )
    )
    client = TestClient(app)

    response = client.get(
        "/api/anki/jobs",
        headers={"host": "study.example.com"},
    )

    assert response.status_code == 401
    client.close()
    app.state.database.close()


def test_anki_bootstrap_exposes_grouped_current_sources_and_editable_tag(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app

    response = client.get("/api/anki/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    lecture = next(item for item in payload["lectures"] if item["id"] == lecture_id)
    assert lecture["topic"] == 'Anemia "I"'
    assert lecture["revisions"] == [
        {
            "id": revision_id,
            "kind": "slides",
            "source_sha256": SHA,
        },
        {
            "id": revision_id + 1,
            "kind": "transcripts",
            "source_sha256": "c" * 64,
        },
    ]
    assert lecture["source_ready"] is True
    assert lecture["source_status"] == {
        "slides": True,
        "transcripts": True,
        "summary": True,
    }
    assert lecture["target_deck"].startswith("OMS-II_Custom_Cards::")
    assert response.json()["indexed_decks"] == [
        "AnKing Step Deck",
        "Sketchy Pepper",
    ]
    assert response.json()["prompt_catalog"]["ready"] is True
    assert lecture["outline"]["kind"] == "summary"
    assert lecture["target_tag"] == (
        "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"
    )
    assert payload["lecture_groups"] == [
        {
            "course": "Heme Lymph",
            "exams": [
                {
                    "exam_number": 1,
                    "lectures": [lecture],
                }
            ],
        }
    ]


def test_anki_bootstrap_uses_saved_anki_curation_assignment_and_models(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, _, _, _ = prepared_app
    app.state.llm_settings.set_model(
        ProviderName.ANTHROPIC,
        "claude-sonnet-4-6",
    )
    app.state.llm_settings.set_assignment(
        LLMTask.ANKI_CURATION,
        ProviderName.ANTHROPIC,
        "claude-sonnet-4-6",
    )

    response = client.get("/api/anki/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert payload["defaults"]["provider"] == "anthropic"
    assert payload["defaults"]["model"] == "claude-sonnet-4-6"
    assert payload["defaults"]["lcl_prompt_version"] == ("lecture-concept-ledger")
    assert payload["defaults"]["judgment_rubric_version"] == ("coverage-rubric")
    assert payload["defaults"]["gap_prompt_version"] == ("gap-card-generation")
    assert payload["provider_models"] == {
        "anthropic": "claude-sonnet-4-6",
        "gemini": "gemini-3.6-flash",
        "openai": "gpt-5.2",
        "openrouter": "openai/gpt-4o-mini",
    }


def test_anki_page_embeds_quote_safe_lecture_json(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app

    response = client.get("/anki")

    assert response.status_code == 200
    document = HTMLParser(response.text)
    payload_node = document.css_first("#anki-lecture-data")
    assert payload_node is not None
    lectures = json.loads(payload_node.text())
    lecture = next(item for item in lectures if item["id"] == lecture_id)
    assert lecture["topic"] == 'Anemia "I"'
    assert lecture["revisions"][0]["id"] == revision_id
    assert document.css_first("[data-revisions]") is None


def test_anki_page_renders_dependent_course_exam_lecture_selects(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, _, _, _ = prepared_app

    response = client.get("/anki")

    assert response.status_code == 200
    document = HTMLParser(response.text)
    course = document.css_first('select[name="course"]')
    exam = document.css_first('select[name="exam_number"]')
    lecture = document.css_first('select[name="lecture_path_id"]')
    assert course is not None
    assert exam is not None
    assert lecture is not None
    assert "disabled" not in course.attributes
    assert "disabled" in exam.attributes
    assert "disabled" in lecture.attributes
    assert document.css_first(".anki-course-group") is None
    assert document.css_first(".anki-exam-group") is None


def test_anki_page_renders_openrouter_provider_option(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, _, _, _ = prepared_app

    response = client.get("/anki")

    assert response.status_code == 200
    document = HTMLParser(response.text)
    provider_select = document.css_first('select[name="provider"]')
    assert provider_select is not None
    values = {option.attributes.get("value") for option in provider_select.css("option")}
    assert values == {"anthropic", "openai", "gemini", "openrouter"}


def test_anki_page_hides_private_fixture_action_when_artifact_is_unavailable(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, _, _, _ = prepared_app

    response = client.get("/anki")

    assert response.status_code == 200
    document = HTMLParser(response.text)
    assert document.css_first("[data-run-fixture]") is None
    unavailable = document.css_first("[data-fixture-unavailable]")
    assert unavailable is not None
    assert "not installed and SHA-256 pinned" in unavailable.text()


def test_anki_page_shows_fixture_action_for_a_valid_pinned_external_artifact(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
    tmp_path: Path,
) -> None:
    client, app, _, _, _ = prepared_app
    path, pin = _pinned_private_fixture(tmp_path)
    app.state.settings.anki_fixture_artifact_path = path
    app.state.settings.anki_card_centric_fixture_sha256 = pin

    response = client.get("/anki")

    assert response.status_code == 200
    document = HTMLParser(response.text)
    assert document.css_first("[data-run-fixture]") is not None
    assert document.css_first("[data-fixture-unavailable]") is None


def test_anki_bootstrap_selects_openrouter_default_when_assigned(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, _, _, _ = prepared_app
    app.state.llm_settings.set_assignment(
        LLMTask.ANKI_CURATION,
        ProviderName.OPENROUTER,
        "openai/gpt-4o-mini",
    )

    response = client.get("/anki")

    assert response.status_code == 200
    document = HTMLParser(response.text)
    provider_select = document.css_first('select[name="provider"]')
    assert provider_select is not None
    selected = provider_select.css_first("option[selected]")
    assert selected is not None
    assert selected.attributes.get("value") == "openrouter"


def test_create_and_list_job_pins_server_generations_and_rejects_amboss(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, _ = prepared_app
    payload = _create_payload(lecture_id, revision_id)

    rejected = client.post(
        "/api/anki/jobs",
        json={**payload, "amboss_input": "must not be accepted"},
    )
    created = client.post("/api/anki/jobs", json=payload)
    listed = client.get("/api/anki/jobs")

    assert rejected.status_code == 422
    assert created.status_code == 201
    ingestion = IngestionRepository(app.state.database)
    revision = ingestion.get_study_revision(revision_id)
    transcript = ingestion.get_study_revision(revision_id + 1)
    assert created.json()["source_revision_hashes"] == {
        str(revision_id): revision_fingerprint(revision),
        str(revision_id + 1): revision_fingerprint(transcript),
    }
    assert created.json()["summary_outline_id"] is not None
    assert len(created.json()["summary_outline_sha256"]) == 64
    assert created.json()["companion_generation"] == "snapshot-test"
    assert created.json()["semantic_generation"] == "33a3b975-0e93-41e6-8a44-ec255c7e1269"
    assert listed.json()["jobs"][0]["id"] == created.json()["id"]


def test_create_job_pins_explicit_model(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app
    payload = _create_payload(lecture_id, revision_id)
    payload["model"] = "claude-sonnet-4-6"

    created = client.post("/api/anki/jobs", json=payload)

    assert created.status_code == 201
    assert created.json()["model"] == "claude-sonnet-4-6"


def test_create_job_accepts_openrouter_provider(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app
    payload = _create_payload(lecture_id, revision_id)
    payload["provider"] = "openrouter"
    payload["model"] = "openai/gpt-4o-mini"

    created = client.post("/api/anki/jobs", json=payload)

    assert created.status_code == 201
    assert created.json()["provider"] == "openrouter"
    assert created.json()["model"] == "openai/gpt-4o-mini"


def test_create_job_without_model_uses_anki_curation_assignment_default(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, _ = prepared_app
    app.state.llm_settings.set_model(
        ProviderName.ANTHROPIC,
        "claude-sonnet-4-6",
    )
    app.state.llm_settings.set_assignment(
        LLMTask.ANKI_CURATION,
        ProviderName.ANTHROPIC,
        "claude-sonnet-4-6",
    )
    payload = _create_payload(lecture_id, revision_id)
    del payload["model"]

    created = client.post("/api/anki/jobs", json=payload)

    assert created.status_code == 201
    assert created.json()["model"] == "claude-sonnet-4-6"


def test_create_job_rejects_blank_model(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app
    payload = _create_payload(lecture_id, revision_id)
    payload["model"] = "   "

    response = client.post("/api/anki/jobs", json=payload)

    assert response.status_code == 422


def test_create_job_rejects_oversized_model(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app
    payload = _create_payload(lecture_id, revision_id)
    payload["model"] = "x" * 201

    response = client.post("/api/anki/jobs", json=payload)

    assert response.status_code == 422


def test_create_v2_job_rejects_a_redirected_fast_classifier_destination(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app
    payload = _create_payload(lecture_id, revision_id)
    config = ResolvedModelConfiguration.card_centric_v2_default(
        "anthropic", "claude-sonnet-5"
    ).canonical_document()
    config["fast_classify_s4b"] = {
        **config["fast_classify_s4b"],
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
    }

    response = client.post(
        "/api/anki/jobs",
        json={
            **payload,
            "pipeline_contract_version": "card_centric_v2",
            "resolved_model_config": config,
        },
    )

    assert response.status_code == 422
    assert "S4b must use openai gpt-4o-mini" in response.json()["detail"]


def test_create_v2_job_accepts_the_fixed_default_fast_classifier_destination(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app

    response = client.post(
        "/api/anki/jobs",
        json={
            **_create_payload(lecture_id, revision_id),
            "pipeline_contract_version": "card_centric_v2",
        },
    )

    assert response.status_code == 201
    assert response.json()["state"] == "queued"


def test_create_job_requires_complete_three_source_bundle(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app
    payload = _create_payload(lecture_id, revision_id)
    payload["source_revision_ids"] = [revision_id]

    response = client.post("/api/anki/jobs", json=payload)

    assert response.status_code == 409
    assert "slides, transcript" in response.json()["detail"]


def test_create_job_rejects_malformed_notebook_summary_before_queueing(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
    tmp_path: Path,
) -> None:
    client, app, lecture_id, revision_id, _ = prepared_app
    payload = OutlinePdfRenderer().render(
        "Wrong Outline",
        "# CORE CONCEPTS\n- Ferritin falls early",
    )
    path = tmp_path / "malformed-outline.pdf"
    path.write_bytes(payload)
    generation = GenerationRepository(app.state.database)
    job = generation.current_job(lecture_id, GenerationKind.OUTLINE)
    assert job is not None
    generation.record_outline(
        lecture_id,
        job.id,
        path,
        hashlib.sha256(payload).hexdigest(),
    )

    response = client.post(
        "/api/anki/jobs",
        json=_create_payload(lecture_id, revision_id),
    )

    assert response.status_code == 409
    assert response.json()["detail"].startswith("summary_malformed:")


def test_failed_curation_job_can_be_retried_through_api(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, _ = prepared_app
    repository: AnkiCurationRepository = app.state.anki_repository
    created = repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id="heme-block-1",
            source_revision_ids=(revision_id,),
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=("#AK_Step2_v12::Hematology",),
            instruction_text="",
            target_deck="OMS::Heme::Lecture 4",
            target_tag=TARGET_TAG,
            index_snapshot_id="snapshot-test",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet-5",
        )
    )
    claimed = repository.claim_next_job(
        datetime.now(UTC),
        worker_id="worker-1",
        lease_seconds=30,
    )
    assert claimed is not None
    repository.start_stage(
        created.id,
        CurationStage.PREFLIGHT,
        expected_state=CurationState.PREFLIGHT,
        lease_owner="worker-1",
        now=datetime.now(UTC),
    )
    repository.fail_stage(
        created.id,
        CurationStage.PREFLIGHT,
        "malformed output",
        expected_state=CurationState.PREFLIGHT,
        lease_owner="worker-1",
    )
    repository.fail_job(
        created.id,
        "worker-1",
        "malformed output",
        expected_state=CurationState.PREFLIGHT,
        now=datetime.now(UTC),
    )

    response = client.post(f"/api/anki/jobs/{created.id}/retry")

    assert response.status_code == 200
    assert response.json()["state"] == "preflight"
    assert response.json()["error"] is None


def test_blank_card_scope_retry_repairs_legacy_job_through_api(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, _ = prepared_app
    repository: AnkiCurationRepository = app.state.anki_repository
    created = repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id="heme-block-1",
            source_revision_ids=(revision_id,),
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=(),
            instruction_text="",
            target_deck="OMS::Heme::Lecture 4",
            target_tag=TARGET_TAG,
            index_snapshot_id="snapshot-test",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet-5",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        )
    )
    assert created.tag_allowlist == ("heme",)
    repository.start_stage(created.id, CurationStage.CARD_TAG_SCOPE)
    repository.fail_stage(
        created.id,
        CurationStage.CARD_TAG_SCOPE,
        "tag scope has no resolved tokens",
        expected_state=CurationState.QUEUED,
        lease_owner=None,
    )
    with app.state.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(created.id))
        assert stored is not None
        stored.state = CurationState.FAILED.value
        stored.error = "tag scope has no resolved tokens"
        stored.tag_allowlist_json = "[]"

    response = client.post(f"/api/anki/jobs/{created.id}/retry")

    assert response.status_code == 200
    assert response.json()["state"] == "building_source_index"
    assert response.json()["tag_allowlist"] == ["heme"]
    assert response.json()["error"] is None


def test_failed_curation_job_can_be_removed_through_api(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, _ = prepared_app
    repository: AnkiCurationRepository = app.state.anki_repository
    created = repository.create_job(
        CreateCurationJob(
            lecture_id=lecture_id,
            block_id="heme-block-1",
            source_revision_ids=(revision_id,),
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=("#AK_Step2_v12::Hematology",),
            instruction_text="",
            target_deck="OMS::Heme::Lecture 4",
            target_tag=TARGET_TAG,
            index_snapshot_id="snapshot-test",
            lcl_prompt_version="lcl-v1",
            judgment_rubric_version="judgment-v1",
            gap_prompt_version="gap-v1",
            provider="anthropic",
            model="claude-sonnet-5",
        )
    )
    claimed = repository.claim_next_job(
        datetime.now(UTC),
        worker_id="worker-1",
        lease_seconds=30,
    )
    assert claimed is not None
    repository.start_stage(
        created.id,
        CurationStage.PREFLIGHT,
        expected_state=CurationState.PREFLIGHT,
        lease_owner="worker-1",
        now=datetime.now(UTC),
    )
    repository.fail_stage(
        created.id,
        CurationStage.PREFLIGHT,
        "malformed output",
        expected_state=CurationState.PREFLIGHT,
        lease_owner="worker-1",
    )
    repository.fail_job(
        created.id,
        "worker-1",
        "malformed output",
        expected_state=CurationState.PREFLIGHT,
        now=datetime.now(UTC),
    )

    page = client.get("/anki")
    document = HTMLParser(page.text)
    row = document.css_first(f'[data-job-id="{created.id}"]')
    assert row is not None
    retry = row.css_first("[data-retry-queued-job]")
    remove = row.css_first("[data-remove-failed-job]")
    assert retry is not None
    assert retry.attributes["aria-label"] == "Retry failed run"
    assert remove is not None
    assert remove.attributes["aria-label"] == "Remove failed run"

    removed = client.post(f"/api/anki/jobs/{created.id}/remove")
    listed = client.get("/api/anki/jobs")

    assert removed.status_code == 200
    assert removed.json() == {"job_id": str(created.id), "removed": True}
    assert listed.json()["jobs"] == []


def test_remove_job_api_rejects_nonfailed_runs(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, _, lecture_id, revision_id, _ = prepared_app
    created = client.post(
        "/api/anki/jobs",
        json=_create_payload(lecture_id, revision_id),
    )

    response = client.post(f"/api/anki/jobs/{created.json()['id']}/remove")

    assert response.status_code == 409
    assert "failed" in response.json()["detail"]


def test_create_job_blocks_when_semantic_alignment_is_below_threshold(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, _ = prepared_app
    app.state.anki_companion_index.semantic_coverage = 0.99

    response = client.post(
        "/api/anki/jobs",
        json=_create_payload(lecture_id, revision_id),
    )

    assert response.status_code == 409
    assert "99.000%" in response.json()["detail"]
    assert "refresh" in response.json()["detail"].casefold()


def test_review_groups_evidence_and_uses_optimistic_revision(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, _ = prepared_app
    job_id = _ready_job(app, lecture_id, revision_id)

    review = client.get(f"/api/anki/jobs/{job_id}/review")
    saved = client.put(
        f"/api/anki/jobs/{job_id}/review",
        json={
            "contract_version": 1,
            "expected_revision": 0,
            "reviewer": "local-user",
            "candidate_selections": {"42": True},
            "gap_edits": [],
            "tag_patches": [],
        },
    )
    stale = client.put(
        f"/api/anki/jobs/{job_id}/review",
        json={
            "contract_version": 1,
            "expected_revision": 0,
            "candidate_selections": {"42": False},
            "gap_edits": [],
            "tag_patches": [],
        },
    )
    evidence = client.get(f"/api/anki/jobs/{job_id}/evidence/slide-12")

    assert review.status_code == 200
    assert len(review.json()["groups"]["pass_1_matches"]) == 1
    assert len(review.json()["groups"]["generated_cards"]) == 1
    assert review.json()["groups"]["generated_cards"][0]["card_id"]
    assert saved.json()["revision"] == 1
    assert stale.status_code == 409
    assert evidence.json()["source_refs"][0]["locator"] == "slide 12"


def test_review_keeps_convergence_candidates_visible_with_recovered_matches(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, gateway = prepared_app
    job_id = _ready_job(app, lecture_id, revision_id)
    repository: AnkiCurationRepository = app.state.anki_repository
    first = repository.list_candidates(job_id)[0]
    convergence = replace(
        first,
        note_id=43,
        content_hash="4" * 64,
        retrieval_pass=RetrievalPass.CONVERGENCE,
    )
    repository.replace_candidates(job_id, (first, convergence))
    gateway.notes[43] = {
        **gateway.notes[42],
        "noteId": 43,
        "fields": {
            "Text": {
                "value": "Iron deficiency lowers transferrin saturation.",
                "order": 0,
            },
            "Extra": {
                "value": "Recovered during convergence.",
                "order": 1,
            },
        },
        "cards": [1_043],
    }

    review = client.get(f"/api/anki/jobs/{job_id}/review")

    assert review.status_code == 200
    recovered = review.json()["groups"]["recovered_in_pass_2"]
    assert [candidate["note_id"] for candidate in recovered] == [43]


def test_review_convergence_summary_exposes_manual_review_warning() -> None:
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    artifact = StageArtifact(
        artifact_id="convergence_pass_5:" + "a" * 64,
        stage=CurationStage.CONVERGENCE_PASS_5,
        kind="convergence_pass_5",
        relative_path="job/convergence.json",
        input_sha256="b" * 64,
        content_sha256="c" * 64,
    )
    repository = SimpleNamespace(
        list_stage_artifacts=lambda requested: [artifact] if requested == job_id else []
    )
    payload = {
        "pass_number": 5,
        "concepts": [
            {
                "concept_id": "C01",
                "passes_run": 3,
                "seen_note_ids": [1, 2],
                "growth": [1.0, 0.5, 0.0],
                "converged": True,
            },
            {
                "concept_id": "C02",
                "passes_run": 5,
                "seen_note_ids": [3, 4, 5],
                "growth": [1.0, 0.5, 0.4, 0.3, 0.2],
                "converged": False,
            },
        ],
        "needs_manual_review": True,
        "manual_review_concept_ids": ["C02"],
    }
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                anki_repository=repository,
                anki_curation_pipeline=SimpleNamespace(
                    artifacts=SimpleNamespace(read=lambda item: payload)
                ),
            )
        )
    )

    summary = _convergence_summary(request, job_id)

    assert summary == {
        "passes_run": 5,
        "concepts_converged": 1,
        "concepts_total": 2,
        "needs_manual_review": True,
        "manual_review_concept_ids": ["C02"],
    }


def test_review_reads_committed_reconciliation_findings() -> None:
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    artifact = StageArtifact(
        artifact_id="reconciliation:" + "a" * 64,
        stage=CurationStage.RECONCILIATION,
        kind="reconciliation_report_v2",
        relative_path="job/reconciliation.json",
        input_sha256="b" * 64,
        content_sha256="c" * 64,
    )
    payload = {
        "schema_name": "reconciliation_v2",
        "passed": ["A1", "A2"],
        "failed": [],
        "warned": [{"assertion_id": "A9", "message": "Some passages are uncited"}],
        "can_render_envelope": True,
        "snapshot": {"source_passage_ids": ["SLD:07:0001"]},
    }
    repository = SimpleNamespace(
        list_stage_artifacts=lambda requested: [artifact] if requested == job_id else []
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                anki_repository=repository,
                anki_curation_pipeline=SimpleNamespace(
                    artifacts=SimpleNamespace(read=lambda item: payload)
                ),
            )
        )
    )

    summary = _reconciliation_summary(request, job_id)

    assert summary == payload


def test_v2_envelope_requires_committed_reconciliation_report(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, _ = prepared_app
    job_id = _ready_job(app, lecture_id, revision_id)
    with app.state.database.session() as session:
        stored = session.get(AnkiCurationJobModel, str(job_id))
        assert stored is not None
        stored.gap_prompt_version = "gap-card-generation"

    response = client.post(
        f"/api/anki/jobs/{job_id}/envelope",
        json={"contract_version": 1, "review_revision": 0},
    )

    assert response.status_code == 409
    assert "reconciliation" in response.json()["detail"].casefold()


def test_review_reconciliation_rejects_deselected_generated_fact() -> None:
    reconciliation = {
        "schema_name": "reconciliation_v2",
        "passed": [f"A{number}" for number in range(1, 12)],
        "failed": [],
        "warned": [],
        "can_render_envelope": True,
        "snapshot": {
            "concepts": [
                {
                    "concept_id": "C01",
                    "missing_fact_ids": ["C01-M1"],
                    "status": "covered",
                    "converged": True,
                    "cited_passage_ids": ["SLD:01:0001"],
                }
            ],
            "generated_cards": [
                {
                    "card_id": "card-1",
                    "fact_id": "C01-M1",
                    "text": "The answer is {{c1::one}}.",
                }
            ],
            "unresolved_fact_ids": [],
            "expected_audit_nids": [],
            "audit_verdicts": [],
            "source_passage_ids": ["SLD:01:0001"],
            "forbidden_cloze_targets": [],
            "prompt_sync_stale": False,
        },
    }
    cards = [
        GapCard(
            card_id="card-1",
            concept_id="C01",
            text="The answer is {{c1::one}}.",
            extra="",
            selected=False,
            provenance={"fact_id": "C01-M1"},
        )
    ]

    refreshed = _review_reconciliation_summary(reconciliation, cards)

    assert refreshed["can_render_envelope"] is False
    assert {item["assertion_id"] for item in refreshed["failed"]} >= {
        "A1",
        "A2",
    }


def test_review_rejects_protected_tag_changes(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, gateway = prepared_app
    job_id = _ready_job(app, lecture_id, revision_id)
    before = gateway.notes[42]["tags"]

    response = client.put(
        f"/api/anki/jobs/{job_id}/review",
        json={
            "contract_version": 1,
            "expected_revision": 0,
            "candidate_selections": {},
            "gap_edits": [],
            "tag_patches": [
                {
                    "contract_version": 1,
                    "note_id": 42,
                    "before": before,
                    "after": ["OMS::Old"],
                    "add_tags": [],
                    "remove_tags": ["#Pathoma::Hematology"],
                    "expected_tag_hash": tag_hash(before),
                    "tag_policy_version": "tags-v1",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert "source_managed" in response.json()["detail"]


def test_saved_tag_edit_reloads_as_reviewed_diff_against_live_tags(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, gateway = prepared_app
    job_id = _ready_job(app, lecture_id, revision_id)
    before = gateway.notes[42]["tags"]
    after = ["#Pathoma::Hematology", "OMS::Reviewed"]

    saved = client.put(
        f"/api/anki/jobs/{job_id}/review",
        json={
            "contract_version": 1,
            "expected_revision": 0,
            "candidate_selections": {"42": True},
            "gap_edits": [],
            "tag_patches": [
                {
                    "contract_version": 1,
                    "note_id": 42,
                    "before": before,
                    "after": after,
                    "add_tags": ["OMS::Reviewed"],
                    "remove_tags": ["OMS::Old"],
                    "expected_tag_hash": tag_hash(before),
                    "tag_policy_version": "tags-v1",
                }
            ],
        },
    )
    review = client.get(f"/api/anki/jobs/{job_id}/review")
    note = review.json()["groups"]["pass_1_matches"][0]["note"]

    assert saved.status_code == 200
    assert [tag["value"] for tag in note["tags"]] == after
    assert note["current_tags"] == before
    assert note["tag_hash"] == tag_hash(before)


def test_apply_requires_confirmation_and_reports_counts(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, gateway = prepared_app
    job_id = _ready_job(app, lecture_id, revision_id)

    envelope = client.post(
        f"/api/anki/jobs/{job_id}/envelope",
        json={"contract_version": 1, "review_revision": 0},
    )
    rejected = client.post(
        f"/api/anki/jobs/{job_id}/apply",
        json={
            "contract_version": 1,
            "review_revision": 0,
            "confirmation": "yes",
        },
    )
    applied = client.post(
        f"/api/anki/jobs/{job_id}/apply",
        json={
            "contract_version": 1,
            "review_revision": 0,
            "confirmation": "APPLY TO ANKI",
        },
    )

    assert envelope.status_code == 201
    assert envelope.json()["summary"] == {
        "notes_created": 1,
        "existing_notes_retagged": 1,
        "tags_added": 1,
        "tags_removed": 0,
    }
    assert rejected.status_code == 422
    assert applied.json()["apply_state"] == "complete"
    assert applied.json()["recovery"]["kind"] == "complete"
    assert len(gateway.created_note_ids) == 1


def test_retry_sync_reuses_envelope_without_duplicate_generated_notes(
    prepared_app: tuple[TestClient, Any, int, int, FakeGateway],
) -> None:
    client, app, lecture_id, revision_id, gateway = prepared_app
    job_id = _ready_job(app, lecture_id, revision_id)
    client.post(
        f"/api/anki/jobs/{job_id}/envelope",
        json={"contract_version": 1, "review_revision": 0},
    )
    gateway.fail_sync_calls.add(2)

    first = client.post(
        f"/api/anki/jobs/{job_id}/apply",
        json={
            "contract_version": 1,
            "review_revision": 0,
            "confirmation": "APPLY TO ANKI",
        },
    )
    gateway.fail_sync_calls.clear()
    retried = client.post(
        f"/api/anki/jobs/{job_id}/retry-sync",
        json={
            "contract_version": 1,
            "confirmation": "RETRY SYNC",
        },
    )

    assert first.json()["apply_state"] == "applied_local_sync_retryable"
    assert first.json()["recovery"]["kind"] == "retry_sync"
    assert retried.json()["apply_state"] == "complete"
    assert len(gateway.created_note_ids) == 1
