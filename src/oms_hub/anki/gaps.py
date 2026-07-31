import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from oms_hub.anki.domain import SourceReference
from oms_hub.anki.lcl import LectureConcept
from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import (
    StructuredJSONResult,
    StructuredOutputError,
    StructuredTextService,
    sanitize_model_text,
)

_CLOZE = re.compile(
    r"\{\{c(?P<number>\d+)::(?P<answer>.*?)(?:::[^{}]*?)?\}\}",
    re.IGNORECASE,
)
_UNSAFE_HTML = re.compile(
    r"(?is)<\s*(?:script|iframe|object|embed|style)\b"
    r"|javascript\s*:"
    r"|\bon[a-z]+\s*=",
)
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SupportedGap:
    concept: LectureConcept
    evidence: tuple[SourcePassage, ...]
    initial_tags: tuple[str, ...]
    outcome: Literal["gap_supported"] = "gap_supported"

    def __post_init__(self) -> None:
        if self.outcome != "gap_supported":
            raise ValueError("cards can be generated only for supported gaps")
        if not self.evidence or any(not passage.text for passage in self.evidence):
            raise ValueError("supported gap requires extracted source evidence")


class CardDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    note_type: Literal["Cloze"]
    text: str = Field(min_length=1, max_length=10_000)
    extra: str = Field(max_length=20_000)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)

    @field_validator("note_type", mode="before")
    @classmethod
    def normalize_note_type(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().casefold() == "cloze":
            return "Cloze"
        return value

    @field_validator("evidence_ids")
    @classmethod
    def dedupe_evidence_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))


class EntailmentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "supported",
        "not_supported",
        "contradicted",
        "uncertain",
    ]
    rationale: str = Field(min_length=1, max_length=4_000)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value


@dataclass(frozen=True, slots=True)
class GapCardProposal:
    concept_id: str
    note_type: str
    fields: dict[str, str]
    source_refs: tuple[SourceReference, ...]
    evidence_ids: tuple[str, ...]
    initial_tags: tuple[str, ...]
    provider: ProviderName
    model: str
    prompt_version: str
    confidence: float
    content_hash: str
    provenance: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GapGenerationResult:
    status: Literal["proposed", "rejected", "unresolved"]
    proposal: GapCardProposal | None
    reason: str


class GapValidationError(ValueError):
    """A generated card failed deterministic source-safety checks."""


