import asyncio
from collections.abc import Sequence
from types import SimpleNamespace

from oms_hub.anki.domain import Candidate, CurationStage, RetrievalPass, SourceKind
from oms_hub.anki.judgment import JudgmentCacheRecord
from oms_hub.anki.lcl import LectureConcept, LectureConceptLedger, LedgerSourceRef
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import (
    CurationServicesRunner,
    _candidate_payload,
    _combined_support_ids,
    _final_judgment_stage,
    _passage_payload,
)
from oms_hub.anki.v2_contracts import (
    CoverageJudgmentV2,
    LectureConceptLedgerV2,
    LectureConceptV2,
    MissingFactV2,
    ParaphraseExpansionV2,
)
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


def _candidate(note_id: int, retrieval_pass: RetrievalPass) -> Candidate:
    return Candidate(
        note_id=note_id,
        content_hash=f"{note_id:064x}",
        best_concept_id="C01",
        provenance={"queries": [f"query {note_id}"]},
        scores={"boosted_score": 0.8},
        predicted_band="partial",
        verdict="include",
        confidence=0.7,
        reason="supports part of concept",
        context_trap=False,
        recall_direction="unknown",
        mnemonic_classification="unknown",
        dedupe_disposition="pending",
        selected=True,
        retrieval_pass=retrieval_pass,
    )


def _note(note_id: int) -> NormalizedNote:
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKingOverhaul",
        text=f"Iron deficiency card {note_id}.",
        extra="Lecture fact.",
        raw_fields={"Text": f"Iron deficiency card {note_id}."},
        tags=("#Pathoma",),
        card_ids=(100 + note_id,),
        media=(),
        token_signature=f"iron deficiency {note_id}",
        content_sha256=f"{note_id:064x}",
    )


def _fixture() -> tuple[SourcePassage, LectureConceptLedgerV2, MissingFactV2]:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin before microcytosis.",
        slide_number=3,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=1,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement=(
                    "Iron deficiency causes low ferritin before microcytosis."
                ),
                hypothetical_card=(
                    "Iron deficiency first causes {{c1::low ferritin}}."
                ),
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory sequence",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(passage.source_id,),
            ),
        ),
        intentionally_uncited=(),
    )
    missing = MissingFactV2(
        fact_id="C01-M1",
        statement="Low ferritin precedes microcytosis.",
        passage_ids=(passage.source_id,),
    )
    return passage, ledger, missing


class ConvergenceRepository:
    def __init__(self, candidates: Sequence[Candidate]) -> None:
        self.candidates = list(candidates)
        self.cache: dict[str, JudgmentCacheRecord] = {}

    def list_candidates(self, job_id: object) -> list[Candidate]:
        del job_id
        return list(self.candidates)

    def get_judgment_cache(
        self,
        cache_key: str,
    ) -> JudgmentCacheRecord | None:
        return self.cache.get(cache_key)

    def save_judgment_cache(self, record: JudgmentCacheRecord) -> None:
        self.cache.setdefault(record.cache_key, record)


class ConvergenceNotes:
    def __init__(self, notes: Sequence[NormalizedNote]) -> None:
        self.notes = {note.note_id: note for note in notes}

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.notes.get(note_id)


class ConvergenceRetrieval:
    def __init__(self, candidates: Sequence[Candidate]) -> None:
        self.candidates = list(candidates)
        self.calls: list[tuple[tuple[str, ...], int]] = []

    async def retrieve_convergence(
        self,
        concept: object,
        queries: Sequence[str],
        scope: object,
        *,
        pass_number: int,
    ) -> list[Candidate]:
        del concept, scope
        self.calls.append((tuple(queries), pass_number))
        return list(self.candidates)


class ConvergenceStructured:
    def __init__(
        self,
        expansion: ParaphraseExpansionV2,
        judgment: CoverageJudgmentV2,
    ) -> None:
        self.expansion = expansion
        self.judgment = judgment
        self.calls: list[type[object]] = []
        self.inputs: list[str] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[object],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[object]:
        del instruction
        self.calls.append(output_model)
        self.inputs.append(input_text)
        value = (
            self.expansion
            if output_model is ParaphraseExpansionV2
            else self.judgment
        )
        return StructuredJSONResult(
            value=value,
            raw_text=value.model_dump_json(),
            provider=provider,
            model=model,
            request_id=f"request-{len(self.calls)}",
            input_tokens=20,
            output_tokens=10,
            cost_microusd=5,
        )


