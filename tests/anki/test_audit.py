from collections.abc import Sequence

import pytest

from oms_hub.anki.audit import (
    AuditBatchV2,
    AuditCacheRecord,
    AuditValidationError,
    CardAuditService,
)
from oms_hub.anki.domain import Candidate, RetrievalPass, SourceKind
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.v2_contracts import AuditVerdictV2
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


def _candidate(note_id: int, *, content_hash: str | None = None) -> Candidate:
    return Candidate(
        note_id=note_id,
        content_hash=content_hash or f"{note_id:064x}",
        best_concept_id="SECRET-CONCEPT",
        provenance={"query": "SECRET-RETRIEVAL-QUERY"},
        scores={"boosted_score": 0.91},
        predicted_band="covered",
        verdict="include",
        confidence=1.0,
        reason="SECRET-JUDGE-RATIONALE",
        context_trap=False,
        recall_direction="unknown",
        mnemonic_classification="unknown",
        dedupe_disposition="pending",
        selected=True,
        retrieval_pass=RetrievalPass.PASS_1,
    )


def _note(note_id: int, *, content_hash: str | None = None) -> NormalizedNote:
    return NormalizedNote(
        note_id=note_id,
        model_name="AnKingOverhaul",
        text=f"Card {note_id}: iron deficiency causes low ferritin.",
        extra="Ferritin reflects iron stores.",
        raw_fields={"Text": "iron deficiency causes low ferritin"},
        tags=("#Pathoma::Hematology",),
        card_ids=(note_id + 1000,),
        media=(),
        token_signature="iron deficiency ferritin",
        content_sha256=content_hash or f"{note_id:064x}",
    )


def _passages() -> list[SourcePassage]:
    return [
        SourcePassage.create(
            revision_id=7,
            lecture_id=12,
            artifact_id="slides-7",
            source_kind=SourceKind.SLIDE,
            locator="slide:3",
            text="Iron deficiency causes low ferritin.",
            slide_number=3,
        ),
        SourcePassage.create(
            revision_id=8,
            lecture_id=12,
            artifact_id="transcript-8",
            source_kind=SourceKind.TRANSCRIPT,
            locator="transcript:1:12-24",
            text="Iron stores fall before microcytosis.",
            start_seconds=12,
            end_seconds=24,
        ),
    ]


def _verdict(note_id: int, verdict: str = "keep") -> AuditVerdictV2:
    return AuditVerdictV2(
        nid=note_id,
        verdict=verdict,  # type: ignore[arg-type]
        primary_subject="iron deficiency",
        support="both" if verdict == "keep" else "none",
        reason="Directly supported by slides and transcript",
        structure_issue=(),
    )


