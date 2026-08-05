import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from oms_hub.anki.domain import SourceKind
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.v2_contracts import (
    IntentionallyUncitedV2,
    LectureConceptLedgerV2,
)
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import (
    StructuredJSONResult,
    StructuredOutputError,
    sanitize_model_text,
)

_TOKEN = re.compile(r"[a-z0-9]+")
_MAX_DIAGNOSTIC_IDS = 12
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
    paraphrases: tuple[str, ...] = Field(min_length=2, max_length=6)
    importance: Literal[
        "core",
        "supporting",
        "high",
        "medium",
        "low",
    ]
    primary_entity: str = ""
    aliases: tuple[str, ...] = ()
    depth: Literal["deep", "medium", "surface"] = "surface"
    emphasis_flag: bool = False
    source_passage_ids: tuple[str, ...] = ()

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
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("concept paraphrases cannot be blank")
        return normalized

    @property
    def queries(self) -> tuple[str, ...]:
        return (
            self.statement,
            self.hypothetical_card,
            *self.paraphrases,
        )


class LectureConceptLedger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    concepts: tuple[LectureConcept, ...] = Field(min_length=1)
    lecture_entity_count: int = Field(default=1, ge=1)
    intentionally_uncited: tuple[IntentionallyUncitedV2, ...] = ()

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


def runtime_ledger_from_v2(
    ledger: LectureConceptLedgerV2,
    passages: Sequence[SourcePassage],
) -> LectureConceptLedger:
    source_by_id = {passage.source_id: passage for passage in passages}
    if len(source_by_id) != len(passages):
        raise ValueError("source bundle contains duplicate readable IDs")
    concepts: list[LectureConcept] = []
    for concept in ledger.concepts:
        try:
            cited = tuple(source_by_id[value] for value in concept.passage_ids)
        except KeyError as exc:
            raise LCLGenerationError(
                "V2 ledger source reference does not resolve"
            ) from exc
        concepts.append(
            LectureConcept(
                concept_id=concept.concept_id,
                source_refs=tuple(
                    LedgerSourceRef(passage_id=passage.passage_id)
                    for passage in cited
                ),
                statement=concept.canonical_statement,
                hypothetical_card=concept.hypothetical_card,
                paraphrases=concept.paraphrases,
                importance=concept.importance,
                primary_entity=concept.primary_entity,
                aliases=concept.aliases,
                depth=concept.depth,
                emphasis_flag=concept.emphasis_flag,
                source_passage_ids=concept.passage_ids,
            )
        )
    return LectureConceptLedger(
        concepts=tuple(concepts),
        lecture_entity_count=ledger.lecture_entity_count,
        intentionally_uncited=ledger.intentionally_uncited,
    )


