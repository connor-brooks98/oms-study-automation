"""Frozen, evidence-only R9 generation contracts for ``card_centric_v3``."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms_hub.anki.classification_v3 import ESTIMATOR_VERSION
from oms_hub.anki.contracts import canonical_payload_sha256
from oms_hub.anki.domain import ResolvedStageModel, StageUsage
from oms_hub.anki.gaps import GapValidationError, validate_gap_card_fields
from oms_hub.anki.provider_attempts import (
    emit_provider_event,
    finalize_provider_call,
    provider_call_scope,
)
from oms_hub.llm.domain import GeneratedText, GenerationOptions, ProviderName, ThinkingMode
from oms_hub.llm.structured import (
    StructuredJSONResult,
    StructuredOutputError,
    StructuredTextService,
)

ANKING_NOTE_TYPE = "AnKingOverhaul (AnKing Step Deck / AnKingMed)"
MAX_FACT_BYTES = 16_384
MAX_BATCH_BYTES = 65_536
MAX_BATCH_FACTS = 16
_CLOZE = re.compile(r"\{\{c\d+::(.*?)(?:::[^{}]*?)?\}\}", re.IGNORECASE)


class V3Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=50_000)


class V3GenerationFact(BaseModel):
    """One R9 fact, projected with precisely its R3-cited source evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1, max_length=10_000)
    evidence: tuple[V3Evidence, ...] = Field(min_length=1)
    forbidden_cloze_targets: tuple[str, ...] = ()
    generation_allowed: bool

    @field_validator("forbidden_cloze_targets")
    @classmethod
    def _ordered_targets(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("forbidden cloze targets must be sorted and unique")
        if any(not value.strip() for value in values):
            raise ValueError("forbidden cloze targets cannot be blank")
        return values

    @model_validator(mode="after")
    def _fact_size_is_bounded(self) -> V3GenerationFact:
        if _bytes(self.model_dump(mode="json")) > MAX_FACT_BYTES:
            raise ValueError("one R9 fact exceeds 16384 serialized bytes")
        return self


class V3GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    style_profile: str = Field(min_length=1, max_length=200)
    facts: tuple[V3GenerationFact, ...] = Field(min_length=1, max_length=MAX_BATCH_FACTS)

    @model_validator(mode="after")
    def _ordered_and_bounded(self) -> V3GenerationRequest:
        fact_ids = tuple(fact.fact_id for fact in self.facts)
        if fact_ids != tuple(sorted(fact_ids)) or len(fact_ids) != len(set(fact_ids)):
            raise ValueError("R9 facts must be sorted and unique")
        if _bytes(self.provider_document()) > MAX_BATCH_BYTES:
            raise ValueError("R9 batch exceeds 65536 serialized bytes")
        return self

    def provider_document(self) -> dict[str, object]:
        return {
            "serialization_version": "gap-generation-r9-input-v1",
            "policy_style_profile": self.style_profile,
            "facts": [fact.model_dump(mode="json") for fact in self.facts],
        }


class V3GeneratedCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(min_length=1, max_length=300)
    status: Literal["generated"] = "generated"
    text: str = Field(min_length=1, max_length=10_000)
    extra: str = Field(max_length=20_000)
    note_type: Literal["AnKingOverhaul (AnKing Step Deck / AnKingMed)"]
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    split: bool
    split_index: int | None = Field(default=None, ge=1)

    @field_validator("evidence_ids")
    @classmethod
    def _ordered_evidence(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("R9 evidence IDs must be sorted and unique")
        return values


class V3UnresolvedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(min_length=1, max_length=300)
    status: Literal["unresolved"] = "unresolved"
    reason: str = Field(min_length=1, max_length=2_000)


V3GenerationResolution = Annotated[
    V3GeneratedCard | V3UnresolvedFact,
    Field(discriminator="status"),
]


class V3GenerationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resolutions: tuple[V3GenerationResolution, ...]


class V3GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resolutions: tuple[V3GenerationResolution, ...]
    calls: tuple[dict[str, object], ...] = ()
    blocking_error: str | None = None


def generation_options(route: ResolvedStageModel) -> GenerationOptions:
    if route.thinking_mode == "default":
        raise ValueError("R9 route must explicitly declare thinking")
    return GenerationOptions(
        cacheable_source_prefix=None,
        thinking=ThinkingMode.ENABLED
        if route.thinking_mode == "enabled"
        else ThinkingMode.DISABLED,
        temperature=0.0,
        max_tokens=4096,
    )


class R9GenerationService:
    """One bounded generation attempt plus one explicitly authorized repair."""

    def __init__(self, structured: StructuredTextService) -> None:
        self.structured = structured
        self._repair_used = False

    def generate(
        self,
        request: V3GenerationRequest,
        *,
        route: ResolvedStageModel,
        repair_authorization: Mapping[str, object] | None = None,
        rate_table_sha256: str | None = None,
        ordinary_limit_microusd: int | None = None,
        hard_limit_microusd: int | None = None,
        batch_index: int = 0,
    ) -> tuple[V3GenerationResult, StageUsage | None]:
        disabled = [fact for fact in request.facts if not fact.generation_allowed]
        enabled = [fact for fact in request.facts if fact.generation_allowed]
        if disabled and enabled:
            return self._unresolved(
                request, "mixed generation eligibility is not dispatchable"
            ), None
        if disabled:
            return self._unresolved(request, "generation disabled by scoped fact"), None
        options = generation_options(route)
        document = request.provider_document()
        primary: StructuredJSONResult[V3GenerationBatch] | None = None
        try:
            primary = self._call(document, route, options, batch_index, "primary")
            _validate_batch(primary.value, request)
            finalize_provider_call(primary.attempt_handle)
            return self._result(
                primary.value.resolutions, (_call_document(primary),), None
            ), _usage((primary,))
        except (StructuredOutputError, GapValidationError) as exc:
            if primary is not None:
                _contract_failed(primary, str(exc))
            failed_calls = _failed_calls(primary, exc)
            repair = _repair_document(document, exc, primary)
            if _bytes(repair) > MAX_BATCH_BYTES:
                return self._unresolved(
                    request, "R9 repair input exceeds 65536 serialized bytes", failed_calls
                ), _usage_from_call_documents(failed_calls)
            if self._repair_used:
                return self._unresolved(
                    request, "R9 repair already consumed", failed_calls
                ), _usage_from_call_documents(failed_calls)
            if not _repair_authorized(
                repair_authorization,
                canonical_payload_sha256(repair),
                request.policy_sha256,
                rate_table_sha256,
                ordinary_limit_microusd,
                hard_limit_microusd,
            ):
                return self._unresolved(
                    request, "R9 repair is not authorized", failed_calls
                ), _usage_from_call_documents(failed_calls)
            self._repair_used = True
            repaired: StructuredJSONResult[V3GenerationBatch] | None = None
            try:
                repaired = self._call(repair, route, options, batch_index, "repair")
                _validate_batch(repaired.value, request)
                finalize_provider_call(repaired.attempt_handle)
                calls = (*failed_calls, _call_document(repaired))
                return self._result(
                    repaired.value.resolutions, calls, None
                ), _usage_from_call_documents(calls)
            except (StructuredOutputError, GapValidationError) as repair_error:
                repair_calls: tuple[dict[str, object], ...]
                if repaired is not None:
                    _contract_failed(repaired, str(repair_error))
                    repair_calls = (_call_document(repaired),)
                else:
                    repair_calls = _failed_calls(None, repair_error)
                calls = (*failed_calls, *repair_calls)
                return self._unresolved(
                    request, f"R9 repair failed: {repair_error}", calls
                ), _usage_from_call_documents(calls)
            except Exception as repair_error:
                return self._unresolved(
                    request, f"R9 repair transport failure: {repair_error}", failed_calls
                ), _usage_from_call_documents(failed_calls)
        except Exception as exc:
            # Transport faults deliberately do not consume the repair allowance.
            return self._unresolved(request, f"R9 provider transport failure: {exc}"), None

    def _call(
        self,
        document: Mapping[str, object],
        route: ResolvedStageModel,
        options: GenerationOptions,
        ordinal: int,
        kind: Literal["primary", "repair"],
    ) -> StructuredJSONResult[V3GenerationBatch]:
        instruction = (
            "Generate only source-grounded AnKing cloze cards from each supplied fact. "
            "Return exactly one unresolved row, one unsplit card, or sequential split cards "
            "per fact."
            if kind == "primary"
            else "Repair the invalid R9 batch. Return the complete corrected batch only."
        )
        with provider_call_scope(
            batch_index=ordinal,
            batch_note_ids=(),
            kind=kind,
            subcall_ordinal=1 if kind == "repair" else 0,
            defer_acceptance=True,
        ):
            return self.structured.generate_json(
                instruction,
                json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                output_model=V3GenerationBatch,
                provider=ProviderName(route.provider),
                model=route.model,
                options=options,
            )

    @staticmethod
    def _result(
        resolutions: Sequence[V3GenerationResolution],
        calls: Sequence[dict[str, object]],
        error: str | None,
    ) -> V3GenerationResult:
        return V3GenerationResult(
            resolutions=tuple(resolutions),
            calls=tuple(calls),
            blocking_error=error,
        )

    @staticmethod
    def _unresolved(
        request: V3GenerationRequest,
        reason: str,
        calls: Sequence[dict[str, object]] = (),
    ) -> V3GenerationResult:
        return V3GenerationResult(
            resolutions=tuple(
                V3UnresolvedFact(fact_id=fact.fact_id, reason=reason) for fact in request.facts
            ),
            calls=tuple(calls),
            blocking_error=reason,
        )


def _validate_batch(batch: V3GenerationBatch, request: V3GenerationRequest) -> None:
    expected = {fact.fact_id: fact for fact in request.facts}
    grouped: dict[str, list[V3GenerationResolution]] = {fact_id: [] for fact_id in expected}
    for resolution in batch.resolutions:
        if resolution.fact_id not in grouped:
            raise GapValidationError("R9 output names an unrequested fact")
        grouped[resolution.fact_id].append(resolution)
    if any(not rows for rows in grouped.values()):
        raise GapValidationError("R9 output does not partition requested facts")
    for fact_id, rows in grouped.items():
        unresolved = [row for row in rows if isinstance(row, V3UnresolvedFact)]
        cards = [row for row in rows if isinstance(row, V3GeneratedCard)]
        if unresolved and (len(unresolved) != 1 or cards):
            raise GapValidationError("a fact must have exactly one unresolved row or cards")
        if unresolved:
            continue
        if len(cards) == 1:
            if cards[0].split or cards[0].split_index is not None:
                raise GapValidationError("an unsplit R9 card cannot carry split metadata")
        elif not all(card.split for card in cards) or [card.split_index for card in cards] != list(
            range(1, len(cards) + 1)
        ):
            raise GapValidationError("split R9 cards require contiguous indices")
        allowed_evidence = {item.evidence_id for item in expected[fact_id].evidence}
        forbidden = {_normal(value) for value in expected[fact_id].forbidden_cloze_targets}
        for card in cards:
            if set(card.evidence_ids) - allowed_evidence:
                raise GapValidationError("R9 card evidence escapes its fact-cited evidence")
            validate_gap_card_fields(card.text.strip(), card.extra.strip())
            if len(_CLOZE.findall(card.text)) > 2:
                raise GapValidationError("R9 cards cannot contain more than two clozes")
            if any(_normal(answer) in forbidden for answer in _CLOZE.findall(card.text)):
                raise GapValidationError("generated card blanks a forbidden cloze target")


def _repair_authorized(
    authorization: Mapping[str, object] | None,
    request_sha256: str,
    policy_sha256: str,
    rate_table_sha256: str | None,
    ordinary_limit_microusd: int | None,
    hard_limit_microusd: int | None,
) -> bool:
    if (
        authorization is None
        or rate_table_sha256 is None
        or ordinary_limit_microusd is None
        or hard_limit_microusd is None
    ):
        return False
    required = {
        "policy_sha256",
        "rate_table_sha256",
        "estimator_version",
        "repair_request_sha256",
        "predicted_total_before_repair_microusd",
        "predicted_repair_cost_microusd",
        "predicted_total_after_repair_microusd",
        "authorization_sha256",
    }
    if (
        set(authorization) != required
        or authorization.get("policy_sha256") != policy_sha256
        or authorization.get("rate_table_sha256") != rate_table_sha256
        or authorization.get("estimator_version") != ESTIMATOR_VERSION
        or authorization.get("repair_request_sha256") != request_sha256
    ):
        return False
    values = tuple(
        authorization.get(name)
        for name in (
            "predicted_total_before_repair_microusd",
            "predicted_repair_cost_microusd",
            "predicted_total_after_repair_microusd",
        )
    )
    if any(type(value) is not int for value in values):
        return False
    before, repair, total = cast(tuple[int, int, int], values)
    if (
        before < 0
        or repair < 0
        or total < 0
        or before + repair != total
        or total > ordinary_limit_microusd
        or total > hard_limit_microusd
    ):
        return False
    document = {key: value for key, value in authorization.items() if key != "authorization_sha256"}
    return authorization.get("authorization_sha256") == canonical_payload_sha256(document)


def _call_document(value: StructuredJSONResult[V3GenerationBatch]) -> dict[str, object]:
    return {
        "request_id": value.request_id,
        "usage": {
            "input_tokens": value.input_tokens,
            "output_tokens": value.output_tokens,
            "cost_microusd": value.cost_microusd,
            "cache_creation_input_tokens": value.cache_creation_input_tokens,
            "cache_read_input_tokens": value.cache_read_input_tokens,
        },
    }


def _repair_document(
    document: Mapping[str, object],
    error: Exception,
    primary: StructuredJSONResult[V3GenerationBatch] | None,
) -> dict[str, object]:
    raw = getattr(error, "raw_text", None)
    if not isinstance(raw, str):
        raw = primary.raw_text if primary is not None else ""
    return {
        "serialization_version": "gap-generation-r9-repair-v1",
        "generation_input": document,
        "invalid_response": raw,
        "validation_error": str(error),
    }


def _failed_calls(
    primary: StructuredJSONResult[V3GenerationBatch] | None,
    error: Exception,
) -> tuple[dict[str, object], ...]:
    if primary is not None:
        return (_call_document(primary),)
    generation = getattr(error, "generation", None)
    return (_generated_text_document(generation),) if isinstance(generation, GeneratedText) else ()


def _contract_failed(result: StructuredJSONResult[V3GenerationBatch], error: str) -> None:
    emit_provider_event(
        result.attempt_handle,
        "contract_failed",
        request_id=result.request_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_microusd=result.cost_microusd,
        cache_creation_input_tokens=result.cache_creation_input_tokens,
        cache_read_input_tokens=result.cache_read_input_tokens,
        response_text=result.raw_text,
        error=error,
    )


def _generated_text_document(value: GeneratedText) -> dict[str, object]:
    return {
        "request_id": value.request_id,
        "usage": {
            "input_tokens": value.input_tokens,
            "output_tokens": value.output_tokens,
            "cost_microusd": value.cost_microusd,
            "cache_creation_input_tokens": value.cache_creation_input_tokens,
            "cache_read_input_tokens": value.cache_read_input_tokens,
        },
    }


def _usage(calls: Sequence[StructuredJSONResult[V3GenerationBatch]]) -> StageUsage:
    return StageUsage(
        request_id=f"r9:{calls[-1].request_id}",
        input_tokens=sum(call.input_tokens for call in calls),
        output_tokens=sum(call.output_tokens for call in calls),
        cost_microusd=sum(call.cost_microusd for call in calls),
    )


def _usage_from_call_documents(calls: Sequence[Mapping[str, object]]) -> StageUsage | None:
    usage = [item.get("usage") for item in calls]
    typed = [item for item in usage if isinstance(item, Mapping)]
    if not typed:
        return None
    return StageUsage(
        request_id=f"r9:{calls[-1]['request_id']}",
        input_tokens=sum(cast(int, item["input_tokens"]) for item in typed),
        output_tokens=sum(cast(int, item["output_tokens"]) for item in typed),
        cost_microusd=sum(cast(int, item["cost_microusd"]) for item in typed),
    )


def _bytes(value: object) -> int:
    return len(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def _normal(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", value).casefold().split())