def _context(
    *,
    convergence_payloads: dict[CurationStage, dict[str, object]] | None = None,
) -> SimpleNamespace:
    passage, ledger, missing = _fixture()
    first = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(missing,),
        rationale="The first card is incomplete.",
    )
    second = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(2,),
        missing_facts=(missing,),
        rationale="The rescue card is also incomplete.",
    )
    payloads: dict[CurationStage, dict[str, object]] = {
        CurationStage.PREFLIGHT: {
            "prompt_snapshot": [
                {
                    "id": "coverage-rubric",
                    "content": "# Coverage rubric V2",
                    "prompt_hash": "123456789abc",
                    "metadata": {"schema": "coverage_v2"},
                },
                {
                    "id": "paraphrase-expansion",
                    "content": "# Paraphrase expansion",
                    "prompt_hash": "abcdef123456",
                    "metadata": {"schema": "paraphrase_v2"},
                },
            ]
        },
        CurationStage.SOURCE_INDEX: {
            "passages": [_passage_payload(passage)]
        },
        CurationStage.LCL: {
            "ledger": ledger.model_dump(mode="json"),
            "schema_name": "lcl_v2",
        },
        CurationStage.RETRIEVAL_PASS_1: {
            "groups": {"C01": [_candidate_payload(_candidate(1, RetrievalPass.PASS_1))]}
        },
        CurationStage.JUDGMENT_PASS_1: {
            "schema_name": "coverage_v2",
            "judgments": {"C01": {"judgment": first.model_dump(mode="json")}},
        },
        CurationStage.RETRIEVAL_PASS_2: {
            "groups": {
                "C01": [
                    _candidate_payload(_candidate(2, RetrievalPass.PASS_2_RESCUE))
                ]
            }
        },
        CurationStage.JUDGMENT_PASS_2: {
            "schema_name": "coverage_v2",
            "judgments": {"C01": {"judgment": second.model_dump(mode="json")}},
        },
    }
    payloads.update(convergence_payloads or {})
    return SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            provider="openai",
            model="gpt-5.2",
            judgment_rubric_version="coverage-rubric",
            deck_allowlist=("AnKing Step Deck",),
            tag_allowlist=(),
            target_tag="OMS::Lecture",
            block_id=None,
        ),
        prior_payloads=payloads,
    )


def _runner(
    retrieved: Sequence[Candidate],
    judgment: CoverageJudgmentV2,
) -> tuple[CurationServicesRunner, ConvergenceStructured, ConvergenceRetrieval]:
    expansion = ParaphraseExpansionV2(
        concept_id="C01",
        paraphrases=(
            "iron deficiency transferrin saturation",
            "iron deficiency zinc protoporphyrin",
            "iron deficiency reticulocyte response",
        ),
        targeting="Residual laboratory sequence.",
    )
    structured = ConvergenceStructured(expansion, judgment)
    retrieval = ConvergenceRetrieval(retrieved)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    runner.retrieval = retrieval
    runner.repository = ConvergenceRepository(
        (
            _candidate(1, RetrievalPass.PASS_1),
            _candidate(2, RetrievalPass.PASS_2_RESCUE),
        )
    )
    runner.companion = ConvergenceNotes((_note(1), _note(2), _note(3)))
    return runner, structured, retrieval


def test_pass_three_expands_and_judges_when_growth_remains_high() -> None:
    passage, _, missing = _fixture()
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1, 2, 3),
        missing_facts=(missing,),
        rationale="A residual lecture fact is still missing.",
    )
    runner, structured, retrieval = _runner(
        (_candidate(3, RetrievalPass.CONVERGENCE),),
        judgment,
    )

    product = asyncio.run(
        runner._convergence_pass(_context(), pass_number=3)
    )

    concept = product.payload["concepts"][0]
    assert concept == {
        "concept_id": "C01",
        "passes_run": 3,
        "seen_note_ids": [1, 2, 3],
        "growth": [1.0, 0.5, 1 / 3],
        "converged": False,
    }
    assert product.payload["needs_manual_review"] is False
    assert product.payload["judgments"]["C01"]["judgment"] == (
        judgment.model_dump(mode="json")
    )
    assert retrieval.calls[0][1] == 3
    assert structured.calls == [ParaphraseExpansionV2, CoverageJudgmentV2]
    assert "Iron deficiency first causes" in structured.inputs[0]
    assert product.usage is not None
    assert product.usage.input_tokens == 40
    assert passage.source_id in product.payload["judgments"]["C01"]["judgment"]["missing_facts"][0]["passage_ids"]


