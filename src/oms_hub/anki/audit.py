import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from oms_hub.anki.domain import Candidate
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.v2_contracts import AuditVerdictV2
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import (
    StructuredJSONResult,
    StructuredOutputError,
    sanitize_model_text,
)


class AuditBatchV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdicts: tuple[AuditVerdictV2, ...]


@dataclass(frozen=True, slots=True)
class AuditCacheRecord:
    cache_key: str
    note_id: int
    lecture_id: int
    note_content_hash: str
    source_digest: str
    prompt_hash: str
    provider: ProviderName
    model: str
    result: dict[str, object]
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    created_at: str


@dataclass(frozen=True, slots=True)
class AuditRunResult:
    verdicts: tuple[AuditVerdictV2, ...]
    cache_hits: int
    request_ids: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    cost_microusd: int


class AuditValidationError(ValueError):
    """An audit response did not partition its supplied note IDs."""


class AuditCache(Protocol):
    def get_audit_cache(
        self,
        cache_key: str,
    ) -> AuditCacheRecord | None: ...

    def save_audit_cache(self, record: AuditCacheRecord) -> None: ...


class AuditNoteReader(Protocol):
    def get_note(self, note_id: int) -> NormalizedNote | None: ...


class StructuredAuditService(Protocol):
    def generate_json[StructuredModel: BaseModel](
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[StructuredModel],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[StructuredModel]: ...


class CardAuditService:
    def __init__(
        self,
        structured: StructuredAuditService,
        cache: AuditCache,
        notes: AuditNoteReader,
        *,
        provider: ProviderName,
        model: str,
        prompt_text: str,
        prompt_hash: str,
        batch_size: int = 30,
    ) -> None:
        if (
            not model.strip()
            or not prompt_text.strip()
            or len(prompt_hash) != 12
            or batch_size < 1
        ):
            raise ValueError("card-audit configuration is invalid")
        self.structured = structured
        self.cache = cache
        self.notes = notes
        self.provider = provider
        self.model = model
        self.prompt_text = prompt_text.strip()
        self.prompt_hash = prompt_hash
        self.batch_size = batch_size

    def audit(
        self,
        *,
        lecture_id: int,
        lecture_title: str,
        lecture_entity_count: int,
        candidates: Sequence[Candidate],
        passages: Sequence[SourcePassage],
    ) -> AuditRunResult:
        if lecture_id <= 0 or not lecture_title.strip():
            raise ValueError("audit lecture identity is invalid")
        if lecture_entity_count < 1 or not passages:
            raise ValueError("audit source context is invalid")
        candidate_ids = [candidate.note_id for candidate in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("audit candidates must be unique")
        notes = self._candidate_notes(candidates)
        source_digest = _source_digest(passages)
        cached: dict[int, AuditVerdictV2] = {}
        pending: list[Candidate] = []
        keys: dict[int, str] = {}
        for candidate in candidates:
            cache_key = _cache_key(
                candidate,
                lecture_id=lecture_id,
                source_digest=source_digest,
                prompt_hash=self.prompt_hash,
                provider=self.provider,
                model=self.model,
            )
            keys[candidate.note_id] = cache_key
            record = self.cache.get_audit_cache(cache_key)
            if record is None:
                pending.append(candidate)
                continue
            try:
                verdict = AuditVerdictV2.model_validate(record.result)
            except ValueError:
                pending.append(candidate)
                continue
            if verdict.nid != candidate.note_id:
                pending.append(candidate)
                continue
            cached[candidate.note_id] = verdict

        generated: dict[int, AuditVerdictV2] = {}
        request_ids: list[str] = []
        input_tokens = 0
        output_tokens = 0
        cost_microusd = 0
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start : start + self.batch_size]
            result, attempts = self._audit_batch(
                lecture_title=lecture_title,
                lecture_entity_count=lecture_entity_count,
                candidates=batch,
                notes=notes,
                passages=passages,
            )
            request_ids.extend(item.request_id for item in attempts)
            input_tokens += sum(item.input_tokens for item in attempts)
            output_tokens += sum(item.output_tokens for item in attempts)
            cost_microusd += sum(item.cost_microusd for item in attempts)
            for verdict in result.verdicts:
                generated[verdict.nid] = verdict
                candidate = next(
                    item for item in batch if item.note_id == verdict.nid
                )
                self.cache.save_audit_cache(
                    AuditCacheRecord(
                        cache_key=keys[verdict.nid],
                        note_id=verdict.nid,
                        lecture_id=lecture_id,
                        note_content_hash=candidate.content_hash,
                        source_digest=source_digest,
                        prompt_hash=self.prompt_hash,
                        provider=self.provider,
                        model=self.model,
                        result=verdict.model_dump(mode="json"),
                        input_tokens=attempts[-1].input_tokens,
                        output_tokens=attempts[-1].output_tokens,
                        cost_microusd=attempts[-1].cost_microusd,
                        created_at=datetime.now(UTC).isoformat(),
                    )
                )
        verdict_by_id = {**cached, **generated}
        if set(verdict_by_id) != set(candidate_ids):
            raise AuditValidationError(
                "every audit candidate must appear exactly once"
            )
        return AuditRunResult(
            verdicts=tuple(verdict_by_id[nid] for nid in sorted(candidate_ids)),
            cache_hits=len(cached),
            request_ids=tuple(request_ids),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
        )

    def _audit_batch(
        self,
        *,
        lecture_title: str,
        lecture_entity_count: int,
        candidates: Sequence[Candidate],
        notes: dict[int, NormalizedNote],
        passages: Sequence[SourcePassage],
    ) -> tuple[
        AuditBatchV2,
        tuple[StructuredJSONResult[AuditBatchV2], ...],
    ]:
        audit_input = _audit_input(
            lecture_title=lecture_title,
            lecture_entity_count=lecture_entity_count,
            candidates=candidates,
            notes=notes,
            passages=passages,
        )
        attempts: list[StructuredJSONResult[AuditBatchV2]] = []
        try:
            first = self._request(self.prompt_text, audit_input)
            attempts.append(first)
            _validate_batch(first.value, {item.note_id for item in candidates})
            return first.value, tuple(attempts)
        except (StructuredOutputError, AuditValidationError) as first_error:
            raw = (
                first_error.raw_text
                if isinstance(first_error, StructuredOutputError)
                else sanitize_model_text(first.raw_text)
            )
            repair_input = json.dumps(
                {
                    "audit_input": json.loads(audit_input),
                    "invalid_response": raw,
                    "validation_error": str(first_error),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            repaired = self._request(
                f"{self.prompt_text}\n\nRepair the invalid audit batch. "
                "Correct only the reported defect and return the complete batch.",
                repair_input,
            )
            attempts.append(repaired)
            _validate_batch(
                repaired.value,
                {item.note_id for item in candidates},
            )
            return repaired.value, tuple(attempts)

    def _request(
        self,
        instruction: str,
        input_text: str,
    ) -> StructuredJSONResult[AuditBatchV2]:
        return self.structured.generate_json(
            instruction,
            input_text,
            output_model=AuditBatchV2,
            provider=self.provider,
            model=self.model,
        )

    def _candidate_notes(
        self,
        candidates: Sequence[Candidate],
    ) -> dict[int, NormalizedNote]:
        notes: dict[int, NormalizedNote] = {}
        for candidate in candidates:
            note = self.notes.get_note(candidate.note_id)
            if note is None:
                raise ValueError("audit candidate is absent from companion index")
            if note.content_sha256 != candidate.content_hash:
                raise ValueError("audit candidate content is stale")
            notes[candidate.note_id] = note
        return notes


def _validate_batch(batch: AuditBatchV2, expected_ids: set[int]) -> None:
    returned = [verdict.nid for verdict in batch.verdicts]
    if set(returned) != expected_ids or len(returned) != len(set(returned)):
        raise AuditValidationError(
            "every audit candidate must appear exactly once"
        )


def _audit_input(
    *,
    lecture_title: str,
    lecture_entity_count: int,
    candidates: Sequence[Candidate],
    notes: dict[int, NormalizedNote],
    passages: Sequence[SourcePassage],
) -> str:
    return json.dumps(
        {
            "lecture_title": lecture_title,
            "lecture_entity_count": lecture_entity_count,
            "sources": [
                {
                    "passage_id": passage.source_id,
                    "source_kind": passage.source_kind.value,
                    "locator": passage.locator,
                    "text": passage.text,
                    "summary_backrefs": list(passage.summary_backrefs),
                }
                for passage in sorted(passages, key=lambda item: item.source_id)
            ],
            "cards": [
                {
                    "nid": candidate.note_id,
                    "text": notes[candidate.note_id].text,
                    "extra": notes[candidate.note_id].extra,
                    "tags": list(notes[candidate.note_id].tags),
                }
                for candidate in candidates
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _source_digest(passages: Sequence[SourcePassage]) -> str:
    return _sha256_json(
        [
            {
                "source_id": passage.source_id,
                "content_hash": passage.content_hash,
                "source_kind": passage.source_kind.value,
            }
            for passage in sorted(passages, key=lambda item: item.source_id)
        ]
    )


def _cache_key(
    candidate: Candidate,
    *,
    lecture_id: int,
    source_digest: str,
    prompt_hash: str,
    provider: ProviderName,
    model: str,
) -> str:
    return _sha256_json(
        {
            "note_id": candidate.note_id,
            "note_content_hash": candidate.content_hash,
            "lecture_id": lecture_id,
            "source_digest": source_digest,
            "prompt_hash": prompt_hash,
            "provider": provider.value,
            "model": model,
        }
    )


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
