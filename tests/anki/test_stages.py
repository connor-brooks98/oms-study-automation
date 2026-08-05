import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import oms_hub.anki.stages as stages_module
from oms_hub.anki.audit import AuditBatchV2, AuditCacheRecord
from oms_hub.anki.domain import (
    Candidate,
    CurationStage,
    GapCard,
    RetrievalPass,
    SourceKind,
)
from oms_hub.anki.gaps import GapBatchV2
from oms_hub.anki.judgment import JudgmentCacheRecord
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.anki.prompts import StaticPromptSynchronizer
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner, _priority_candidate_groups
from oms_hub.anki.v2_contracts import (
    AuditVerdictV2,
    CoverageJudgmentV2,
    GeneratedGapCardV2,
    LectureConceptLedgerV2,
    LectureConceptV2,
    MissingFactV2,
)
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


class ReadyRuntime:
    async def ensure_running(self) -> SimpleNamespace:
        return SimpleNamespace(
            reachable=True,
            ankiconnect_version=6,
            active_profile="Acceptance",
            collection_accessible=True,
            sync_available=True,
            blocking_reason=None,
        )


def test_priority_candidate_groups_preserve_deck_order() -> None:
    candidates = (
        Candidate(
            note_id=2,
            content_hash="2" * 64,
            best_concept_id="c1",
            provenance={"deck_priority": 1},
            scores={},
            predicted_band="unjudged",
            verdict="pending",
            confidence=0,
            reason="retrieved",
            context_trap=False,
            recall_direction="unknown",
            mnemonic_classification="unknown",
            dedupe_disposition="pending",
            selected=False,
        ),
        Candidate(
            note_id=1,
            content_hash="1" * 64,
            best_concept_id="c1",
            provenance={"deck_priority": 0},
            scores={},
            predicted_band="unjudged",
            verdict="pending",
            confidence=0,
            reason="retrieved",
            context_trap=False,
            recall_direction="unknown",
            mnemonic_classification="unknown",
            dedupe_disposition="pending",
            selected=False,
        ),
    )
    assert [group[0].note_id for group in _priority_candidate_groups(candidates)] == [
        1,
        2,
    ]


def test_bounded_map_preserves_order_and_limits_concurrency() -> None:
    async def scenario() -> None:
        active = 0
        maximum = 0

        async def operation(value: int) -> int:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.001 * (6 - value))
            active -= 1
            return value * 2

        results = await stages_module._bounded_map(
            tuple(range(6)),
            operation,
            limit=3,
        )

        assert results == (0, 2, 4, 6, 8, 10)
        assert maximum == 3

    asyncio.run(scenario())


def test_preflight_snapshots_all_prompts_for_the_job() -> None:
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.runtime = ReadyRuntime()
    runner.prompts = AnkiPromptCatalogService()
    runner.prompt_sync = StaticPromptSynchronizer()
    context = SimpleNamespace(
        job=SimpleNamespace(
            lcl_prompt_version="lecture-concept-ledger",
            judgment_rubric_version="coverage-rubric",
            gap_prompt_version="gap-card-generation",
        )
    )

    product = asyncio.run(runner._preflight(context))

    prompts = {
        item["id"]: item for item in product.payload["prompt_snapshot"]
    }
    assert set(prompts) == {
        "lecture-concept-ledger",
        "coverage-rubric",
        "card-relevance-audit",
        "gap-card-generation",
        "paraphrase-expansion",
    }
    assert all(len(item["prompt_hash"]) == 12 for item in prompts.values())
    assert all(item["content"] for item in prompts.values())
    assert product.payload["prompt_sync_stale"] is False