def test_later_pass_skips_concepts_already_converged() -> None:
    _, _, missing = _fixture()
    runner, structured, retrieval = _runner(
        (),
        CoverageJudgmentV2(
            concept_id="C01",
            supporting_note_ids=(1, 2),
            missing_facts=(missing,),
            rationale="Unused response.",
        ),
    )
    converged = {
        "pass_number": 3,
        "concepts": [
            {
                "concept_id": "C01",
                "passes_run": 3,
                "seen_note_ids": [1, 2],
                "growth": [1.0, 0.5, 0.0],
                "converged": True,
            }
        ],
        "expanded_paraphrases": {"C01": []},
        "judgments": {},
    }

    product = asyncio.run(
        runner._convergence_pass(
            _context(
                convergence_payloads={
                    CurationStage.CONVERGENCE_PASS_3: converged
                }
            ),
            pass_number=4,
        )
    )

    assert product.payload["concepts"] == converged["concepts"]
    assert product.payload["active_concept_ids"] == []
    assert structured.calls == []
    assert retrieval.calls == []


def test_pass_four_stops_after_single_low_yield_convergence_pass() -> None:
    _, _, missing = _fixture()
    runner, structured, retrieval = _runner(
        (_candidate(3, RetrievalPass.CONVERGENCE),),
        CoverageJudgmentV2(
            concept_id="C01",
            supporting_note_ids=(1, 2, 3),
            missing_facts=(missing,),
            rationale="An additional search result would remain incomplete.",
        ),
    )
    nonconverged = {
        "pass_number": 3,
        "schema_name": "coverage_v2",
        "concepts": [
            {
                "concept_id": "C01",
                "passes_run": 3,
                "seen_note_ids": [1, 2],
                "growth": [1.0, 0.5, 0.5],
                "converged": False,
            }
        ],
        "expanded_paraphrases": {"C01": []},
        "judgments": {},
    }

    product = asyncio.run(
        runner._convergence_pass(
            _context(
                convergence_payloads={
                    CurationStage.CONVERGENCE_PASS_3: nonconverged
                }
            ),
            pass_number=4,
        )
    )

    assert product.payload["optimization_skipped"] is True
    assert product.payload["concepts"] == nonconverged["concepts"]
    assert structured.calls == []
    assert retrieval.calls == []


def test_stable_pass_three_does_not_require_an_expansion_prompt() -> None:
    _, _, missing = _fixture()
    runner, structured, retrieval = _runner(
        (),
        CoverageJudgmentV2(
            concept_id="C01",
            supporting_note_ids=(1, 2),
            missing_facts=(missing,),
            rationale="Unused response.",
        ),
    )
    context = _context()
    context.prior_payloads[CurationStage.RETRIEVAL_PASS_2] = {
        "groups": {"C01": []}
    }
    context.prior_payloads[CurationStage.PREFLIGHT]["prompt_snapshot"] = [
        context.prior_payloads[CurationStage.PREFLIGHT]["prompt_snapshot"][0]
    ]

    product = asyncio.run(
        runner._convergence_pass(context, pass_number=3)
    )

    assert product.payload["active_concept_ids"] == []
    assert structured.calls == []
    assert retrieval.calls == []


