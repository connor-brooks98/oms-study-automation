from collections.abc import Sequence

import pytest

from oms_hub.anki.domain import Candidate, RetrievalPass
from oms_hub.anki.judgment import (
    CoverageJudgment,
    JudgmentCacheRecord,
    JudgmentService,
    JudgmentValidationError,
)
from oms_hub.anki.lcl import (
    LectureConcept,
    LedgerSourceRef,
)
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


def _concept(statement: str = "Iron deficiency lowers ferritin") -> LectureConcept:
    return LectureConcept(
        concept_id="iron-deficiency",
        source_refs=(LedgerSourceRef(passage_id="a" * 64),),
        statement=statement,
        hypothetical_card="Ferritin is low in iron deficiency",
        paraphrases=(
            "Early marker of depleted iron stores",
            "Ferritin response to iron deficiency",
        ),
        importance="core",
    )


def _candidate(note_id: int, content_hash: str | None = None) -> Candidate:
    return Candidate(
        note_id=note_id,
        content_hash=content_hash or f"{note_id:064x}",
        best_concept_id="iron-deficiency",
        provenance={},
        scores={"boosted_score": 0.1},
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


def _note(note_id: int, content_hash: str | None = None) -> NormalizedNote:
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKingOverhaul",
        text="Iron deficiency causes low ferritin.",
        extra="Ferritin reflects iron stores.",
        raw_fields={"Text": "Iron deficiency causes low ferritin."},
        tags=("#Pathoma::Hematology",),
        card_ids=(note_id + 100,),
        media=(),
        token_signature="ferritin iron",
        content_sha256=content_hash or f"{note_id:064x}",
    )


def _result(
    judgment: CoverageJudgment,
) -> StructuredJSONResult[CoverageJudgment]:
    return StructuredJSONResult(
        value=judgment,
        raw_text=judgment.model_dump_json(),
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        request_id="request-1",
        input_tokens=20,
        output_tokens=10,
        cost_microusd=5,
    )


class QueueStructured:
    def __init__(
        self,
        judgments: Sequence[CoverageJudgment | Exception],
    ) -> None:
        self.judgments = list(judgments)
        self.calls = 0
        self.requests: list[tuple[str, str]] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[CoverageJudgment],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[CoverageJudgment]:
        self.calls += 1
        self.requests.append((instruction, input_text))
        value = self.judgments.pop(0)
        if isinstance(value, Exception):
            raise value
        return _result(value)


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, JudgmentCacheRecord] = {}

    def get_judgment_cache(
        self,
        cache_key: str,
    ) -> JudgmentCacheRecord | None:
        return self.values.get(cache_key)

    def save_judgment_cache(
        self,
        record: JudgmentCacheRecord,
    ) -> None:
        self.values.setdefault(record.cache_key, record)


class NoteReader:
    def __init__(self, notes: Sequence[NormalizedNote]) -> None:
        self.notes = {note.note_id: note for note in notes}

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.notes.get(note_id)


def _service(
    structured: QueueStructured,
    cache: MemoryCache,
    notes: Sequence[NormalizedNote],
) -> JudgmentService:
    return JudgmentService(
        structured,
        cache,
        NoteReader(notes),
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_version="judgment-v1",
    )


def test_valid_judgment_is_cached_and_reused() -> None:
    judgment = CoverageJudgment(
        status="covered",
        supporting_note_ids=(1,),
        missing_facts=(),
        rationale="The note directly covers low ferritin.",
    )
    structured = QueueStructured([judgment])
    cache = MemoryCache()
    service = _service(structured, cache, [_note(1)])

    first = service.judge(_concept(), [_candidate(1)])
    second = service.judge(_concept(), [_candidate(1)])

    assert first.judgment == judgment
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert structured.calls == 1