class V2StageStructuredService:
    def __init__(self, ledger: LectureConceptLedgerV2) -> None:
        self.ledger = ledger

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[LectureConceptLedgerV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[LectureConceptLedgerV2]:
        del instruction, input_text
        assert output_model is LectureConceptLedgerV2
        return StructuredJSONResult(
            value=self.ledger,
            raw_text=self.ledger.model_dump_json(),
            provider=provider,
            model=model,
            request_id="lcl-v2-request",
            input_tokens=40,
            output_tokens=20,
            cost_microusd=7,
        )


def test_lcl_stage_activates_schema_from_pinned_prompt_metadata() -> None:
    slide = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    transcript = SourcePassage.create(
        revision_id=8,
        lecture_id=12,
        artifact_id="transcript-8",
        source_kind=SourceKind.TRANSCRIPT,
        locator="transcript:1:12-24",
        text="Iron deficiency depletes iron stores.",
        start_seconds=12,
        end_seconds=24,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(slide.source_id, transcript.source_id),
            ),
        ),
        intentionally_uncited=(),
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = V2StageStructuredService(ledger)
    context = SimpleNamespace(
        job=SimpleNamespace(
            lcl_prompt_version="lecture-concept-ledger",
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "lecture-concept-ledger",
                        "content": "# V2 ledger prompt",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "lcl_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "passages": [
                    stages_module._passage_payload(slide),
                    stages_module._passage_payload(transcript),
                ]
            },
        },
    )

    product = asyncio.run(runner._lcl(context))

    assert product.payload["ledger"] == ledger.model_dump(mode="json")
    assert product.payload["prompt_hash"] == "123456789abc"
    assert product.payload["schema_name"] == "lcl_v2"


def test_downstream_ledger_reader_adapts_v2_artifact() -> None:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=True,
                importance="high",
                passage_ids=(passage.source_id,),
            ),
        ),
        intentionally_uncited=(),
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "passages": [stages_module._passage_payload(passage)]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
        }
    )

    runtime = stages_module._ledger(context)

    assert runtime.concepts[0].statement == (
        "Iron deficiency causes low ferritin."
    )
    assert runtime.concepts[0].source_refs[0].passage_id == passage.passage_id
    assert runtime.concepts[0].primary_entity == "iron deficiency"


class CoverageCache:
    def __init__(self) -> None:
        self.records: dict[str, JudgmentCacheRecord] = {}

    def get_judgment_cache(
        self,
        cache_key: str,
    ) -> JudgmentCacheRecord | None:
        return self.records.get(cache_key)

    def save_judgment_cache(self, record: JudgmentCacheRecord) -> None:
        self.records.setdefault(record.cache_key, record)


class CompanionNotes:
    def __init__(self, *notes: NormalizedNote) -> None:
        self.notes = {note.note_id: note for note in notes}

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.notes.get(note_id)


