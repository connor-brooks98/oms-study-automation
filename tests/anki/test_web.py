from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from oms_hub.anki.apply import ApplyCoordinator
from oms_hub.anki.domain import (
    Candidate,
    CreateCurationJob,
    CurationState,
    EvidenceSupport,
    GapCard,
    RetrievalPass,
    SourceEvidence,
    SourceKind,
    SourceReference,
)
from oms_hub.anki.models import AnkiCurationJobModel
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.runtime import AnkiPreflight
from oms_hub.anki.stages import revision_fingerprint
from oms_hub.anki.tag_policy import TagPolicy, tag_hash
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.models import LectureModel, StudyRevisionModel

SHA = "a" * 64
TARGET_TAG = "AnkiHub_Optional::LMU_OMS_II::Heme::Lecture_4"


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
    def snapshot_id(self) -> str:
        return "snapshot-test"

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
            manifest=SimpleNamespace(generation=UUID("33a3b975-0e93-41e6-8a44-ec255c7e1269"))
        )


@pytest.fixture
def prepared_app(tmp_path: Path) -> tuple[TestClient, Any, int, int, FakeGateway]:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    with app.state.database.session() as session:
        lecture = LectureModel(
            subject="Heme Lymph",
            exam_number=1,
            lecture_number=4,
            topic="Anemia I",
            lecturer="Professor",
        )
        session.add(lecture)
        session.flush()
        revision = StudyRevisionModel(
            upload_item_id="test-slides",
            lecture_id=lecture.id,
            kind="slides",
            source_sha256=SHA,
            immutable_source_path=str(tmp_path / "slides.pptx"),
            state="accepted",
            current=True,
        )
        session.add(revision)
        session.flush()
        lecture_id = lecture.id
        revision_id = revision.id

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
    client = TestClient(app)
    yield client, app, lecture_id, revision_id, gateway
    client.close()
    app.state.database.close()


def _create_payload(lecture_id: int, revision_id: int) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "lecture_id": lecture_id,
        "block_id": "heme-block-1",
        "source_revision_ids": [revision_id],
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


def _ready_job(app: Any, lecture_id: int, revision_id: int) -> UUID:
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
    revision = IngestionRepository(app.state.database).get_study_revision(revision_id)
    assert created.json()["source_revision_hashes"] == {
        str(revision_id): revision_fingerprint(revision)
    }
    assert created.json()["companion_generation"] == "snapshot-test"
    assert created.json()["semantic_generation"] == "33a3b975-0e93-41e6-8a44-ec255c7e1269"
    assert listed.json()["jobs"][0]["id"] == created.json()["id"]


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
    assert saved.json()["revision"] == 1
    assert stale.status_code == 409
    assert evidence.json()["source_refs"][0]["locator"] == "slide 12"


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
