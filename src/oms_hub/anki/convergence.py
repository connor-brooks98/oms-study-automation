import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oms_hub.anki.lcl import LectureConcept
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.v2_contracts import ParaphraseExpansionV2
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import (
    StructuredJSONResult,
    StructuredOutputError,
    sanitize_model_text,
)

MAX_CONVERGENCE_PASSES = 5
CONVERGENCE_GROWTH_THRESHOLD = 0.05


@dataclass(frozen=True, slots=True)
class GrowthUpdate:
    new_note_ids: tuple[int, ...]
    seen_note_ids: tuple[int, ...]
    growth: float
    converged: bool


class ConvergenceState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    concept_id: str = Field(min_length=1, max_length=200)
    passes_run: int = Field(ge=1, le=MAX_CONVERGENCE_PASSES)
    seen_note_ids: tuple[int, ...]
    growth: tuple[float, ...] = Field(
        min_length=1,
        max_length=MAX_CONVERGENCE_PASSES,
    )
    converged: bool

    @model_validator(mode="after")
    def reconcile_history(self) -> "ConvergenceState":
        if self.passes_run != len(self.growth):
            raise ValueError("convergence pass count must match growth history")
        if len(self.seen_note_ids) != len(set(self.seen_note_ids)):
            raise ValueError("convergence note IDs must be unique")
        if any(value < 0 or value > 1 for value in self.growth):
            raise ValueError("convergence growth must be between zero and one")
        return self


@dataclass(frozen=True, slots=True)
class ExpansionResult:
    expansion: ParaphraseExpansionV2
    request_ids: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    cost_microusd: int


class ConvergenceValidationError(ValueError):
    """Convergence output violated its deterministic search contract."""


class StructuredExpansionService(Protocol):
    def generate_json[StructuredModel: BaseModel](
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[StructuredModel],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[StructuredModel]: ...


class ParaphraseExpansionService:
    def __init__(
        self,
        structured: StructuredExpansionService,
        *,
        provider: ProviderName,
        model: str,
        prompt_text: str,
        prompt_hash: str,
    ) -> None:
        if (
            not model.strip()
            or not prompt_text.strip()
            or len(prompt_hash) != 12
        ):
            raise ValueError("paraphrase-expansion configuration is invalid")
        self.structured = structured
        self.provider = provider
        self.model = model
        self.prompt_text = prompt_text.strip()
        self.prompt_hash = prompt_hash

    def expand(
        self,
        concept: LectureConcept,
        *,
        used_paraphrases: Sequence[str],
        found_notes: Sequence[NormalizedNote],
        missing_facts: Sequence[str],
    ) -> ExpansionResult:
        expansion_input = _expansion_input(
            concept,
            used_paraphrases=used_paraphrases,
            found_notes=found_notes,
            missing_facts=missing_facts,
        )
        attempts: list[StructuredJSONResult[ParaphraseExpansionV2]] = []
        try:
            first = self._request(self.prompt_text, expansion_input)
            attempts.append(first)
            _validate_expansion(
                first.value,
                concept,
                used_paraphrases,
            )
            expansion = first.value
        except (
            StructuredOutputError,
            ConvergenceValidationError,
        ) as first_error:
            raw = (
                first_error.raw_text
                if isinstance(first_error, StructuredOutputError)
                else sanitize_model_text(first.raw_text)
            )
            repair_input = json.dumps(
                {
                    "expansion_input": json.loads(expansion_input),
                    "invalid_response": raw,
                    "validation_error": str(first_error),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            repaired = self._request(
                f"{self.prompt_text}\n\nRepair the invalid paraphrase "
                "expansion. Correct only the reported defect and return the "
                "complete expansion.",
                repair_input,
            )
            attempts.append(repaired)
            _validate_expansion(
                repaired.value,
                concept,
                used_paraphrases,
            )
            expansion = repaired.value
        return ExpansionResult(
            expansion=expansion,
            request_ids=tuple(attempt.request_id for attempt in attempts),
            input_tokens=sum(attempt.input_tokens for attempt in attempts),
            output_tokens=sum(attempt.output_tokens for attempt in attempts),
            cost_microusd=sum(attempt.cost_microusd for attempt in attempts),
        )

    def _request(
        self,
        instruction: str,
        input_text: str,
    ) -> StructuredJSONResult[ParaphraseExpansionV2]:
        return self.structured.generate_json(
            instruction,
            input_text,
            output_model=ParaphraseExpansionV2,
            provider=self.provider,
            model=self.model,
        )


def update_growth(
    *,
    seen_note_ids: Sequence[int],
    retrieved_note_ids: Sequence[int],
) -> GrowthUpdate:
    seen = set(seen_note_ids)
    new = set(retrieved_note_ids) - seen
    cumulative = seen | new
    growth = len(new) / max(len(cumulative), 1)
    return GrowthUpdate(
        new_note_ids=tuple(sorted(new)),
        seen_note_ids=tuple(sorted(cumulative)),
        growth=growth,
        converged=growth < CONVERGENCE_GROWTH_THRESHOLD,
    )


def _validate_expansion(
    expansion: ParaphraseExpansionV2,
    concept: LectureConcept,
    used_paraphrases: Sequence[str],
) -> None:
    if expansion.concept_id != concept.concept_id:
        raise ConvergenceValidationError(
            "paraphrase expansion belongs to another concept"
        )
    primary = concept.primary_entity.casefold()
    if not primary or any(
        primary not in paraphrase.casefold()
        for paraphrase in expansion.paraphrases
    ):
        raise ConvergenceValidationError(
            "every expanded paraphrase must retain the primary entity"
        )
    used = {value.strip().casefold() for value in used_paraphrases}
    if any(value.casefold() in used for value in expansion.paraphrases):
        raise ConvergenceValidationError(
            "expanded paraphrases must be new"
        )


def _expansion_input(
    concept: LectureConcept,
    *,
    used_paraphrases: Sequence[str],
    found_notes: Sequence[NormalizedNote],
    missing_facts: Sequence[str],
) -> str:
    return json.dumps(
        {
            "concept": {
                "concept_id": concept.concept_id,
                "canonical_statement": concept.statement,
                "primary_entity": concept.primary_entity,
                "aliases": list(concept.aliases),
            },
            "used_paraphrases": list(used_paraphrases),
            "found_card_fronts": [
                {"nid": note.note_id, "text": note.text}
                for note in found_notes[:20]
            ],
            "missing_facts": list(missing_facts),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