class V2CoverageStructuredService:
    def __init__(self, judgment: CoverageJudgmentV2) -> None:
        self.judgment = judgment

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[CoverageJudgmentV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[CoverageJudgmentV2]:
        del instruction, input_text
        assert output_model is CoverageJudgmentV2
        return StructuredJSONResult(
            value=self.judgment,
            raw_text=self.judgment.model_dump_json(),
            provider=provider,
            model=model,
            request_id="coverage-v2-request",
            input_tokens=30,
            output_tokens=15,
            cost_microusd=8,
        )


def test_judgment_stage_projects_only_supporting_candidates_for_audit() -> None:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(passage.source_id,),
            ),
        ),
        intentionally_uncited=(),
    )
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron stores fall before microcytosis.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="The note omits the laboratory sequence.",
    )
    note = NormalizedNote(
        note_id=1,
        model_name="AnKingOverhaul",
        text="Iron deficiency causes low ferritin.",
        extra="Ferritin reflects iron stores.",
        raw_fields={"Text": "Iron deficiency causes low ferritin."},
        tags=("#Pathoma",),
        card_ids=(101,),
        media=(),
        token_signature="iron deficiency ferritin",
        content_sha256="1" * 64,
    )
    candidate = Candidate(
        note_id=1,
        content_hash="1" * 64,
        best_concept_id="C01",
        provenance={},
        scores={"boosted_score": 0.9},
        predicted_band="unjudged",
        verdict="pending",
        confidence=0,
        reason="retrieved",
        context_trap=False,
        recall_direction="unknown",
        mnemonic_classification="unknown",
        dedupe_disposition="pending",
        selected=False,
        retrieval_pass=RetrievalPass.PASS_1,
    )
    non_supporting_candidate = replace(
        candidate,
        note_id=2,
        content_hash="2" * 64,
    )
    non_supporting_note = replace(
        note,
        note_id=2,
        content_sha256="2" * 64,
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = V2CoverageStructuredService(judgment)
    runner.repository = CoverageCache()
    runner.companion = CompanionNotes(note, non_supporting_note)
    context = SimpleNamespace(
        job=SimpleNamespace(
            judgment_rubric_version="coverage-rubric",
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "coverage-rubric",
                        "content": "# Coverage rubric V2",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "coverage_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "passages": [stages_module._passage_payload(passage)]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
            CurationStage.RETRIEVAL_PASS_1: {
                "groups": {
                    "C01": [
                        stages_module._candidate_payload(candidate),
                        stages_module._candidate_payload(non_supporting_candidate),
                    ]
                }
            },
        },
    )

    product = asyncio.run(runner._judgment_pass_1(context))

    assert product.payload["schema_name"] == "coverage_v2"
    assert product.payload["judgments"]["C01"]["judgment"] == (
        judgment.model_dump(mode="json")
    )
    assert product.candidates is not None
    assert [candidate.note_id for candidate in product.candidates] == [1]
    assert product.candidates[0].predicted_band == "partial"


def test_downstream_coverage_reader_adapts_v2_artifact() -> None:
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron stores fall before microcytosis.",
                passage_ids=("TRX:07:0198",),
            ),
        ),
        rationale="The note omits the laboratory sequence.",
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.JUDGMENT_PASS_1: {
                "schema_name": "coverage_v2",
                "judgments": {
                    "C01": {"judgment": judgment.model_dump(mode="json")}
                },
            }
        }
    )

    runtime = stages_module._coverage_judgment(
        context,
        CurationStage.JUDGMENT_PASS_1,
        "C01",
    )

    assert runtime.status == "partial"
    assert runtime.missing_fact_records[0].fact_id == "C01-M1"


class AuditRepository(CoverageCache):
    def __init__(self, candidate: Candidate) -> None:
        super().__init__()
        self.candidate = candidate
        self.audit_records: dict[str, AuditCacheRecord] = {}

    def list_candidates(self, job_id: object) -> list[Candidate]:
        del job_id
        return [self.candidate]

    def lecture_title(self, lecture_id: int) -> str:
        assert lecture_id == 12
        return "Heme Exam 1 Lecture 7: Anemia IV"

    def get_audit_cache(self, cache_key: str) -> AuditCacheRecord | None:
        return self.audit_records.get(cache_key)

    def save_audit_cache(self, record: AuditCacheRecord) -> None:
        self.audit_records.setdefault(record.cache_key, record)


class AuditStructuredService:
    def __init__(self, verdict: AuditVerdictV2) -> None:
        self.verdict = verdict

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[AuditBatchV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[AuditBatchV2]:
        del instruction, input_text
        assert output_model is AuditBatchV2
        batch = AuditBatchV2(verdicts=(self.verdict,))
        return StructuredJSONResult(
            value=batch,
            raw_text=batch.model_dump_json(),
            provider=provider,
            model=model,
            request_id="audit-request",
            input_tokens=100,
            output_tokens=20,
            cost_microusd=30,
        )


def _audit_stage_fixture() -> tuple[
    SourcePassage,
    LectureConceptLedgerV2,
    Candidate,
    NormalizedNote,
]:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(passage.source_id,),
            ),
        ),
        intentionally_uncited=(),
    )
    candidate = Candidate(
        note_id=1,
        content_hash="1" * 64,
        best_concept_id="C01",
        provenance={"query": "hidden retrieval reason"},
        scores={"boosted_score": 0.9},
        predicted_band="covered",
        verdict="include",
        confidence=1,
        reason="old coverage rationale",
        context_trap=False,
        recall_direction="unknown",
        mnemonic_classification="unknown",
        dedupe_disposition="pending",
        selected=True,
        retrieval_pass=RetrievalPass.PASS_1,
    )
    note = NormalizedNote(
        note_id=1,
        model_name="AnKingOverhaul",
        text="Hemophilia A is inherited in an X-linked recessive pattern.",
        extra="Factor VIII deficiency.",
        raw_fields={"Text": "Hemophilia A is X-linked recessive."},
        tags=("#Pathoma",),
        card_ids=(101,),
        media=(),
        token_signature="hemophilia x linked",
        content_sha256="1" * 64,
    )
    return passage, ledger, candidate, note