@dataclass(frozen=True, slots=True)
class LCLArtifact:
    ledger: LectureConceptLedger | LectureConceptLedgerV2
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
    def generate_json[StructuredModel: BaseModel](
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[StructuredModel],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[StructuredModel]: ...


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
        schema_name: Literal["lcl_v1", "lcl_v2"] = "lcl_v1",
    ) -> None:
        if not model.strip() or not prompt_version.strip():
            raise ValueError("ledger model and prompt version are required")
        self.structured = structured
        self.provider = provider
        self.model = model
        self.prompt_version = prompt_version
        self.prompt_text = prompt_text.strip() if prompt_text is not None else None
        self.prompt_hash = prompt_hash
        self.schema_name = schema_name
        if prompt_text is not None and not self.prompt_text:
            raise ValueError("ledger prompt text cannot be blank")

    def generate(
        self,
        passages: Sequence[SourcePassage],
    ) -> LCLArtifact:
        source_by_id = {
            (
                passage.source_id
                if self.schema_name == "lcl_v2"
                else passage.passage_id
            ): passage
            for passage in passages
        }
        if len(source_by_id) != len(passages):
            keys = [
                passage.source_id
                if self.schema_name == "lcl_v2"
                else passage.passage_id
                for passage in passages
            ]
            duplicates = sorted(
                {key for key in keys if keys.count(key) > 1}
            )
            raise ValueError(
                "source bundle contains duplicate passages: "
                f"ids={_format_ids(duplicates)}"
            )
        if not source_by_id:
            raise ValueError("source bundle cannot be empty")
        if self.schema_name == "lcl_v2" and not any(
            passage.source_kind is not SourceKind.SUMMARY
            for passage in passages
        ):
            raise ValueError(
                "V2 LCL requires at least one primary-source passage"
            )
        source_input = (
            _source_input_v2(passages)
            if self.schema_name == "lcl_v2"
            else _source_input(passages)
        )
        try:
            first = self._request(
                self.prompt_text or _generation_instruction(self.prompt_version),
                source_input,
            )
            first = _canonicalize_summary_emphasis(first, source_by_id)
            self._validate(first.value, source_by_id)
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
                repaired = _canonicalize_summary_emphasis(
                    repaired,
                    source_by_id,
                )
                self._validate(repaired.value, source_by_id)
            except (
                StructuredOutputError,
                LCLGenerationError,
            ) as repair_error:
                raise LCLGenerationError(
                    f"{repair_error}; initial validation: {first_error}"
                ) from repair_error
            return self._artifact(repaired, repair_attempted=True)

    def _request(
        self,
        instruction: str,
        input_text: str,
    ) -> StructuredJSONResult[Any]:
        output_model = (
            LectureConceptLedgerV2
            if self.schema_name == "lcl_v2"
            else LectureConceptLedger
        )
        return self.structured.generate_json(
            instruction,
            input_text,
            output_model=output_model,
            provider=self.provider,
            model=self.model,
        )

    def _validate(
        self,
        ledger: LectureConceptLedger | LectureConceptLedgerV2,
        source_by_id: dict[str, SourcePassage],
    ) -> None:
        if isinstance(ledger, LectureConceptLedgerV2):
            _validate_ledger_v2(ledger, source_by_id)
        else:
            _validate_ledger(ledger, source_by_id)

    def _artifact(
        self,
        result: StructuredJSONResult[Any],
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
                    "ledger source reference does not resolve: "
                    f"passage_ids={_format_ids((source_ref.passage_id,))}"
                )
            if not passage.text:
                raise LCLGenerationError(
                    "ledger source reference has no extracted evidence: "
                    f"passage_ids={_format_ids((source_ref.passage_id,))}"
                )
            cited.append(passage)
        statement_tokens = _meaningful_tokens(concept.statement)
        evidence_tokens = set().union(
            *(_meaningful_tokens(passage.text) for passage in cited)
        )
        if not statement_tokens or not statement_tokens & evidence_tokens:
            raise LCLGenerationError(
                "concept statement is unsupported by its cited source: "
                f"concept_id={concept.concept_id}"
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


def _validate_ledger_v2(
    ledger: LectureConceptLedgerV2,
    source_by_id: dict[str, SourcePassage],
) -> None:
    cited_ids: set[str] = set()
    for concept in ledger.concepts:
        cited: list[SourcePassage] = []
        for passage_id in concept.passage_ids:
            passage = source_by_id.get(passage_id)
            if passage is None:
                raise LCLGenerationError(
                    "ledger source reference does not resolve: "
                    f"passage_ids={_format_ids((passage_id,))}; "
                    f"concept_id={concept.concept_id}"
                )
            if not passage.text:
                raise LCLGenerationError(
                    "ledger source reference has no extracted evidence: "
                    f"passage_ids={_format_ids((passage_id,))}; "
                    f"concept_id={concept.concept_id}"
                )
            cited.append(passage)
            cited_ids.add(passage_id)
        if not any(
            passage.source_kind is not SourceKind.SUMMARY
            for passage in cited
        ):
            raise LCLGenerationError(
                "every concept requires primary-source evidence: "
                f"concept_id={concept.concept_id}"
            )
        for passage in cited:
            if (
                passage.summary_section == "emphasis"
                and not concept.emphasis_flag
            ):
                raise LCLGenerationError(
                    "concept emphasis flag conflicts with the summary: "
                    f"concept_id={concept.concept_id}; "
                    f"passage_id={passage.source_id}"
                )
            if passage.summary_section != "depth":
                continue
            match = re.match(
                r"^(deep|medium|surface)\s*:",
                passage.text,
                flags=re.IGNORECASE,
            )
            if match is not None and concept.depth != match.group(1).casefold():
                raise LCLGenerationError(
                    "concept depth classification conflicts with the depth map: "
                    f"concept_id={concept.concept_id}; "
                    f"passage_id={passage.source_id}"
                )
        statement_tokens = _meaningful_tokens(concept.canonical_statement)
        primary_evidence_tokens = set().union(
            *(
                _meaningful_tokens(passage.text)
                for passage in cited
                if passage.source_kind is not SourceKind.SUMMARY
            )
        )
        if (
            not statement_tokens
            or not statement_tokens & primary_evidence_tokens
        ):
            raise LCLGenerationError(
                "concept statement is unsupported by primary evidence: "
                f"concept_id={concept.concept_id}"
            )
    uncited_ids = {item.passage_id for item in ledger.intentionally_uncited}
    if any(passage_id not in source_by_id for passage_id in uncited_ids):
        unresolved = sorted(
            passage_id
            for passage_id in uncited_ids
            if passage_id not in source_by_id
        )
        raise LCLGenerationError(
            "intentionally uncited source reference does not resolve: "
            f"passage_ids={_format_ids(unresolved)}"
        )
    primary_ids = {
        source_id
        for source_id, passage in source_by_id.items()
        if passage.source_kind is not SourceKind.SUMMARY
    }
    partition_errors: list[str] = []
    missing_primary_ids = sorted(primary_ids - cited_ids - uncited_ids)
    if missing_primary_ids:
        partition_errors.append(
            "every primary passage requires a cited or intentionally uncited "
            "disposition: "
            f"missing_primary_passage_ids={_format_ids(missing_primary_ids)}"
        )
    required_summary_ids = {
        source_id
        for source_id, passage in source_by_id.items()
        if passage.source_kind is SourceKind.SUMMARY
        and passage.summary_section in {"depth", "emphasis"}
    }
    missing_summary_ids = sorted(required_summary_ids - cited_ids)
    if missing_summary_ids:
        partition_errors.append(
            "every DEPTH or EMPHASIS summary item must map to a concept: "
            f"missing_summary_passage_ids={_format_ids(missing_summary_ids)}"
        )
    if partition_errors:
        raise LCLGenerationError("; ".join(partition_errors))


def _canonicalize_summary_emphasis(
    result: StructuredJSONResult[Any],
    source_by_id: dict[str, SourcePassage],
) -> StructuredJSONResult[Any]:
    if not isinstance(result.value, LectureConceptLedgerV2):
        return result
    concepts = tuple(
        concept.model_copy(
            update={"emphasis_flag": True, "importance": "high"}
        )
        if not concept.emphasis_flag
        and any(
            (passage := source_by_id.get(passage_id)) is not None
            and passage.summary_section == "emphasis"
            for passage_id in concept.passage_ids
        )
        else concept
        for concept in result.value.concepts
    )
    if concepts == result.value.concepts:
        return result
    return replace(
        result,
        value=result.value.model_copy(update={"concepts": concepts}),
    )


def _format_ids(values: Sequence[str]) -> str:
    unique = sorted(set(values))
    preview = unique[:_MAX_DIAGNOSTIC_IDS]
    suffix = "..." if len(unique) > _MAX_DIAGNOSTIC_IDS else ""
    return "[" + ", ".join(preview) + suffix + "]"


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


def _source_input_v2(passages: Sequence[SourcePassage]) -> str:
    return json.dumps(
        {
            "passages": [
                {
                    "passage_id": passage.source_id,
                    "source_kind": passage.source_kind.value,
                    "locator": passage.locator,
                    "citation": passage.citation,
                    "text": passage.text,
                    "extraction_status": passage.extraction_status,
                    "summary_backrefs": list(passage.summary_backrefs),
                    "summary_section": passage.summary_section,
                }
                for passage in sorted(
                    passages,
                    key=lambda item: item.source_id,
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
        "Correct the reported validation defects without removing or changing "
        "any already-valid concept or passage disposition. Use only the "
        "supplied source bundle and return the complete corrected ledger. "
        "Recheck the entire ledger before returning it: every non-summary "
        "passage must be cited or intentionally uncited, and every DEPTH or "
        "EMPHASIS summary passage must be cited by a primary-grounded concept. "
        "Give every listed missing passage ID exactly one valid disposition."
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
