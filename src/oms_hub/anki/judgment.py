import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from oms_hub.anki.domain import Candidate
from oms_hub.anki.lcl import LectureConcept
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult


class CoverageJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["covered", "partial", "missing"]
    supporting_note_ids: tuple[int, ...]
    missing_facts: tuple[str, ...]
    rationale: str = Field(min_length=1, max_length=4_000)


@dataclass(frozen=True, slots=True)
class JudgmentCacheRecord:
    cache_key: str
    concept_content_hash: str
    candidate_digest: str
    prompt_version: str
    provider: ProviderName
    model: str
    result: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    created_at: str


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    judgment: CoverageJudgment
    cache_key: str
    cache_hit: bool
    provider: ProviderName
    model: str
    request_id: str | None
    input_tokens: int
    output_tokens: int
    cost_microusd: int


class JudgmentValidationError(ValueError):
    """A structured judgment contradicts its candidates or status."""


class JudgmentCache(Protocol):
    def get_judgment_cache(
        self,
        cache_key: str,
    ) -> JudgmentCacheRecord | None: ...

    def save_judgment_cache(
        self,
        record: JudgmentCacheRecord,
    ) -> None: ...


class JudgmentNoteReader(Protocol):
    def get_note(self, note_id: int) -> NormalizedNote | None: ...


class StructuredJudgmentService(Protocol):
    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[CoverageJudgment],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[CoverageJudgment]: ...


class JudgmentService:
    def __init__(
        self,
        structured: StructuredJudgmentService,
        cache: JudgmentCache,
        notes: JudgmentNoteReader,
        *,
        provider: ProviderName,
        model: str,
        prompt_version: str,
    ) -> None:
        if not model.strip() or not prompt_version.strip():
            raise ValueError(
                "judgment model and prompt version are required"
            )
        self.structured = structured
        self.cache = cache
        self.notes = notes
        self.provider = provider
        self.model = model
        self.prompt_version = prompt_version

    def judge(
        self,
        concept: LectureConcept,
        candidates: Sequence[Candidate],
    ) -> JudgmentResult:
        candidate_notes = self._candidate_notes(candidates)
        candidate_ids = {candidate.note_id for candidate in candidates}
        concept_hash = _sha256_json(
            concept.model_dump(mode="json")
        )
        candidate_digest = _sha256_json(
            [
                {
                    "note_id": candidate.note_id,
                    "content_hash": candidate.content_hash,
                }
                for candidate in sorted(
                    candidates,
                    key=lambda item: item.note_id,
                )
            ]
        )
        cache_key = _sha256_json(
            {
                "concept_content_hash": concept_hash,
                "candidate_digest": candidate_digest,
                "prompt_version": self.prompt_version,
                "provider": self.provider.value,
                "model": self.model,
            }
        )
        cached = self.cache.get_judgment_cache(cache_key)
        if cached is not None:
            try:
                judgment = CoverageJudgment.model_validate(cached.result)
                _validate_judgment(judgment, candidate_ids)
            except (ValueError, JudgmentValidationError):
                pass
            else:
                return JudgmentResult(
                    judgment=judgment,
                    cache_key=cache_key,
                    cache_hit=True,
                    provider=cached.provider,
                    model=cached.model,
                    request_id=None,
                    input_tokens=cached.input_tokens,
                    output_tokens=cached.output_tokens,
                    cost_microusd=cached.cost_microusd,
                )

        generated = self.structured.generate_json(
            _judgment_instruction(self.prompt_version),
            _judgment_input(concept, candidates, candidate_notes),
            output_model=CoverageJudgment,
            provider=self.provider,
            model=self.model,
        )
        _validate_judgment(generated.value, candidate_ids)
        record = JudgmentCacheRecord(
            cache_key=cache_key,
            concept_content_hash=concept_hash,
            candidate_digest=candidate_digest,
            prompt_version=self.prompt_version,
            provider=generated.provider,
            model=generated.model,
            result=generated.value.model_dump(mode="json"),
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            cost_microusd=generated.cost_microusd,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.cache.save_judgment_cache(record)
        return JudgmentResult(
            judgment=generated.value,
            cache_key=cache_key,
            cache_hit=False,
            provider=generated.provider,
            model=generated.model,
            request_id=generated.request_id,
            input_tokens=generated.input_tokens,
            output_tokens=generated.output_tokens,
            cost_microusd=generated.cost_microusd,
        )

    def _candidate_notes(
        self,
        candidates: Sequence[Candidate],
    ) -> dict[int, NormalizedNote]:
        if len({candidate.note_id for candidate in candidates}) != len(
            candidates
        ):
            raise ValueError("judgment candidates must be unique")
        notes: dict[int, NormalizedNote] = {}
        for candidate in candidates:
            if (
                candidate.note_id <= 0
                or not _is_sha256(candidate.content_hash)
            ):
                raise ValueError("judgment candidate metadata is invalid")
            note = self.notes.get_note(candidate.note_id)
            if note is None:
                raise ValueError(
                    "judgment candidate is absent from companion index"
                )
            if note.content_sha256 != candidate.content_hash:
                raise ValueError(
                    "judgment candidate content is stale"
                )
            notes[candidate.note_id] = note
        return notes


def _validate_judgment(
    judgment: CoverageJudgment,
    candidate_ids: set[int],
) -> None:
    supporting = judgment.supporting_note_ids
    if len(set(supporting)) != len(supporting):
        raise JudgmentValidationError(
            "supporting candidate note IDs must be unique"
        )
    if any(note_id not in candidate_ids for note_id in supporting):
        raise JudgmentValidationError(
            "supporting note ID is not a supplied candidate"
        )
    missing_facts = tuple(
        fact.strip() for fact in judgment.missing_facts
    )
    if any(not fact for fact in missing_facts):
        raise JudgmentValidationError("missing facts cannot be blank")
    rationale = judgment.rationale.casefold()
    if judgment.status == "covered":
        if not supporting:
            raise JudgmentValidationError(
                "covered judgment requires a supporting candidate"
            )
        if missing_facts:
            raise JudgmentValidationError(
                "covered judgment cannot contain missing facts"
            )
    elif judgment.status == "missing":
        if supporting:
            raise JudgmentValidationError(
                "missing judgment cannot cite supporting candidates"
            )
        if not missing_facts:
            raise JudgmentValidationError(
                "missing judgment requires missing facts"
            )
        if any(
            phrase in rationale
            for phrase in ("fully covered", "adequately covered", "no gap")
        ):
            raise JudgmentValidationError(
                "missing judgment rationale contradicts its status"
            )
    elif not missing_facts:
        raise JudgmentValidationError(
            "partial judgment requires missing facts"
        )


def _judgment_instruction(prompt_version: str) -> str:
    return (
        f"Apply coverage rubric {prompt_version}. Decide whether the supplied "
        "Anki candidates fully cover, partially cover, or miss the lecture "
        "concept. Use only supplied note IDs. List exact missing facts and "
        "give a concise rationale. Do not treat retrieval rank as coverage."
    )


def _judgment_input(
    concept: LectureConcept,
    candidates: Sequence[Candidate],
    notes: dict[int, NormalizedNote],
) -> str:
    return json.dumps(
        {
            "concept": concept.model_dump(mode="json"),
            "candidates": [
                {
                    "note_id": candidate.note_id,
                    "text": notes[candidate.note_id].text,
                    "extra": notes[candidate.note_id].extra,
                    "tags": notes[candidate.note_id].tags,
                    "retrieval_scores": candidate.scores,
                }
                for candidate in candidates
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