def test_card_audit_stage_replaces_coverage_selection_with_blind_verdict() -> None:
    passage, ledger, candidate, note = _audit_stage_fixture()
    verdict = AuditVerdictV2(
        nid=1,
        verdict="drop",
        primary_subject="hemophilia A",
        support="none",
        reason="Different disease sharing only an inheritance pattern",
        structure_issue=("context_trap",),
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = AuditStructuredService(verdict)
    runner.repository = AuditRepository(candidate)
    runner.companion = CompanionNotes(note)
    context = SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            lecture_id=12,
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "card-relevance-audit",
                        "content": "# Blind audit",
                        "prompt_hash": "123456789abc",
                        "metadata": {
                            "schema": "audit_verdict_v2",
                            "batch_size": 30,
                        },
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "passages": [stages_module._passage_payload(passage)]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
        },
    )

    product = asyncio.run(runner._card_audit(context))

    assert product.payload["verdicts"] == [verdict.model_dump(mode="json")]
    assert product.candidates is not None
    audited = product.candidates[0]
    assert audited.verdict == "drop"
    assert audited.selected is False
    assert audited.context_trap is True
    assert audited.provenance["audit"]["primary_subject"] == "hemophilia A"


class MissingCoverageStructuredService:
    def __init__(self, judgment: CoverageJudgmentV2) -> None:
        self.judgment = judgment
        self.calls = 0
        self.inputs: list[str] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[CoverageJudgmentV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[CoverageJudgmentV2]:
        del instruction
        assert output_model is CoverageJudgmentV2
        self.calls += 1
        self.inputs.append(input_text)
        return StructuredJSONResult(
            value=self.judgment,
            raw_text=self.judgment.model_dump_json(),
            provider=provider,
            model=model,
            request_id="recompute-request",
            input_tokens=25,
            output_tokens=15,
            cost_microusd=9,
        )


def test_coverage_recompute_creates_missing_fact_after_audit_drop() -> None:
    passage, ledger, candidate, note = _audit_stage_fixture()
    original = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(),
        rationale="The candidate appears to cover the concept.",
    )
    recomputed = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron deficiency causes low ferritin.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="No audited candidate covers this lecture fact.",
    )
    structured = MissingCoverageStructuredService(recomputed)
    audited_candidate = replace(candidate, verdict="drop", selected=False)
    repository = AuditRepository(audited_candidate)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    runner.repository = repository
    runner.companion = CompanionNotes(note)
    context = SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            judgment_rubric_version="coverage-rubric",
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "coverage-rubric",
                        "content": "# Coverage rubric V2",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "coverage_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "passages": [stages_module._passage_payload(passage)]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
            CurationStage.JUDGMENT_PASS_1: {
                "schema_name": "coverage_v2",
                "judgments": {
                    "C01": {"judgment": original.model_dump(mode="json")}
                },
            },
            CurationStage.JUDGMENT_PASS_2: {
                "schema_name": "coverage_v2",
                "judgments": {},
            },
            CurationStage.CARD_AUDIT: {
                "verdicts": [
                    AuditVerdictV2(
                        nid=1,
                        verdict="drop",
                        primary_subject="hemophilia A",
                        support="none",
                        reason="Different disease",
                        structure_issue=(),
                    ).model_dump(mode="json")
                ]
            },
        },
    )

    product = asyncio.run(runner._coverage_recompute(context))

    assert structured.calls == 1
    assert product.payload["schema_name"] == "coverage_v2"
    assert product.payload["judgments"]["C01"]["recomputed"] is True
    assert product.payload["judgments"]["C01"]["judgment"] == (
        recomputed.model_dump(mode="json")
    )


