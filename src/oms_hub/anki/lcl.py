import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import (
    StructuredJSONResult,
    StructuredOutputError,
    sanitize_model_text,
)

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "of",
    "on",
    "the",
    "to",
    "what",
    "which",
    "with",
}


class LedgerSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passage_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class LectureConcept(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    concept_id: str = Field(min_length=1, max_length=200)
    source_refs: tuple[LedgerSourceRef, ...] = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=4_000)
    hypothetical_card: str = Field(min_length=1, max_length=4_000)
    paraphrases: tuple[
        str,
        str,
    ]
    importance: Literal["core", "supporting"]

    @field_validator(
        "concept_id",
        "statement",
        "hypothetical_card",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("paraphrases")
    @classmethod
    def validate_paraphrases(
        cls,
        value: tuple[str, str],
    ) -> tuple[str, str]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("concept paraphrases cannot be blank")
        return normalized  # type: ignore[return-value]

    @property
    def queries(self) -> tuple[str, str, str, str]:
        return (
            self.statement,
            self.hypothetical_card,
            *self.paraphrases,
        )


class LectureConceptLedger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    concepts: tuple[LectureConcept, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def rekey_duplicate_concept_ids(self) -> "LectureConceptLedger":
        used: set[str] = set()
        normalized: list[LectureConcept] = []
        for concept in self.concepts:
            concept_id = concept.concept_id
            suffix = 2
            while concept_id in used:
                concept_id = f"{concept.concept_id}-{suffix}"
                suffix += 1
            used.add(concept_id)
            normalized.append(
                concept
                if concept_id == concept.concept_id
                else concept.model_copy(update={"concept_id": concept_id})
            )
        object.__setattr__(self, "concepts", tuple(normalized))
        return self


@dataclass(frozen=True, slots=True)
class LCLArtifact:
    ledger: LectureConceptLedger
    raw_response: str
    prompt_version: str
    provider: ProviderName
    model: str
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    repair_attempted: bool
    prompt_hash: str | None = None


class LCLGenerationError(ValueError):
    """The provider returned a ledger that could not be grounded."""


class StructuredLedgerService(Protocol):
    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[LectureConceptLedger],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[LectureConceptLedger]: ...


class LCLService:
    def __init__(
        self,
        structured: StructuredLedgerService,
        *,
        provider: ProviderName,
        model: str,
        prompt_version: str,
        prompt_text: str | None = None,
        prompt_hash: str | None = None,
    ) -> None:
        if not model.strip() or not prompt_version.strip():
            raise ValueError("ledger model and prompt version are required")
        self.structured = structured
        self.provider = provider
        self.model = model
        self.prompt_version = prompt_version
        self.prompt_text = prompt_text.strip() if prompt_text is not None else None
        self.prompt_hash = prompt_hash
        if prompt_text is not None and not self.prompt_text:
            raise ValueError("ledger prompt text cannot be blank")

    def generate(
        self,
        passages: Sequence[SourcePassage],
    ) -> LCLArtifact:
        source_by_id = {
            passage.passage_id: passage for passage in passages
        }
        if len(source_by_id) != len(passages):
            raise ValueError("source bundle contains duplicate passages")
        if not source_by_id:
            raise ValueError("source bundle cannot be empty")
        source_input = _source_input(passages)
        try:
            first = self._request(
                self.prompt_text or _generation_instruction(self.prompt_version),
                source_input,
            )
            _validate_ledger(first.value, source_by_id)
            return self._artifact(first, repair_attempted=False)
        except (StructuredOutputError, LCLGenerationError) as first_error:
            raw = (
                first_error.raw_text
                if isinstance(first_error, StructuredOutputError)
                else (
                    sanitize_model_text(first.raw_text)
                    if "first" in locals()
                    else ""
                )
            )
            repair_input = json.dumps(
                {
                    "source_bundle": json.loads(source_input),
                    "invalid_response": raw,
                    "validation_error": str(first_error),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            try:
                repaired = self._request(
                    _repair_instruction(
                        self.prompt_version,
                        prompt_text=self.prompt_text,
                    ),
                    repair_input,
                )
                _validate_ledger(repaired.value, source_by_id)
            except (
                StructuredOutputError,
                LCLGenerationError,
            ) as repair_error:
                raise LCLGenerationError(str(repair_error)) from repair_error
            return self._artifact(repaired, repair_attempted=True)

    def _request(
        self,
        instruction: str,
        input_text: str,
    ) -> StructuredJSONResult[LectureConceptLedger]:
        return self.structured.generate_json(
            instruction,
            input_text,
            output_model=LectureConceptLedger,
            provider=self.provider,
            model=self.model,
        )

    def _artifact(
        self,
        result: StructuredJSONResult[LectureConceptLedger],
        *,
        repair_attempted: bool,
    ) -> LCLArtifact:
        return LCLArtifact(
            ledger=result.value,
            raw_response=sanitize_model_text(result.raw_text),
            prompt_version=self.prompt_version,
            provider=result.provider,
            model=result.model,
            request_id=result.request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_microusd=result.cost_microusd,
            repair_attempted=repair_attempted,
            prompt_hash=self.prompt_hash,
        )


def _validate_ledger(
    ledger: LectureConceptLedger,
    source_by_id: dict[str, SourcePassage],
) -> None:
    for concept in ledger.concepts:
        cited = []
        for source_ref in concept.source_refs:
            passage = source_by_id.get(source_ref.passage_id)
            if passage is None:
                raise LCLGenerationError(
                    "ledger source reference does not resolve"
                )
            if not passage.text:
                raise LCLGenerationError(
                    "ledger source reference has no extracted evidence"
                )
            cited.append(passage)
        statement_tokens = _meaningful_tokens(concept.statement)
        evidence_tokens = set().union(
            *(_meaningful_tokens(passage.text) for passage in cited)
        )
        if not statement_tokens or not statement_tokens & evidence_tokens:
            raise LCLGenerationError(
                "concept statement is unsupported by its cited source"
            )
        normalized_queries = [
            _normalize_query(query) for query in concept.queries
        ]
        if any(not query for query in normalized_queries):
            raise LCLGenerationError("concept query cannot be blank")
        for position, left in enumerate(normalized_queries):
            for right in normalized_queries[position + 1 :]:
                if _near_duplicate(left, right):
                    raise LCLGenerationError(
                        "concept contains duplicate or near-duplicate queries"
                    )


def _source_input(passages: Sequence[SourcePassage]) -> str:
    return json.dumps(
        {
            "passages": [
                {
                    "passage_id": passage.passage_id,
                    "revision_id": passage.revision_id,
                    "source_kind": passage.source_kind.value,
                    "locator": passage.locator,
                    "citation": passage.citation,
                    "text": passage.text,
                    "extraction_status": passage.extraction_status,
                }
                for passage in sorted(
                    passages,
                    key=lambda item: item.passage_id,
                )
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _generation_instruction(prompt_version: str) -> str:
    return (
        f"Generate lecture concept ledger {prompt_version}. Use only the "
        "provided passages. Cite one or more passage_id values for every "
        "concept. Produce a concise canonical statement, a hypothetical "
        "Anki card, exactly two distinct search paraphrases, importance, and "
        "a unique concept_id. Do not add unsupported medical facts."
    )


def _repair_instruction(
    prompt_version: str,
    *,
    prompt_text: str | None = None,
) -> str:
    repair = (
        f"Repair the invalid lecture concept ledger for {prompt_version}. "
        "Correct only the reported validation defects, use only the supplied "
        "source bundle, and return the complete corrected ledger."
    )
    return repair if prompt_text is None else f"{prompt_text}\n\n{repair}"


def _meaningful_tokens(value: str) -> set[str]:
    return {
        token
        for token in _TOKEN.findall(value.casefold())
        if token not in _STOPWORDS and len(token) > 1
    }


def _normalize_query(value: str) -> frozenset[str]:
    return frozenset(_meaningful_tokens(value))


def _near_duplicate(
    left: frozenset[str],
    right: frozenset[str],
) -> bool:
    if left == right:
        return True
    union = left | right
    return bool(union) and len(left & right) / len(union) >= 0.9