class QueueStructured:
    def __init__(self, batches: Sequence[AuditBatchV2]) -> None:
        self.batches = list(batches)
        self.requests: list[tuple[str, str]] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[AuditBatchV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[AuditBatchV2]:
        assert output_model is AuditBatchV2
        self.requests.append((instruction, input_text))
        value = self.batches.pop(0)
        return StructuredJSONResult(
            value=value,
            raw_text=value.model_dump_json(),
            provider=provider,
            model=model,
            request_id=f"audit-{len(self.requests)}",
            input_tokens=100,
            output_tokens=20,
            cost_microusd=30,
        )


class MemoryCache:
    def __init__(self) -> None:
        self.records: dict[str, AuditCacheRecord] = {}

    def get_audit_cache(self, cache_key: str) -> AuditCacheRecord | None:
        return self.records.get(cache_key)

    def save_audit_cache(self, record: AuditCacheRecord) -> None:
        self.records.setdefault(record.cache_key, record)


class NoteReader:
    def __init__(self, notes: Sequence[NormalizedNote]) -> None:
        self.notes = {note.note_id: note for note in notes}

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.notes.get(note_id)


def _service(
    structured: QueueStructured,
    notes: Sequence[NormalizedNote],
    *,
    cache: MemoryCache | None = None,
    batch_size: int = 30,
) -> CardAuditService:
    return CardAuditService(
        structured,
        cache or MemoryCache(),
        NoteReader(notes),
        provider=ProviderName.OPENAI,
        model="gpt-5.2",
        prompt_text="# Blind card audit",
        prompt_hash="123456789abc",
        batch_size=batch_size,
    )


def test_audit_input_is_blind_to_retrieval_and_coverage_rationale() -> None:
    candidate = _candidate(1)
    structured = QueueStructured(
        [AuditBatchV2(verdicts=(_verdict(1),))]
    )
    service = _service(structured, [_note(1)])

    result = service.audit(
        lecture_id=12,
        lecture_title="Anemia IV",
        lecture_entity_count=6,
        candidates=(candidate,),
        passages=_passages(),
    )

    assert result.verdicts == (_verdict(1),)
    request = structured.requests[0][1]
    assert "Anemia IV" in request
    assert "Iron stores fall before microcytosis" in request
    assert "SECRET-CONCEPT" not in request
    assert "SECRET-RETRIEVAL-QUERY" not in request
    assert "SECRET-JUDGE-RATIONALE" not in request
    assert "boosted_score" not in request


def test_audit_batches_at_configured_limit_and_caches_each_note() -> None:
    candidates = tuple(_candidate(value) for value in (1, 2, 3))
    notes = tuple(_note(value) for value in (1, 2, 3))
    structured = QueueStructured(
        [
            AuditBatchV2(verdicts=(_verdict(1), _verdict(2))),
            AuditBatchV2(verdicts=(_verdict(3, "drop"),)),
        ]
    )
    cache = MemoryCache()
    service = _service(structured, notes, cache=cache, batch_size=2)

    first = service.audit(
        lecture_id=12,
        lecture_title="Anemia IV",
        lecture_entity_count=6,
        candidates=candidates,
        passages=_passages(),
    )
    second = service.audit(
        lecture_id=12,
        lecture_title="Anemia IV",
        lecture_entity_count=6,
        candidates=candidates,
        passages=_passages(),
    )

    assert [item.nid for item in first.verdicts] == [1, 2, 3]
    assert first.cache_hits == 0
    assert second.cache_hits == 3
    assert len(structured.requests) == 2
    assert len(cache.records) == 3


def test_audit_rejects_missing_or_duplicate_candidate_verdicts() -> None:
    invalid = AuditBatchV2(verdicts=(_verdict(1), _verdict(1)))
    service = _service(
        QueueStructured([invalid, invalid]),
        [_note(1), _note(2)],
    )

    with pytest.raises(AuditValidationError, match="exactly once"):
        service.audit(
            lecture_id=12,
            lecture_title="Anemia IV",
            lecture_entity_count=6,
            candidates=(_candidate(1), _candidate(2)),
            passages=_passages(),
        )


def test_audit_cache_invalidates_when_sources_change() -> None:
    cache = MemoryCache()
    structured = QueueStructured(
        [
            AuditBatchV2(verdicts=(_verdict(1),)),
            AuditBatchV2(verdicts=(_verdict(1),)),
        ]
    )
    service = _service(structured, [_note(1)], cache=cache)
    service.audit(
        lecture_id=12,
        lecture_title="Anemia IV",
        lecture_entity_count=6,
        candidates=(_candidate(1),),
        passages=_passages(),
    )
    changed = _passages()
    changed[0] = SourcePassage.create(
        revision_id=10,
        lecture_id=12,
        artifact_id="slides-10",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency also lowers transferrin saturation.",
        slide_number=3,
    )

    result = service.audit(
        lecture_id=12,
        lecture_title="Anemia IV",
        lecture_entity_count=6,
        candidates=(_candidate(1),),
        passages=changed,
    )

    assert result.cache_hits == 0
    assert len(structured.requests) == 2