class MultipleCompanionNotes:
    def __init__(self, notes: tuple[NormalizedNote, ...]) -> None:
        self.notes = {note.note_id: note for note in notes}

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.notes.get(note_id)


class MultipleAuditRepository(CoverageCache):
    def __init__(self, candidates: tuple[Candidate, ...]) -> None:
        super().__init__()
        self.candidates = candidates

    def list_candidates(self, job_id: object) -> list[Candidate]:
        del job_id
        return list(self.candidates)


def test_coverage_recompute_combines_surviving_supports_from_both_passes() -> None:
    passage, ledger, first_candidate, first_note = _audit_stage_fixture()
    second_candidate = replace(
        first_candidate,
        note_id=2,
        content_hash="2" * 64,
        retrieval_pass=RetrievalPass.PASS_2_RESCUE,
    )
    second_note = replace(
        first_note,
        note_id=2,
        content_sha256="2" * 64,
        text="Iron deficiency depletes iron stores before microcytosis.",
    )
    first = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron stores fall before microcytosis.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="The first card covers ferritin but not the sequence.",
    )
    second = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(2,),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron deficiency causes low ferritin.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="The rescue card covers the sequence but not ferritin.",
    )
    combined = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1, 2),
        missing_facts=(),
        rationale="Together the audited cards cover the concept.",
    )
    structured = MissingCoverageStructuredService(combined)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    runner.repository = MultipleAuditRepository(
        (first_candidate, second_candidate)
    )
    runner.companion = MultipleCompanionNotes((first_note, second_note))
    context = SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            judgment_rubric_version="coverage-rubric",
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "coverage-rubric",
                        "content": "# Coverage rubric V2",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "coverage_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "passages": [stages_module._passage_payload(passage)]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
            CurationStage.JUDGMENT_PASS_1: {
                "schema_name": "coverage_v2",
                "judgments": {
                    "C01": {"judgment": first.model_dump(mode="json")}
                },
            },
            CurationStage.JUDGMENT_PASS_2: {
                "schema_name": "coverage_v2",
                "judgments": {
                    "C01": {"judgment": second.model_dump(mode="json")}
                },
            },
            CurationStage.CARD_AUDIT: {
                "verdicts": [
                    AuditVerdictV2(
                        nid=note_id,
                        verdict="keep",
                        primary_subject="iron deficiency",
                        support="slides",
                        reason="Directly supported by the lecture slide",
                        structure_issue=(),
                    ).model_dump(mode="json")
                    for note_id in (1, 2)
                ]
            },
        },
    )

    product = asyncio.run(runner._coverage_recompute(context))

    assert structured.calls == 1
    assert [
        candidate["note_id"]
        for candidate in json.loads(structured.inputs[0])["candidates"]
    ] == [1, 2]
    assert product.payload["judgments"]["C01"]["judgment"] == (
        combined.model_dump(mode="json")
    )


def test_audit_created_gap_localization_excludes_summary_only_evidence() -> None:
    slide = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    summary = SourcePassage.create(
        revision_id=9,
        lecture_id=12,
        artifact_id="summary-9",
        source_kind=SourceKind.SUMMARY,
        locator="summary:core:1",
        text="Iron deficiency causes low ferritin.",
        source_id="SUM:12:CORE:01",
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=1,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(slide.source_id, summary.source_id),
            ),
        ),
        intentionally_uncited=(),
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "passages": [
                    stages_module._passage_payload(slide),
                    stages_module._passage_payload(summary),
                ]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
        }
    )
    concept = stages_module._ledger(context).concepts[0]

    localization = stages_module._localization_from_concept(
        concept,
        (slide, summary),
    )

    assert localization.evidence == (slide,)