class GapCardService:
    def __init__(
        self,
        structured: StructuredTextService,
        *,
        provider: ProviderName,
        model: str,
        prompt_version: str,
    ) -> None:
        if not model.strip() or not prompt_version.strip():
            raise ValueError("gap model and prompt version are required")
        self.structured = structured
        self.provider = provider
        self.model = model
        self.prompt_version = prompt_version

    def generate(self, gap: SupportedGap) -> GapGenerationResult:
        generation_input = _generation_input(gap)
        evidence_by_id = {passage.passage_id: passage for passage in gap.evidence}
        try:
            generated = self._draft_request(
                _generation_instruction(self.prompt_version),
                generation_input,
            )
            text, extra = _validate_draft(
                generated.value,
                evidence_by_id,
            )
        except (
            StructuredOutputError,
            GapValidationError,
        ) as first_error:
            raw = (
                first_error.raw_text
                if isinstance(first_error, StructuredOutputError)
                else (
                    sanitize_model_text(generated.raw_text)
                    if "generated" in locals()
                    else ""
                )
            )
            repair_input = json.dumps(
                {
                    "generation_input": json.loads(generation_input),
                    "invalid_response": raw,
                    "validation_error": str(first_error),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            generated = self._draft_request(
                _repair_instruction(self.prompt_version),
                repair_input,
            )
            text, extra = _validate_draft(
                generated.value,
                evidence_by_id,
            )
        draft = generated.value
        cited = tuple(evidence_by_id[evidence_id] for evidence_id in draft.evidence_ids)
        entailment_input = _entailment_input(text, extra, cited)
        try:
            entailment = self.structured.generate_json(
                _entailment_instruction(self.prompt_version),
                entailment_input,
                output_model=EntailmentDecision,
                provider=self.provider,
                model=self.model,
            )
        except StructuredOutputError as first_error:
            repair_input = json.dumps(
                {
                    "entailment_input": json.loads(entailment_input),
                    "invalid_response": first_error.raw_text,
                    "validation_error": str(first_error),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            entailment = self.structured.generate_json(
                _entailment_repair_instruction(self.prompt_version),
                repair_input,
                output_model=EntailmentDecision,
                provider=self.provider,
                model=self.model,
            )
        if entailment.value.status in {
            "not_supported",
            "contradicted",
        }:
            return GapGenerationResult(
                status="rejected",
                proposal=None,
                reason=entailment.value.rationale,
            )
        if entailment.value.status == "uncertain":
            return GapGenerationResult(
                status="unresolved",
                proposal=None,
                reason=entailment.value.rationale,
            )
        fields = {"Text": text, "Extra": extra}
        content_hash = _content_hash(draft.note_type, fields)
        proposal = GapCardProposal(
            concept_id=gap.concept.concept_id,
            note_type=draft.note_type,
            fields=fields,
            source_refs=tuple(
                SourceReference(
                    source_kind=passage.source_kind,
                    revision_id=passage.revision_id,
                    locator=passage.locator,
                    content_hash=passage.content_hash,
                )
                for passage in cited
            ),
            evidence_ids=draft.evidence_ids,
            initial_tags=tuple(dict.fromkeys(gap.initial_tags)),
            provider=generated.provider,
            model=generated.model,
            prompt_version=self.prompt_version,
            confidence=draft.confidence,
            content_hash=content_hash,
            provenance={
                "generation_request_id": generated.request_id,
                "generation_input_tokens": generated.input_tokens,
                "generation_output_tokens": generated.output_tokens,
                "generation_cost_microusd": generated.cost_microusd,
                "entailment_request_id": entailment.request_id,
                "entailment_status": entailment.value.status,
                "entailment_rationale": entailment.value.rationale,
                "entailment_input_tokens": entailment.input_tokens,
                "entailment_output_tokens": entailment.output_tokens,
                "entailment_cost_microusd": entailment.cost_microusd,
            },
        )
        return GapGenerationResult(
            status="proposed",
            proposal=proposal,
            reason=entailment.value.rationale,
        )

    def _draft_request(
        self,
        instruction: str,
        input_text: str,
    ) -> StructuredJSONResult[CardDraft]:
        return self.structured.generate_json(
            instruction,
            input_text,
            output_model=CardDraft,
            provider=self.provider,
            model=self.model,
        )


def _validate_draft(
    draft: CardDraft,
    evidence_by_id: dict[str, SourcePassage],
) -> tuple[str, str]:
    if any(
        evidence_id not in evidence_by_id
        for evidence_id in draft.evidence_ids
    ):
        raise GapValidationError("generated card cites unavailable evidence")
    text = draft.text.strip()
    extra = draft.extra.strip()
    validate_gap_card_fields(text, extra)
    return text, extra


def validate_gap_card_fields(text: str, extra: str) -> None:
    """Apply the same deterministic safety checks to generated or edited cards."""
    if not text or len(text) > 10_000 or len(extra) > 20_000:
        raise GapValidationError("generated card fields violate length limits")
    if _UNSAFE_HTML.search(f"{text}\n{extra}"):
        raise GapValidationError("generated card contains unsafe HTML")
    clozes = list(_CLOZE.finditer(text))
    if not clozes:
        raise GapValidationError("generated Cloze card contains no cloze deletion")
    numbers = {int(match.group("number")) for match in clozes}
    if numbers != set(range(1, max(numbers) + 1)):
        raise GapValidationError(
            "generated cloze numbering must start at one and be contiguous"
        )
    visible = _normalize_for_leakage(_CLOZE.sub(" ", text))
    for match in clozes:
        answer = _normalize_for_leakage(match.group("answer"))
        if not answer:
            raise GapValidationError("generated cloze answer cannot be blank")
        if answer in visible:
            raise GapValidationError(
                "generated card leaks a cloze answer outside the cloze"
            )


def _generation_instruction(prompt_version: str) -> str:
    return (
        f"Generate one source-grounded Anki Cloze card using prompt "
        f"{prompt_version}. Use only the supplied concept and evidence. "
        "Cite each passage ID used at most once. Keep the front concise and "
        "put only source-supported explanation in Extra."
    )


def _repair_instruction(prompt_version: str) -> str:
    return (
        f"Repair the invalid source-grounded Anki card for {prompt_version}. "
        "Correct only the reported validation defects, use and cite only the "
        "supplied evidence, and return the complete corrected card draft."
    )


def _generation_input(gap: SupportedGap) -> str:
    return json.dumps(
        {
            "concept": gap.concept.model_dump(mode="json"),
            "evidence": [
                {
                    "passage_id": passage.passage_id,
                    "citation": passage.citation,
                    "text": passage.text,
                }
                for passage in gap.evidence
            ],
            "initial_tags": gap.initial_tags,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _entailment_instruction(prompt_version: str) -> str:
    return (
        f"Apply entailment rubric {prompt_version}. Judge every factual claim "
        "in the proposed card using only the cited passages. Return supported, "
        "not_supported, contradicted, or uncertain. Do not use outside "
        "medical knowledge."
    )


def _entailment_repair_instruction(prompt_version: str) -> str:
    return (
        f"Repair the invalid entailment decision for {prompt_version}. "
        "Correct only the reported schema defect, use only the supplied "
        "card and passages, and return the complete corrected decision."
    )


def _entailment_input(
    text: str,
    extra: str,
    evidence: tuple[SourcePassage, ...],
) -> str:
    return json.dumps(
        {
            "proposed_card": {"Text": text, "Extra": extra},
            "cited_passages": [
                {
                    "passage_id": passage.passage_id,
                    "text": passage.text,
                }
                for passage in evidence
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _content_hash(note_type: str, fields: dict[str, str]) -> str:
    canonical = json.dumps(
        {"note_type": note_type, "fields": fields},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_for_leakage(value: str) -> str:
    return _SPACE.sub(" ", re.sub(r"<[^>]+>", " ", value)).strip().casefold()