def test_first_pass_covered_concept_records_only_one_pass() -> None:
    runner, structured, retrieval = _runner(
        (),
        CoverageJudgmentV2(
            concept_id="C01",
            supporting_note_ids=(1,),
            missing_facts=(),
            rationale="Unused response.",
        ),
    )
    context = _context()
    covered = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(),
        rationale="The first-pass card covers the concept.",
    )
    context.prior_payloads[CurationStage.JUDGMENT_PASS_1]["judgments"] = {
        "C01": {"judgment": covered.model_dump(mode="json")}
    }
    context.prior_payloads[CurationStage.RETRIEVAL_PASS_2] = {"groups": {}}
    context.prior_payloads[CurationStage.JUDGMENT_PASS_2] = {
        "schema_name": "coverage_v2",
        "judgments": {},
    }
    context.prior_payloads[CurationStage.PREFLIGHT]["prompt_snapshot"] = [
        context.prior_payloads[CurationStage.PREFLIGHT]["prompt_snapshot"][0]
    ]

    product = asyncio.run(
        runner._convergence_pass(context, pass_number=3)
    )

    assert product.payload["concepts"][0] == {
        "concept_id": "C01",
        "passes_run": 1,
        "seen_note_ids": [1],
        "growth": [1.0],
        "converged": True,
    }
    assert structured.calls == []
    assert retrieval.calls == []


def test_legacy_ledger_keeps_two_pass_behavior_without_expansion_prompt() -> None:
    _, _, missing = _fixture()
    runner, structured, retrieval = _runner(
        (_candidate(3, RetrievalPass.CONVERGENCE),),
        CoverageJudgmentV2(
            concept_id="C01",
            supporting_note_ids=(1, 2, 3),
            missing_facts=(missing,),
            rationale="Unused response.",
        ),
    )
    context = _context()
    legacy = LectureConceptLedger(
        concepts=(
            LectureConcept(
                concept_id="C01",
                source_refs=(LedgerSourceRef(passage_id="1" * 64),),
                statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes low ferritin.",
                paraphrases=(
                    "iron deficiency ferritin",
                    "iron deficiency depleted stores",
                ),
                importance="core",
            ),
        )
    )
    context.prior_payloads[CurationStage.LCL] = {
        "ledger": legacy.model_dump(mode="json"),
        "schema_name": "lcl_v1",
    }
    context.prior_payloads[CurationStage.PREFLIGHT]["prompt_snapshot"] = [
        context.prior_payloads[CurationStage.PREFLIGHT]["prompt_snapshot"][0]
    ]

    product = asyncio.run(
        runner._convergence_pass(context, pass_number=3)
    )

    assert product.payload["compatibility_skipped_concept_ids"] == ["C01"]
    assert product.payload["concepts"][0]["converged"] is True
    assert structured.calls == []
    assert retrieval.calls == []


def test_pass_five_flags_nonconverged_concept_for_manual_review() -> None:
    _, _, missing = _fixture()
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1, 2, 3),
        missing_facts=(missing,),
        rationale="Coverage is still incomplete.",
    )
    runner, _, _ = _runner(
        (_candidate(3, RetrievalPass.CONVERGENCE),),
        judgment,
    )
    pass_four = {
        "pass_number": 4,
        "concepts": [
            {
                "concept_id": "C01",
                "passes_run": 4,
                "seen_note_ids": [1, 2],
                "growth": [1.0, 0.5, 0.5, 0.5],
                "converged": False,
            }
        ],
        "expanded_paraphrases": {
            "C01": [
                "iron deficiency prior query one",
                "iron deficiency prior query two",
                "iron deficiency prior query three",
            ]
        },
        "judgments": {},
    }

    product = asyncio.run(
        runner._convergence_pass(
            _context(
                convergence_payloads={
                    CurationStage.CONVERGENCE_PASS_4: pass_four
                }
            ),
            pass_number=5,
        )
    )

    assert product.payload["needs_manual_review"] is True
    assert product.payload["manual_review_concept_ids"] == ["C01"]


def test_downstream_coverage_uses_latest_convergence_judgment_and_supports() -> None:
    _, _, missing = _fixture()
    convergence = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1, 2, 3),
        missing_facts=(missing,),
        rationale="Convergence found an additional support.",
    )
    context = _context(
        convergence_payloads={
            CurationStage.CONVERGENCE_PASS_3: {
                "schema_name": "coverage_v2",
                "judgments": {
                    "C01": {
                        "judgment": convergence.model_dump(mode="json")
                    }
                },
            }
        }
    )

    assert _final_judgment_stage(context, "C01") is (
        CurationStage.CONVERGENCE_PASS_3
    )
    assert _combined_support_ids(context, "C01") == {1, 2, 3}