def test_v2_gap_request_retains_summary_cited_by_missing_fact() -> None:
    slide = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    summary = SourcePassage.create(
        revision_id=9,
        lecture_id=12,
        artifact_id="summary-9",
        source_kind=SourceKind.SUMMARY,
        locator="summary:depth:1",
        text="DEEP: iron deficiency causes low ferritin.",
        source_id="SUM:12:DEPTH:D1",
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=1,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(slide.source_id, summary.source_id),
            ),
        ),
        intentionally_uncited=(),
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "passages": [
                    stages_module._passage_payload(slide),
                    stages_module._passage_payload(summary),
                ]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
        }
    )
    concept = stages_module._ledger(context).concepts[0]
    missing_fact = MissingFactV2(
        fact_id="C01-M1",
        statement="Iron deficiency causes low ferritin.",
        passage_ids=(summary.source_id,),
    )

    evidence = stages_module._v2_gap_evidence(
        concept,
        (missing_fact,),
        (slide, summary),
    )

    assert evidence == (slide, summary)


def test_v2_gap_request_still_requires_primary_evidence() -> None:
    summary = SourcePassage.create(
        revision_id=9,
        lecture_id=12,
        artifact_id="summary-9",
        source_kind=SourceKind.SUMMARY,
        locator="summary:depth:1",
        text="DEEP: iron deficiency causes low ferritin.",
        source_id="SUM:12:DEPTH:D1",
    )
    concept = SimpleNamespace(source_passage_ids=(summary.source_id,))
    missing_fact = MissingFactV2(
        fact_id="C01-M1",
        statement="Iron deficiency causes low ferritin.",
        passage_ids=(summary.source_id,),
    )

    with pytest.raises(
        stages_module.PinnedInputChanged,
        match="no primary-source evidence",
    ):
        stages_module._v2_gap_evidence(
            concept,
            (missing_fact,),
            (summary,),
        )


class GapStageRepository:
    def list_candidates(self, job_id: object) -> list[Candidate]:
        del job_id
        return []

    def lecture_title(self, lecture_id: int) -> str:
        assert lecture_id == 12
        return "Iron Deficiency Anemia"

    def list_source_evidence(self, job_id: object) -> list[object]:
        del job_id
        return []


class V2GapStageStructuredService:
    def __init__(self, batch: GapBatchV2) -> None:
        self.batch = batch
        self.inputs: list[str] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[GapBatchV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[GapBatchV2]:
        del instruction
        assert output_model is GapBatchV2
        self.inputs.append(input_text)
        return StructuredJSONResult(
            value=self.batch,
            raw_text=self.batch.model_dump_json(),
            provider=provider,
            model=model,
            request_id="gap-v2-request",
            input_tokens=30,
            output_tokens=15,
            cost_microusd=8,
        )


def test_gap_stage_routes_on_audited_missing_facts_not_display_outcome() -> None:
    passage, ledger, _, _ = _audit_stage_fixture()
    ledger = ledger.model_copy(update={"lecture_entity_count": 1})
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron deficiency causes low ferritin.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="No audited card covers ferritin.",
    )
    generated = GeneratedGapCardV2(
        fact_id="C01-M1",
        status="generated",
        text="<b>Iron deficiency</b> causes {{c1::<b>low ferritin</b>}}.",
        extra="Ferritin reflects depleted iron stores.",
        note_type="AnKingOverhaul (AnKing Step Deck / AnKingMed)",
        source_passage_ids=(passage.source_id,),
        split=True,
        image_needed=None,
    )
    structured = V2GapStageStructuredService(
        GapBatchV2(
            resolutions=(
                generated,
                generated.model_copy(),
            )
        )
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    runner.repository = GapStageRepository()
    runner.companion = MultipleCompanionNotes(())
    runner.embedder = SimpleNamespace()
    context = SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            lecture_id=12,
            gap_prompt_version="gap-card-generation",
            provider="openai",
            model="gpt-5.6-terra",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "gap-card-generation",
                        "content": "# Gap generation V2",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "gap_cards_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "passages": [stages_module._passage_payload(passage)]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
            CurationStage.COVERAGE_RECOMPUTE: {
                "schema_name": "coverage_v2",
                "judgments": {
                    "C01": {"judgment": judgment.model_dump(mode="json")}
                },
            },
            CurationStage.DEDUPE: {
                "outcomes": {"C01": "covered_audited"}
            },
            CurationStage.RESCUE: {"localizations": {}},
        },
    )

    product = asyncio.run(runner._generate_gaps(context))

    assert len(structured.inputs) == 1
    sent = json.loads(structured.inputs[0])
    assert [fact["fact_id"] for fact in sent["missing_facts"]] == ["C01-M1"]
    assert sent["forbidden_cloze_targets"] == [
        "Iron Deficiency Anemia",
        "iron deficiency",
    ]
    assert product.gap_cards is not None
    assert len(product.gap_cards) == 1
    assert product.gap_cards[0].provenance["fact_id"] == "C01-M1"