def test_changed_candidate_content_hash_invalidates_cache() -> None:
    judgment = CoverageJudgment(
        status="partial",
        supporting_note_ids=(1,),
        missing_facts=("Treatment response is absent.",),
        rationale="The retrieved note covers diagnosis only.",
    )
    structured = QueueStructured([judgment, judgment])
    cache = MemoryCache()
    first_hash = "1" * 64
    second_hash = "2" * 64
    service = _service(structured, cache, [_note(1, first_hash)])
    service.judge(_concept(), [_candidate(1, first_hash)])
    service.notes = NoteReader([_note(1, second_hash)])

    changed = service.judge(
        _concept(),
        [_candidate(1, second_hash)],
    )

    assert changed.cache_hit is False
    assert structured.calls == 2


def test_supporting_note_ids_must_be_supplied_candidates() -> None:
    invalid = CoverageJudgment(
        status="covered",
        supporting_note_ids=(999,),
        missing_facts=(),
        rationale="A different note covers it.",
    )
    structured = QueueStructured(
        [invalid, invalid]
    )
    service = _service(structured, MemoryCache(), [_note(1)])

    with pytest.raises(JudgmentValidationError, match="candidate"):
        service.judge(_concept(), [_candidate(1)])


def test_duplicate_supporting_note_ids_are_normalized_before_caching() -> None:
    judgment = CoverageJudgment(
        status="covered",
        supporting_note_ids=(1, 1, 2, 1),
        missing_facts=(),
        rationale="The supplied notes cover the concept.",
    )
    cache = MemoryCache()
    service = _service(
        QueueStructured([judgment]),
        cache,
        [_note(1), _note(2)],
    )

    result = service.judge(_concept(), [_candidate(1), _candidate(2)])

    assert result.judgment.supporting_note_ids == (1, 2)
    cached = next(iter(cache.values.values()))
    assert cached.result["supporting_note_ids"] == [1, 2]


def test_judgment_status_is_case_and_whitespace_tolerant() -> None:
    judgment = CoverageJudgment(
        status=" Covered ",  # type: ignore[arg-type]
        supporting_note_ids=(1,),
        missing_facts=(),
        rationale="The supplied note covers the concept.",
    )

    assert judgment.status == "covered"


def test_missing_judgment_accepts_negated_coverage_language_and_strips_facts() -> None:
    structured = QueueStructured(
        [
            CoverageJudgment(
                status="missing",
                supporting_note_ids=(),
                missing_facts=("  Treatment timing is absent.  ",),
                rationale="The concept is not fully covered by any candidate.",
            )
        ]
    )
    service = _service(structured, MemoryCache(), [_note(1)])

    result = service.judge(_concept(), [_candidate(1)])

    assert result.judgment.missing_facts == ("Treatment timing is absent.",)


def test_invalid_judgment_gets_one_repair_before_it_is_cached() -> None:
    invalid = CoverageJudgment(
        status="covered",
        supporting_note_ids=(999,),
        missing_facts=(),
        rationale="An unavailable note covers it.",
    )
    repaired = CoverageJudgment(
        status="covered",
        supporting_note_ids=(1,),
        missing_facts=(),
        rationale="The supplied note covers it.",
    )
    structured = QueueStructured([invalid, repaired])
    cache = MemoryCache()
    service = _service(structured, cache, [_note(1)])

    result = service.judge(_concept(), [_candidate(1)])

    assert result.judgment == repaired
    assert structured.calls == 2
    assert "repair" in structured.requests[1][0].casefold()
    assert "not a supplied candidate" in structured.requests[1][1]
    assert len(cache.values) == 1


def test_blank_missing_fact_is_still_rejected_after_one_repair() -> None:
    invalid = CoverageJudgment(
        status="missing",
        supporting_note_ids=(),
        missing_facts=("   ",),
        rationale="No candidate covers the concept.",
    )
    service = _service(
        QueueStructured([invalid, invalid]),
        MemoryCache(),
        [_note(1)],
    )

    with pytest.raises(JudgmentValidationError, match="blank"):
        service.judge(_concept(), [_candidate(1)])


def test_provider_failures_are_not_cached() -> None:
    structured = QueueStructured([RuntimeError("provider unavailable")])
    cache = MemoryCache()
    service = _service(structured, cache, [_note(1)])

    with pytest.raises(RuntimeError, match="provider"):
        service.judge(_concept(), [_candidate(1)])

    assert cache.values == {}