class ReconciliationStageRepository:
    def __init__(self, cards: tuple[GapCard, ...]) -> None:
        self.cards = cards

    def list_candidates(self, job_id: object) -> list[Candidate]:
        del job_id
        return []

    def list_gap_cards(self, job_id: object) -> list[GapCard]:
        del job_id
        return list(self.cards)


def _reconciliation_context(
    *,
    prompt_sync_stale: bool,
) -> tuple[SimpleNamespace, SourcePassage]:
    passage, ledger, _, _ = _audit_stage_fixture()
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron deficiency causes low ferritin.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="No existing card covers ferritin.",
    )
    return (
        SimpleNamespace(
            job=SimpleNamespace(id="job-1"),
            prior_payloads={
                CurationStage.PREFLIGHT: {
                    "prompt_sync_stale": prompt_sync_stale,
                    "prompt_snapshot": [],
                },
                CurationStage.SOURCE_INDEX: {
                    "passages": [stages_module._passage_payload(passage)]
                },
                CurationStage.LCL: {
                    "ledger": ledger.model_dump(mode="json"),
                    "schema_name": "lcl_v2",
                },
                CurationStage.CONVERGENCE_PASS_5: {
                    "concepts": [
                        {
                            "concept_id": "C01",
                            "passes_run": 3,
                            "seen_note_ids": [],
                            "growth": [1.0, 0.1, 0.0],
                            "converged": True,
                        }
                    ]
                },
                CurationStage.CARD_AUDIT: {"verdicts": []},
                CurationStage.COVERAGE_RECOMPUTE: {
                    "schema_name": "coverage_v2",
                    "judgments": {
                        "C01": {
                            "judgment": judgment.model_dump(mode="json")
                        }
                    },
                },
                CurationStage.GAPS: {
                    "schema_name": "gap_cards_v2",
                    "unresolved": [],
                    "forbidden_cloze_targets": ["Iron Deficiency Anemia"],
                },
            },
        ),
        passage,
    )


def test_reconciliation_stage_allows_warning_only_report() -> None:
    context, _ = _reconciliation_context(prompt_sync_stale=True)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.repository = ReconciliationStageRepository(
        (
            GapCard(
                card_id="gap-1",
                concept_id="C01",
                text="<b>Iron deficiency</b> causes {{c1::<b>low ferritin</b>}}.",
                extra="Ferritin reflects depleted stores.",
                provenance={"fact_id": "C01-M1"},
            ),
        )
    )

    product = asyncio.run(runner._reconciliation(context))

    assert product.blocking_error is None
    assert product.payload["can_render_envelope"] is True
    assert [item["assertion_id"] for item in product.payload["warned"]] == [
        "A11"
    ]
    assert product.payload["metrics"] == {
        "audit_keep": 0,
        "audit_drop": 0,
        "audit_uncertain": 0,
        "audit_drop_rate": 0.0,
        "unresolved_concepts": 0,
        "uncited_passage_ids": [],
        "prompt_sync_stale": True,
    }
    assert product.payload["snapshot"]["generated_cards"][0]["fact_id"] == (
        "C01-M1"
    )


def test_reconciliation_stage_blocks_missing_fact_partition() -> None:
    context, _ = _reconciliation_context(prompt_sync_stale=False)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.repository = ReconciliationStageRepository(())

    product = asyncio.run(runner._reconciliation(context))

    assert product.payload["can_render_envelope"] is False
    assert {item["assertion_id"] for item in product.payload["failed"]} >= {
        "A1",
        "A2",
        "A4",
    }
    assert product.blocking_error == "Reconciliation failed: A1, A2, A4"
