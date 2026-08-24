"""Frozen, bundle-only R7 classification and R8 set coverage for ``card_centric_v3``.

This module deliberately owns the provider boundary.  It accepts only already
constructed :class:`CandidateEvidenceBundle` documents, never a source index
or a retrieval service, so a later caller cannot accidentally attach the full
lecture to an R7 request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    create_model,
    field_validator,
    model_validator,
)

from oms_hub.anki.contracts import canonical_payload_sha256
from oms_hub.anki.domain import ResolvedStageModel, StageUsage
from oms_hub.anki.evidence_bundle import CandidateEvidenceBundle
from oms_hub.anki.provider_attempts import (
    emit_provider_event,
    finalize_provider_call,
    provider_call_scope,
)
from oms_hub.llm.domain import GenerationOptions, ProviderName, ThinkingMode
from oms_hub.llm.structured import StructuredOutputError, StructuredTextService

MAX_PROVIDER_INPUT_BYTES = 65_536
MAX_BUNDLE_BYTES = 16_384
MAX_BUNDLE_TOKENS = 16_384
CLASSIFICATION_OUTPUT_MAX_TOKENS = 3_072
CHEAP_BATCH_MAX = 16
THOROUGH_BATCH_MAX = 8
CLASSIFICATION_CANDIDATES_PER_FACT = 3
SERIAL_CONCURRENCY = 1
MAX_REPAIRS_TOTAL = 1
SET_COVERAGE_FACTS_PER_BATCH = 4
SET_COVERAGE_OUTPUT_MAX_TOKENS = 2_048
ESTIMATOR_VERSION = "utf8-byte-upper-bound-v1"
CLASSIFICATION_CONFIG = {
    "version": "classification-r7-v5",
    "bundle_max_input_bytes": MAX_BUNDLE_BYTES,
    "bundle_max_input_tokens": MAX_BUNDLE_TOKENS,
    "output_max_tokens": CLASSIFICATION_OUTPUT_MAX_TOKENS,
    "cheap_batch_size": CHEAP_BATCH_MAX,
    "thorough_batch_size": THOROUGH_BATCH_MAX,
    "candidates_per_fact": CLASSIFICATION_CANDIDATES_PER_FACT,
    "max_provider_input_bytes": MAX_PROVIDER_INPUT_BYTES,
    "provider_schema_strategy": "batch-derived-enums-v1",
    "serial_concurrency": SERIAL_CONCURRENCY,
    "max_repairs_total": MAX_REPAIRS_TOTAL,
    "defer_partial_to_set_coverage": True,
    "estimator_version": ESTIMATOR_VERSION,
    "thresholds_bps": {
        "strict": {"keep": 9000, "exclude": 7500, "redundant": 9500},
        "balanced": {"keep": 8500, "exclude": 8500, "redundant": 9500},
        "permissive": {"keep": 7500, "exclude": 9000, "redundant": 9500},
    },
}

# These are deliberately local frozen constants: R0 pins their content hashes.
_CANDIDATE_COVERAGE_RULE = (
    " Judge the exact target fact, not the broader concept. Candidate text and extra must "
    "themselves fully state every material claim in the fact; tags are context, not fact "
    "support. Attached passages establish lecture truth only and cannot fill content missing "
    "from the candidate. Treat partial coverage as needs_review in cheap classification or "
    "unresolved in thorough classification, never keep. Cite a passage as conflicting only "
    "when it contradicts candidate content."
)
CHEAP_INSTRUCTION = (
    "Classify each requested candidate only from its attached evidence bundle."
    + _CANDIDATE_COVERAGE_RULE
)
THOROUGH_INSTRUCTION = (
    "Resolve only the supplied candidate bundle and its cheap result." + _CANDIDATE_COVERAGE_RULE
)
REPAIR_INSTRUCTION = (
    "Return a contract-valid replacement for the same supplied bundles." + _CANDIDATE_COVERAGE_RULE
)
SET_COVERAGE_INSTRUCTION = (
    "Decide whether each exact target fact is fully covered by the supplied candidate cards "
    "acting together. Select the smallest sufficient set. Every material claim in the fact must "
    "appear in the selected candidates' text or extra; do not infer missing content. The target "
    "fact is comparison-only and is never support. List every absent target clause in "
    "uncovered_material_claims. Status covered is allowed only when that list is empty. No "
    "lecture passages, tags, or deck hints are supplied."
)


class ClassificationInputError(ValueError):
    """A frozen R7 input is malformed or cannot safely reach a provider."""


class ProviderClassificationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: str = Field(min_length=1, max_length=300)
    candidate_id: str = Field(min_length=1, max_length=300)
    disposition: str = Field(min_length=1, max_length=30)
    confidence_bps: int = Field(ge=0, le=10_000)
    supporting_passage_ids: tuple[str, ...] = ()
    conflicting_passage_ids: tuple[str, ...] = ()
    redundant_with_candidate_id: str | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("supporting_passage_ids", "conflicting_passage_ids")
    @classmethod
    def ordered_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            values != tuple(sorted(values))
            or len(values) != len(set(values))
            or any(not item for item in values)
        ):
            raise ValueError("passage IDs must be sorted, unique, and nonblank")
        return values

    @field_validator("reason")
    @classmethod
    def one_line_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("reason must be one line")
        return value


class ProviderClassificationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: tuple[ProviderClassificationRow, ...]


class ProviderSetCoverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str = Field(min_length=1, max_length=300)
    status: Literal["covered", "missing", "unresolved"]
    selected_candidate_ids: tuple[str, ...] = ()
    confidence_bps: int = Field(ge=0, le=10_000)
    uncovered_material_claims: tuple[
        Annotated[str, Field(min_length=1, max_length=200)], ...
    ] = Field(default=(), max_length=8)

    @field_validator("selected_candidate_ids", mode="before")
    @classmethod
    def canonical_ids(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("set-coverage IDs must be an array")
        values = tuple(value)
        if any(type(item) is not str or not item or item.strip() != item for item in values):
            raise ValueError("set-coverage IDs must be nonblank strings")
        return tuple(sorted(set(values)))

    @field_validator("uncovered_material_claims", mode="before")
    @classmethod
    def canonical_claims(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("uncovered material claims must be an array")
        values = tuple(value)
        if any(type(item) is not str or not item.strip() for item in values):
            raise ValueError("uncovered material claims must be nonblank strings")
        return tuple(sorted({item.strip() for item in values}))

    @model_validator(mode="after")
    def disposition_closure(self) -> ProviderSetCoverageRow:
        if self.status == "covered" and not self.selected_candidate_ids:
            raise ValueError("covered facts require at least one selected candidate")
        if self.status == "missing" and self.selected_candidate_ids:
            raise ValueError("missing facts cannot select candidates")
        return self


class ProviderSetCoverageBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: tuple[ProviderSetCoverageRow, ...]


def _provider_output_model(
    tier: Literal["cheap", "thorough"], bundles: Sequence[CandidateEvidenceBundle]
) -> type[BaseModel]:
    bundle_ids = tuple(bundle.bundle_id for bundle in bundles)
    candidate_ids = tuple(sorted({bundle.candidate.candidate_id for bundle in bundles}))
    passage_ids = tuple(sorted({item for bundle in bundles for item in bundle.allowed_passage_ids}))
    sibling_ids = tuple(
        sorted({item for bundle in bundles for item in bundle.duplicate_sibling_ids})
    )
    dispositions = (
        ("keep", "exclude", "redundant", "needs_review")
        if tier == "cheap"
        else ("keep", "exclude", "redundant", "unresolved")
    )
    row = create_model(
        "ProviderClassificationRow",
        __base__=ProviderClassificationRow,
        bundle_id=(
            Annotated[str, WithJsonSchema({"type": "string", "enum": list(bundle_ids)})],
            ...,
        ),
        candidate_id=(
            Annotated[str, WithJsonSchema({"type": "string", "enum": list(candidate_ids)})],
            ...,
        ),
        disposition=(
            Annotated[str, WithJsonSchema({"type": "string", "enum": list(dispositions)})],
            ...,
        ),
        supporting_passage_ids=(
            Annotated[
                tuple[str, ...],
                WithJsonSchema(
                    {
                        "type": "array",
                        "items": {"type": "string", "enum": list(passage_ids)},
                        "maxItems": len(passage_ids),
                    }
                ),
            ],
            (),
        ),
        conflicting_passage_ids=(
            Annotated[
                tuple[str, ...],
                WithJsonSchema(
                    {
                        "type": "array",
                        "items": {"type": "string", "enum": list(passage_ids)},
                        "maxItems": len(passage_ids),
                    }
                ),
            ],
            (),
        ),
        redundant_with_candidate_id=(
            Annotated[
                str | None,
                WithJsonSchema(
                    {
                        "anyOf": [
                            *(
                                ({"type": "string", "enum": list(sibling_ids)},)
                                if sibling_ids
                                else ()
                            ),
                            {"type": "null"},
                        ]
                    }
                ),
            ],
            None,
        ),
    )
    return create_model(
        "ProviderClassificationBatch",
        __base__=ProviderClassificationBatch,
        rows=(
            tuple[row, ...],
            Field(json_schema_extra={"minItems": len(bundles), "maxItems": len(bundles)}),
        ),
    )


@dataclass(frozen=True, slots=True)
class R7Result:
    payload: dict[str, object]
    usage: StageUsage | None
    blocking_error: str | None


@dataclass(frozen=True, slots=True)
class SetCoverageResult:
    payload: dict[str, object]
    usage: StageUsage | None
    blocking_error: str | None


def instruction_sha256(instruction: str) -> str:
    return hashlib.sha256(instruction.encode("utf-8")).hexdigest()


def options_document(options: GenerationOptions) -> dict[str, object]:
    return {
        "cacheable_source_prefix_sha256": None,
        "thinking": options.thinking.value,
        "thinking_budget_tokens": options.thinking_budget_tokens,
        "temperature": options.temperature,
        "max_tokens": options.max_tokens,
    }


def route_document(route: ResolvedStageModel) -> dict[str, object]:
    return {
        "provider": route.provider,
        "model": route.model,
        "thinking_mode": route.thinking_mode,
        "fixture_validation_signature": route.fixture_validation_signature,
    }


def r7_config_document() -> dict[str, object]:
    return deepcopy(CLASSIFICATION_CONFIG)


def r7_pin_document(
    cheap_route: ResolvedStageModel,
    thorough_route: ResolvedStageModel,
    rate_table_sha256: str,
) -> dict[str, object]:
    """The exact immutable R0 envelope required before R7 can call a provider."""
    if not _sha256(rate_table_sha256):
        raise ClassificationInputError("R7 rate-table hash is invalid")
    cheap_options = options_document(_options(cheap_route, cheap=True))
    thorough_options = options_document(_options(thorough_route, cheap=False))
    config = r7_config_document()
    set_options = options_document(_set_coverage_options(cheap_route))
    return {
        "serialization_version": "classification-r7-pin-v5",
        "instruction_sha256": {
            "cheap": instruction_sha256(CHEAP_INSTRUCTION),
            "thorough": instruction_sha256(THOROUGH_INSTRUCTION),
            "repair": instruction_sha256(REPAIR_INSTRUCTION),
        },
        "provider_output_schema_sha256": canonical_payload_sha256(
            ProviderClassificationBatch.model_json_schema()
        ),
        "cheap_options": cheap_options,
        "cheap_options_sha256": canonical_payload_sha256(cheap_options),
        "thorough_options": thorough_options,
        "thorough_options_sha256": canonical_payload_sha256(thorough_options),
        "classification_config": config,
        "classification_config_sha256": canonical_payload_sha256(config),
        "set_coverage": {
            "instruction_sha256": instruction_sha256(SET_COVERAGE_INSTRUCTION),
            "provider_output_schema_sha256": canonical_payload_sha256(
                ProviderSetCoverageBatch.model_json_schema()
            ),
            "options": set_options,
            "options_sha256": canonical_payload_sha256(set_options),
            "facts_per_batch": SET_COVERAGE_FACTS_PER_BATCH,
            "max_provider_input_bytes": MAX_PROVIDER_INPUT_BYTES,
            "provider_schema_strategy": "batch-derived-enums-v1",
            "route": route_document(cheap_route),
        },
        "estimator_version": ESTIMATOR_VERSION,
        "rate_table_sha256": rate_table_sha256,
        "cheap_route": route_document(cheap_route),
        "thorough_route": route_document(thorough_route),
    }


def r7_audit_envelope(
    cheap_route: ResolvedStageModel,
    thorough_route: ResolvedStageModel,
    rate_table_sha256: str,
    *,
    bundles: Sequence[CandidateEvidenceBundle] = (),
    cheap_rows: Sequence[dict[str, object]] = (),
    escalations: Sequence[dict[str, object]] = (),
    thorough_rows: Sequence[dict[str, object]] = (),
    final_partition: Sequence[dict[str, object]] = (),
    calls: Sequence[dict[str, object]] = (),
    blocking: bool = False,
    partial_diagnostics: Sequence[str] = (),
) -> dict[str, object]:
    """Build the same hashable R7 audit shape for normal and local-blocked paths."""
    config = r7_config_document()
    documents = [bundle.model_dump(mode="json") for bundle in bundles]
    return {
        "classification_config": config,
        "classification_config_sha256": canonical_payload_sha256(config),
        "instruction_sha256": {
            "cheap": instruction_sha256(CHEAP_INSTRUCTION),
            "thorough": instruction_sha256(THOROUGH_INSTRUCTION),
            "repair": instruction_sha256(REPAIR_INSTRUCTION),
        },
        "schema_sha256": canonical_payload_sha256(ProviderClassificationBatch.model_json_schema()),
        "options": {
            "cheap": options_document(_options(cheap_route, cheap=True)),
            "thorough": options_document(_options(thorough_route, cheap=False)),
        },
        "routes": {
            "cheap": route_document(cheap_route),
            "thorough": route_document(thorough_route),
        },
        "estimator_version": ESTIMATOR_VERSION,
        "rate_table_sha256": rate_table_sha256,
        "bundles": documents,
        "bundle_sha256s": [bundle.bundle_sha256 for bundle in bundles],
        "bundles_sha256": canonical_payload_sha256(documents),
        "cheap_rows": list(cheap_rows),
        "escalations": list(escalations),
        "thorough_rows": list(thorough_rows),
        "final_partition": list(final_partition),
        "calls": list(calls),
        "blocking": blocking,
        "partial_diagnostics": list(partial_diagnostics),
    }


def provider_input_bytes(value: Mapping[str, object]) -> int:
    return len(_canonical_json(value).encode("utf-8"))


def pack_bundles(
    bundles: Sequence[CandidateEvidenceBundle], *, max_count: int
) -> tuple[tuple[CandidateEvidenceBundle, ...], ...]:
    """Deterministic greedy batches, retaining caller's already sorted order."""
    packed: list[tuple[CandidateEvidenceBundle, ...]] = []
    current: list[CandidateEvidenceBundle] = []
    for bundle in bundles:
        trial = (*current, bundle)
        if provider_input_bytes(_provider_input("cheap", trial)) > MAX_PROVIDER_INPUT_BYTES:
            if not current:
                raise ClassificationInputError("one R7 bundle exceeds the provider-input bound")
            packed.append(tuple(current))
            current = [bundle]
        elif len(trial) > max_count:
            packed.append(tuple(current))
            current = [bundle]
        else:
            current = list(trial)
    if current:
        packed.append(tuple(current))
    return tuple(packed)


class R7ClassificationService:
    def __init__(self, structured: StructuredTextService) -> None:
        self.structured = structured

    def classify(
        self,
        *,
        bundles: Sequence[CandidateEvidenceBundle],
        strictness: Literal["strict", "balanced", "permissive"],
        cheap_route: ResolvedStageModel,
        thorough_route: ResolvedStageModel,
        repair_authorization: Mapping[str, object] | None = None,
        rate_table_sha256: str | None = None,
        ordinary_limit_microusd: int | None = None,
        hard_limit_microusd: int | None = None,
        defer_partial: bool = False,
    ) -> R7Result:
        ordered = tuple(bundles)
        _validate_bundles(ordered)
        _validate_route(cheap_route, cheap=True)
        _validate_route(thorough_route, cheap=False)
        cheap_rows: list[dict[str, object]] = []
        thorough_rows: list[dict[str, object]] = []
        escalations: list[dict[str, object]] = []
        forced_unresolved: dict[str, dict[str, object]] = {}
        calls: list[dict[str, object]] = []
        blocking: list[str] = []
        repaired = False
        ordinal = 0
        for batch in pack_bundles(ordered, max_count=CHEAP_BATCH_MAX):
            result, evidence, error, repairable, invalid_response, diagnostics = self._call_batch(
                tier="cheap", bundles=batch, route=cheap_route, ordinal=ordinal
            )
            calls.extend(evidence)
            if error is not None and repairable and defer_partial:
                result = {}
                for bundle in batch:
                    diagnostic = f"deferred_to_set_coverage: {error}"
                    escalations.append(
                        _escalation(bundle, "contract_invalid", None, diagnostic)
                    )
                    forced_unresolved[bundle.bundle_id] = _unresolved(
                        bundle, None, diagnostic
                    )
            elif error is not None and repairable:
                repair = self._repair_if_authorized(
                    original_tier="cheap",
                    bundles=batch,
                    route=cheap_route,
                    ordinal=ordinal,
                    error=error,
                    invalid_response=invalid_response,
                    authorization=repair_authorization,
                    policy_sha256=ordered[0].policy_sha256,
                    rate_table_sha256=rate_table_sha256,
                    ordinary_limit_microusd=ordinary_limit_microusd,
                    hard_limit_microusd=hard_limit_microusd,
                    already_repaired=repaired,
                )
                calls.extend(repair[1])
                repaired = repaired or repair[2]
                result, repair_error, diagnostics = repair[0], repair[3], repair[4]
                if repair_error is not None:
                    blocking.append(repair_error)
                    result = {}
                    for bundle in batch:
                        forced_unresolved[bundle.bundle_id] = _unresolved(
                            bundle, None, repair_error
                        )
            elif error is not None:
                blocking.append(error)
                result = {}
                for bundle in batch:
                    forced_unresolved[bundle.bundle_id] = _unresolved(bundle, None, error)
            if result:
                for bundle in batch:
                    raw = result[bundle.bundle_id]
                    diagnostic = diagnostics.get(bundle.bundle_id)
                    if diagnostic is not None:
                        escalations.append(_escalation(bundle, "contract_invalid", raw, diagnostic))
                    else:
                        cheap_rows.append(_row_document(raw))
                        reasons = _cheap_escalation_reasons(_row_document(raw), strictness)
                        if reasons:
                            escalations.append(_escalation(bundle, reasons[0], raw, None, reasons))
            else:
                if not any(bundle.bundle_id in forced_unresolved for bundle in batch):
                    for bundle in batch:
                        escalations.append(
                            _escalation(
                                bundle, "contract_invalid", None, error or "batch unavailable"
                            )
                        )
            ordinal += 1
        by_bundle = {bundle.bundle_id: bundle for bundle in ordered}
        to_escalate = tuple(
            by_bundle[cast(str, item["bundle_id"])]
            for item in escalations
            if item["bundle_id"] not in forced_unresolved
        )
        escalation_by_bundle = {cast(str, item["bundle_id"]): item for item in escalations}
        if defer_partial:
            thorough_rows.extend(
                _unresolved(bundle, None, "deferred_to_set_coverage")
                for bundle in to_escalate
            )
            to_escalate = ()
        for batch in _pack_thorough(to_escalate, escalation_by_bundle):
            result, evidence, error, repairable, invalid_response, diagnostics = self._call_batch(
                tier="thorough",
                bundles=batch,
                route=thorough_route,
                ordinal=ordinal,
                escalation=escalation_by_bundle,
            )
            calls.extend(evidence)
            if error is not None and repairable:
                repair = self._repair_if_authorized(
                    original_tier="thorough",
                    bundles=batch,
                    route=thorough_route,
                    ordinal=ordinal,
                    error=error,
                    invalid_response=invalid_response,
                    authorization=repair_authorization,
                    policy_sha256=ordered[0].policy_sha256,
                    rate_table_sha256=rate_table_sha256,
                    ordinary_limit_microusd=ordinary_limit_microusd,
                    hard_limit_microusd=hard_limit_microusd,
                    already_repaired=repaired,
                    escalation=escalation_by_bundle,
                )
                calls.extend(repair[1])
                repaired = repaired or repair[2]
                result, repair_error, diagnostics = repair[0], repair[3], repair[4]
                if repair_error is not None:
                    blocking.append(repair_error)
                    result = {}
                    for bundle in batch:
                        forced_unresolved[bundle.bundle_id] = _unresolved(
                            bundle, None, repair_error
                        )
            elif error is not None:
                blocking.append(error)
                result = {}
                for bundle in batch:
                    forced_unresolved[bundle.bundle_id] = _unresolved(bundle, None, error)
            for bundle in batch:
                if bundle.bundle_id in forced_unresolved:
                    continue
                thorough_raw = result.get(bundle.bundle_id) if result else None
                if thorough_raw is None:
                    thorough_rows.append(_unresolved(bundle, None, error or "batch unavailable"))
                    continue
                diagnostic = diagnostics.get(bundle.bundle_id)
                if diagnostic is None and not _thorough_escalates(thorough_raw, strictness):
                    thorough_rows.append(_row_document(thorough_raw))
                else:
                    thorough_rows.append(_unresolved(bundle, thorough_raw, diagnostic))
            ordinal += 1
        terminal_cheap = {
            row["bundle_id"]: row
            for row in cheap_rows
            if not _cheap_escalation_reasons(row, strictness)
        }
        terminal_thorough = {row["bundle_id"]: row for row in thorough_rows}
        final = [
            forced_unresolved.get(
                bundle.bundle_id,
                terminal_cheap.get(
                    bundle.bundle_id,
                    terminal_thorough.get(
                        bundle.bundle_id, _unresolved(bundle, None, "unresolved")
                    ),
                ),
            )
            for bundle in ordered
        ]
        usage = _usage(calls)
        payload = r7_audit_envelope(
            cheap_route,
            thorough_route,
            cast(str, rate_table_sha256),
            bundles=ordered,
            cheap_rows=cheap_rows,
            escalations=escalations,
            thorough_rows=thorough_rows,
            final_partition=final,
            calls=calls,
            blocking=bool(blocking),
            partial_diagnostics=blocking,
        )
        payload["defer_partial_to_set_coverage"] = defer_partial
        payload["artifact_sha256"] = canonical_payload_sha256(payload)
        return R7Result(payload, usage, "; ".join(blocking) if blocking else None)

    def _call_batch(
        self,
        *,
        tier: Literal["cheap", "thorough"],
        bundles: Sequence[CandidateEvidenceBundle],
        route: ResolvedStageModel,
        ordinal: int,
        escalation: Mapping[str, object] | None = None,
        kind: Literal["primary", "repair"] = "primary",
        subcall_ordinal: int = 0,
        repair_error: str | None = None,
        invalid_response: object | None = None,
    ) -> tuple[
        dict[str, ProviderClassificationRow],
        list[dict[str, object]],
        str | None,
        bool,
        object | None,
        dict[str, str],
    ]:
        document = _provider_input(
            tier,
            bundles,
            escalation=escalation,
            repair_error=repair_error,
            invalid_response=invalid_response,
        )
        if provider_input_bytes(document) > MAX_PROVIDER_INPUT_BYTES:
            return {}, [], "R7 provider input exceeds 65536 bytes", False, None, {}
        instruction = (
            {"cheap": CHEAP_INSTRUCTION, "thorough": THOROUGH_INSTRUCTION}[tier]
            if kind == "primary"
            else REPAIR_INSTRUCTION
        )
        options = _options(route, cheap=tier == "cheap")
        notes = tuple(sorted({bundle.candidate.note_id for bundle in bundles}))
        try:
            with provider_call_scope(
                batch_index=ordinal,
                batch_note_ids=notes,
                kind=kind,
                subcall_ordinal=subcall_ordinal,
                defer_acceptance=True,
            ):
                generated = self.structured.generate_json(
                    instruction,
                    _canonical_json(document),
                    output_model=_provider_output_model(tier, bundles),
                    provider=ProviderName(route.provider),
                    model=route.model,
                    options=options,
                )
        except StructuredOutputError as exc:
            return {}, [_failed_call(tier, ordinal, kind, exc)], str(exc), True, exc.raw_text, {}
        except Exception as exc:
            return {}, [], f"R7 provider transport failure: {exc}", False, None, {}
        rows = generated.value.rows
        expected = {bundle.bundle_id for bundle in bundles}
        actual = [row.bundle_id for row in rows]
        if (
            set(actual) != expected
            or len(actual) != len(set(actual))
            or len(actual) != len(expected)
        ):
            missing = tuple(
                sorted(
                    bundle.candidate.note_id for bundle in bundles if bundle.bundle_id not in actual
                )
            )
            extra_ids = [
                note_id
                for row in rows
                if row.bundle_id not in expected
                and (note_id := _candidate_note_id(row.candidate_id)) is not None
            ]
            extra = tuple(sorted(extra_ids))
            duplicate = tuple(
                sorted(
                    bundle.candidate.note_id
                    for bundle in bundles
                    if actual.count(bundle.bundle_id) > 1
                )
            )
            emit_provider_event(
                generated.attempt_handle,
                "contract_failed",
                error="R7 response does not partition requested bundles",
                missing_note_ids=missing,
                extra_note_ids=extra,
                duplicate_note_ids=duplicate,
            )
            invalid_response = [_row_document(row) for row in rows]
            return (
                {},
                [_generated_call(tier, ordinal, kind, generated, accepted=False)],
                "R7 response does not partition requested bundles",
                True,
                invalid_response,
                {},
            )
        rows_by_bundle = {row.bundle_id: row for row in rows}
        diagnostics = {
            bundle.bundle_id: diagnostic
            for bundle in bundles
            for valid, diagnostic in (
                _validate_row(rows_by_bundle[bundle.bundle_id], bundle, tier),
            )
            if not valid and diagnostic is not None
        }
        if diagnostics:
            emit_provider_event(
                generated.attempt_handle,
                "contract_failed",
                error="R7 response contains locally invalid rows",
            )
            return (
                rows_by_bundle,
                [_generated_call(tier, ordinal, kind, generated, accepted=False)],
                None,
                False,
                None,
                diagnostics,
            )
        finalize_provider_call(generated.attempt_handle)
        return (
            rows_by_bundle,
            [_generated_call(tier, ordinal, kind, generated, accepted=True)],
            None,
            False,
            None,
            {},
        )

    def _repair_if_authorized(
        self,
        *,
        original_tier: Literal["cheap", "thorough"],
        bundles: Sequence[CandidateEvidenceBundle],
        route: ResolvedStageModel,
        ordinal: int,
        error: str,
        invalid_response: object | None,
        authorization: Mapping[str, object] | None,
        policy_sha256: str,
        rate_table_sha256: str | None,
        ordinary_limit_microusd: int | None,
        hard_limit_microusd: int | None,
        already_repaired: bool,
        escalation: Mapping[str, object] | None = None,
    ) -> tuple[
        dict[str, ProviderClassificationRow],
        list[dict[str, object]],
        bool,
        str | None,
        dict[str, str],
    ]:
        request = _provider_input(
            original_tier,
            bundles,
            escalation=escalation,
            repair_error=error,
            invalid_response=invalid_response,
        )
        if provider_input_bytes(request) > MAX_PROVIDER_INPUT_BYTES:
            return {}, [], False, "R7 repair input exceeds 65536 bytes", {}
        request_sha = canonical_payload_sha256(request)
        if already_repaired:
            return {}, [], False, "R7 repair already consumed", {}
        if not _valid_repair_authorization(
            authorization,
            request_sha,
            policy_sha256,
            rate_table_sha256,
            ordinary_limit_microusd,
            hard_limit_microusd,
        ):
            return {}, [], False, "R7 repair is not authorized", {}
        rows, calls, repair_error, _repairable, _invalid_response, diagnostics = self._call_batch(
            tier=original_tier,
            bundles=bundles,
            route=route,
            ordinal=ordinal,
            escalation=escalation,
            kind="repair",
            subcall_ordinal=1,
            repair_error=error,
            invalid_response=invalid_response,
        )
        return rows, calls, True, repair_error, diagnostics


def classify_set_coverage(
    structured: StructuredTextService,
    *,
    bundles: Sequence[CandidateEvidenceBundle],
    strictness: Literal["strict", "balanced", "permissive"],
    route: ResolvedStageModel,
) -> SetCoverageResult:
    """Make one bounded, fail-closed multi-card decision for each requested fact."""
    ordered = tuple(bundles)
    _validate_bundles(ordered)
    _set_coverage_options(route)
    groups = _set_coverage_groups(ordered)
    calls: list[dict[str, object]] = []
    requests: list[dict[str, object]] = []
    rows: dict[str, dict[str, object]] = {}
    blocking: str | None = None
    try:
        batches = _pack_set_coverage(groups)
    except ClassificationInputError as exc:
        batches, blocking = (), str(exc)
    for ordinal, batch in enumerate(batches):
        document = _set_coverage_input(batch)
        requests.append(
            {
                "batch_index": ordinal,
                "input_sha256": canonical_payload_sha256(document),
                "input_bytes": provider_input_bytes(document),
                "fact_ids": [fact_id for fact_id, _items in batch],
            }
        )
        batch_bundles = tuple(item for _fact_id, items in batch for item in items)
        try:
            with provider_call_scope(
                batch_index=ordinal,
                batch_note_ids=tuple(
                    sorted({item.candidate.note_id for item in batch_bundles})
                ),
                kind="primary",
                defer_acceptance=True,
            ):
                generated = structured.generate_json(
                    SET_COVERAGE_INSTRUCTION,
                    _canonical_json(document),
                    output_model=_set_coverage_output_model(batch),
                    provider=ProviderName(route.provider),
                    model=route.model,
                    options=_set_coverage_options(route),
                )
        except StructuredOutputError as exc:
            calls.append(_failed_call("set_coverage", ordinal, "primary", exc))
            blocking = str(exc)
            break
        except Exception as exc:
            blocking = f"R8 set-coverage provider transport failure: {exc}"
            break
        error = _validate_set_coverage_rows(generated.value.rows, batch)
        if error is not None:
            emit_provider_event(generated.attempt_handle, "contract_failed", error=error)
            calls.append(
                _generated_call("set_coverage", ordinal, "primary", generated, accepted=False)
            )
            blocking = error
            break
        finalize_provider_call(generated.attempt_handle)
        calls.append(_generated_call("set_coverage", ordinal, "primary", generated, accepted=True))
        for row in generated.value.rows:
            document_row = row.model_dump(mode="json")
            uncovered = row.uncovered_material_claims
            if row.status == "covered" and uncovered:
                document_row["provider_status"] = row.status
                document_row["status"] = "unresolved"
                document_row["diagnostic"] = "provider reported uncovered material claims"
            elif row.status == "missing" and not uncovered:
                document_row["provider_status"] = row.status
                document_row["status"] = "unresolved"
                document_row["diagnostic"] = "missing result omitted uncovered material claims"
            threshold_key = "keep" if row.status == "covered" else "exclude"
            threshold = cast(
                Mapping[str, int],
                CLASSIFICATION_CONFIG["thresholds_bps"][strictness],  # type: ignore[index]
            ).get(threshold_key, 10_001)
            if document_row["status"] != "unresolved" and row.confidence_bps < threshold:
                document_row["provider_status"] = row.status
                document_row["status"] = "unresolved"
                document_row["diagnostic"] = "set-coverage confidence is below policy threshold"
            rows[row.fact_id] = document_row
    final = _set_coverage_partition(ordered, rows)
    payload: dict[str, object] = {
        "instruction_sha256": instruction_sha256(SET_COVERAGE_INSTRUCTION),
        "schema_sha256": canonical_payload_sha256(ProviderSetCoverageBatch.model_json_schema()),
        "options": options_document(_set_coverage_options(route)),
        "route": route_document(route),
        "facts_per_batch": SET_COVERAGE_FACTS_PER_BATCH,
        "max_provider_input_bytes": MAX_PROVIDER_INPUT_BYTES,
        "requests": requests,
        "rows": [rows[fact_id] for fact_id, _items in groups if fact_id in rows],
        "final_partition": final,
        "calls": calls,
        "blocking": blocking is not None,
        "partial_diagnostics": [] if blocking is None else [blocking],
    }
    payload["artifact_sha256"] = canonical_payload_sha256(payload)
    return SetCoverageResult(payload, _usage(calls), blocking)


def _set_coverage_options(route: ResolvedStageModel) -> GenerationOptions:
    if route.thinking_mode != "disabled":
        raise ClassificationInputError("R8 set coverage requires disabled thinking")
    return GenerationOptions(
        cacheable_source_prefix=None,
        thinking=ThinkingMode.DISABLED,
        temperature=0.0,
        max_tokens=SET_COVERAGE_OUTPUT_MAX_TOKENS,
    )


def _set_coverage_groups(
    bundles: Sequence[CandidateEvidenceBundle],
) -> tuple[tuple[str, tuple[CandidateEvidenceBundle, ...]], ...]:
    grouped: dict[str, list[CandidateEvidenceBundle]] = {}
    for bundle in bundles:
        grouped.setdefault(bundle.fact_id, []).append(bundle)
    result = tuple((fact_id, tuple(items)) for fact_id, items in grouped.items())
    for fact_id, items in result:
        first = items[0]
        fact = next(item for item in first.concept.facts if item.fact_id == fact_id)
        passages = tuple(item.model_dump(mode="json") for item in first.selected_passages)
        if any(
            next(value for value in bundle.concept.facts if value.fact_id == fact_id) != fact
            or tuple(item.model_dump(mode="json") for item in bundle.selected_passages) != passages
            for bundle in items[1:]
        ):
            raise ClassificationInputError("set-coverage bundles disagree on fact evidence")
    return result


def _set_coverage_input(
    groups: Sequence[tuple[str, Sequence[CandidateEvidenceBundle]]],
) -> dict[str, object]:
    facts: list[dict[str, object]] = []
    for fact_id, bundles in groups:
        first = bundles[0]
        fact = next(item for item in first.concept.facts if item.fact_id == fact_id)
        facts.append(
            {
                "fact_id": fact_id,
                "statement": fact.statement,
                "candidates": [
                    {
                        "candidate_id": item.candidate.candidate_id,
                        "note_id": item.candidate.note_id,
                        "text": item.candidate.text,
                        "extra": item.candidate.extra,
                    }
                    for item in bundles
                ],
            }
        )
    return {"serialization_version": "set-coverage-provider-input-v2", "facts": facts}


def _pack_set_coverage(
    groups: Sequence[tuple[str, tuple[CandidateEvidenceBundle, ...]]],
) -> tuple[tuple[tuple[str, tuple[CandidateEvidenceBundle, ...]], ...], ...]:
    packed: list[tuple[tuple[str, tuple[CandidateEvidenceBundle, ...]], ...]] = []
    current: list[tuple[str, tuple[CandidateEvidenceBundle, ...]]] = []
    for group in groups:
        trial = (*current, group)
        if (
            len(trial) > SET_COVERAGE_FACTS_PER_BATCH
            or provider_input_bytes(_set_coverage_input(trial)) > MAX_PROVIDER_INPUT_BYTES
        ):
            if not current:
                raise ClassificationInputError("one R8 set-coverage fact exceeds 65536 bytes")
            packed.append(tuple(current))
            current = [group]
        else:
            current = list(trial)
    if current:
        packed.append(tuple(current))
    return tuple(packed)


def _set_coverage_output_model(
    groups: Sequence[tuple[str, Sequence[CandidateEvidenceBundle]]],
) -> type[BaseModel]:
    fact_ids = tuple(fact_id for fact_id, _items in groups)
    candidate_ids = tuple(
        sorted({item.candidate.candidate_id for _fact_id, items in groups for item in items})
    )
    row = create_model(
        "ProviderSetCoverageRow",
        __base__=ProviderSetCoverageRow,
        fact_id=(Annotated[str, WithJsonSchema({"type": "string", "enum": list(fact_ids)})], ...),
        selected_candidate_ids=(
            Annotated[
                tuple[str, ...],
                WithJsonSchema(
                    {
                        "type": "array",
                        "items": {"type": "string", "enum": list(candidate_ids)},
                        "maxItems": len(candidate_ids),
                    }
                ),
            ],
            (),
        ),
    )
    return create_model(
        "ProviderSetCoverageBatch",
        __base__=ProviderSetCoverageBatch,
        rows=(
            tuple[row, ...],
            Field(json_schema_extra={"minItems": len(groups), "maxItems": len(groups)}),
        ),
    )


def _validate_set_coverage_rows(
    rows: Sequence[ProviderSetCoverageRow],
    groups: Sequence[tuple[str, Sequence[CandidateEvidenceBundle]]],
) -> str | None:
    expected = {fact_id for fact_id, _items in groups}
    actual = [row.fact_id for row in rows]
    if set(actual) != expected or len(actual) != len(set(actual)) or len(actual) != len(expected):
        return "R8 set-coverage response does not partition requested facts"
    by_fact = {fact_id: items for fact_id, items in groups}
    for row in rows:
        bundles = by_fact[row.fact_id]
        if not set(row.selected_candidate_ids) <= {
            item.candidate.candidate_id for item in bundles
        }:
            return "R8 set-coverage response escapes requested candidates"
    return None


def _set_coverage_partition(
    bundles: Sequence[CandidateEvidenceBundle], rows: Mapping[str, Mapping[str, object]]
) -> list[dict[str, object]]:
    partition: list[dict[str, object]] = []
    for bundle in bundles:
        row = rows.get(bundle.fact_id)
        selected = set(cast(Sequence[str], row.get("selected_candidate_ids", ()))) if row else set()
        status = row.get("status") if row else "unresolved"
        uncovered = (
            tuple(cast(Sequence[str], row.get("uncovered_material_claims", ())))
            if row
            else ()
        )
        disposition = (
            "keep"
            if status == "covered" and bundle.candidate.candidate_id in selected
            else "exclude"
            if status in {"covered", "missing"}
            else "unresolved"
        )
        partition.append(
            {
                "bundle_id": bundle.bundle_id,
                "candidate_id": bundle.candidate.candidate_id,
                "disposition": disposition,
                "confidence_bps": 0 if row is None else row["confidence_bps"],
                "supporting_passage_ids": [] if row is None else list(bundle.allowed_passage_ids),
                "conflicting_passage_ids": [],
                "redundant_with_candidate_id": None,
                "reason": (
                    "set coverage unavailable"
                    if row is None
                    else "provider reported complete candidate-set coverage"
                    if status == "covered"
                    else "uncovered material claims: " + "; ".join(uncovered)
                    if uncovered
                    else f"provider reported {status}"
                ),
                "uncovered_material_claims": list(uncovered),
                "diagnostic": (
                    "caller-authored unresolved" if row is None else row.get("diagnostic")
                ),
            }
        )
    return partition


def _options(route: ResolvedStageModel, *, cheap: bool) -> GenerationOptions:
    if cheap and route.thinking_mode != "disabled":
        raise ClassificationInputError("cheap R7 route requires disabled thinking")
    if not cheap and route.thinking_mode == "default":
        raise ClassificationInputError("thorough R7 route must explicitly declare thinking")
    return GenerationOptions(
        cacheable_source_prefix=None,
        thinking=ThinkingMode.ENABLED
        if route.thinking_mode == "enabled"
        else ThinkingMode.DISABLED,
        temperature=0.0,
        max_tokens=CLASSIFICATION_OUTPUT_MAX_TOKENS,
    )


def _validate_route(route: ResolvedStageModel, *, cheap: bool) -> None:
    _options(route, cheap=cheap)


def _validate_bundles(bundles: Sequence[CandidateEvidenceBundle]) -> None:
    if not bundles:
        return
    identities = tuple(
        (item.concept.concept_id, item.fact_id, item.candidate.note_id) for item in bundles
    )
    if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
        raise ClassificationInputError(
            "R7 bundles must be sorted and unique by concept/fact/representative"
        )
    if any(
        item.max_input_bytes != MAX_BUNDLE_BYTES
        or item.max_input_tokens != MAX_BUNDLE_TOKENS
        or item.input_token_estimate != item.input_byte_estimate
        or item.truncated
        for item in bundles
    ):
        raise ClassificationInputError("R7 bundles must use exact untruncated 16384-byte estimates")
    if (
        len({item.policy_sha256 for item in bundles}) != 1
        or len({item.scope_sha256 for item in bundles}) != 1
    ):
        raise ClassificationInputError("R7 bundles must share one frozen policy and scope")


def _provider_input(
    tier: str,
    bundles: Sequence[CandidateEvidenceBundle],
    *,
    escalation: Mapping[str, object] | None = None,
    repair_error: str | None = None,
    invalid_response: object | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "serialization_version": "classification-provider-input-v1",
        "tier": tier,
        "bundles": [bundle.model_dump(mode="json") for bundle in bundles],
    }
    if escalation is not None:
        value["cheap_escalations"] = [escalation[bundle.bundle_id] for bundle in bundles]
    if repair_error is not None:
        value["repair_error"] = repair_error[:1000]
    if invalid_response is not None:
        value["invalid_response"] = invalid_response
    return value


def _pack_thorough(
    bundles: Sequence[CandidateEvidenceBundle], escalation: Mapping[str, object]
) -> tuple[tuple[CandidateEvidenceBundle, ...], ...]:
    packed: list[tuple[CandidateEvidenceBundle, ...]] = []
    current: list[CandidateEvidenceBundle] = []
    for bundle in bundles:
        trial = (*current, bundle)
        input_document = _provider_input("thorough", trial, escalation=escalation)
        if provider_input_bytes(input_document) > MAX_PROVIDER_INPUT_BYTES:
            if not current:
                raise ClassificationInputError(
                    "one R7 thorough bundle exceeds the provider-input bound"
                )
            packed.append(tuple(current))
            current = [bundle]
        elif len(trial) > THOROUGH_BATCH_MAX:
            packed.append(tuple(current))
            current = [bundle]
        else:
            current = list(trial)
    if current:
        packed.append(tuple(current))
    return tuple(packed)


def _validate_row(
    row: ProviderClassificationRow | None, bundle: CandidateEvidenceBundle, tier: str
) -> tuple[bool, str | None]:
    if row is None:
        return False, "missing provider row"
    allowed = (
        {"keep", "exclude", "redundant", "needs_review"}
        if tier == "cheap"
        else {"keep", "exclude", "redundant", "unresolved"}
    )
    if row.bundle_id != bundle.bundle_id or row.candidate_id != bundle.candidate.candidate_id:
        return False, "provider row identity is not the requested bundle/candidate"
    if row.disposition not in allowed:
        return False, "provider disposition is invalid for tier"
    support, conflict = set(row.supporting_passage_ids), set(row.conflicting_passage_ids)
    if (
        not support
        and not conflict
        or support & conflict
        or not (support | conflict) <= set(bundle.allowed_passage_ids)
    ):
        return False, "provider citations are not a valid bundle subset"
    if row.disposition == "redundant":
        if row.redundant_with_candidate_id not in set(bundle.duplicate_sibling_ids):
            return False, "redundancy target is not an attached sibling"
    elif row.redundant_with_candidate_id is not None:
        return False, "non-redundant row carries a redundancy target"
    return True, None


def _cheap_escalation_reasons(row: Mapping[str, object], strictness: str) -> tuple[str, ...]:
    reasons: list[str] = []
    if row["disposition"] == "needs_review":
        reasons.append("cheap_needs_review")
    thresholds = cast(
        Mapping[str, int],
        CLASSIFICATION_CONFIG["thresholds_bps"][strictness],  # type: ignore[index]
    )
    confidence = cast(int, row["confidence_bps"])
    if confidence < thresholds.get(str(row["disposition"]), 10_001):
        reasons.append("low_confidence")
    if row["conflicting_passage_ids"]:
        reasons.append("conflicting_evidence")
    return tuple(reasons)


def _thorough_escalates(row: ProviderClassificationRow, strictness: str) -> bool:
    return (
        row.disposition == "unresolved"
        or bool(row.conflicting_passage_ids)
        or bool(_cheap_escalation_reasons(_row_document(row), strictness))
    )


def _escalation(
    bundle: CandidateEvidenceBundle,
    reason: str,
    raw: ProviderClassificationRow | None,
    diagnostic: str | None,
    reasons: Sequence[str] | None = None,
) -> dict[str, object]:
    return {
        "bundle_id": bundle.bundle_id,
        "candidate_id": bundle.candidate.candidate_id,
        "reasons": list(reasons or (reason,)),
        "cheap_row": _row_document(raw) if raw else None,
        "diagnostic": diagnostic,
    }


def _row_document(row: ProviderClassificationRow) -> dict[str, object]:
    return row.model_dump(mode="json")


def _unresolved(
    bundle: CandidateEvidenceBundle, raw: ProviderClassificationRow | None, diagnostic: str | None
) -> dict[str, object]:
    return {
        "bundle_id": bundle.bundle_id,
        "candidate_id": bundle.candidate.candidate_id,
        "disposition": "unresolved",
        "confidence_bps": 0,
        "supporting_passage_ids": [],
        "conflicting_passage_ids": [],
        "redundant_with_candidate_id": None,
        "reason": "caller-authored unresolved",
        "raw_provider_row": _row_document(raw) if raw else None,
        "diagnostic": diagnostic,
    }


def _generated_call(
    tier: str, ordinal: int, kind: str, generated: Any, *, accepted: bool
) -> dict[str, object]:
    return {
        "tier": tier,
        "batch_index": ordinal,
        "kind": kind,
        "request_id": generated.request_id,
        "usage": {
            "input_tokens": generated.input_tokens,
            "output_tokens": generated.output_tokens,
            "cost_microusd": generated.cost_microusd,
            "cache_creation_input_tokens": generated.cache_creation_input_tokens,
            "cache_read_input_tokens": generated.cache_read_input_tokens,
        },
        "accepted": accepted,
    }


def _failed_call(
    tier: str, ordinal: int, kind: str, error: StructuredOutputError
) -> dict[str, object]:
    generation = error.generation
    return {
        "tier": tier,
        "batch_index": ordinal,
        "kind": kind,
        "request_id": generation.request_id,
        "usage": {
            "input_tokens": generation.input_tokens,
            "output_tokens": generation.output_tokens,
            "cost_microusd": generation.cost_microusd,
            "cache_creation_input_tokens": generation.cache_creation_input_tokens,
            "cache_read_input_tokens": generation.cache_read_input_tokens,
        },
        "accepted": False,
    }


def _usage(calls: Sequence[Mapping[str, object]]) -> StageUsage | None:
    if not calls:
        return None
    usage = [
        cast(Mapping[str, object], call["usage"])
        for call in calls
        if isinstance(call.get("usage"), Mapping)
    ]
    if not usage:
        return None
    return StageUsage(
        "r7-aggregate",
        sum(cast(int, item["input_tokens"]) for item in usage),
        sum(cast(int, item["output_tokens"]) for item in usage),
        sum(cast(int, item["cost_microusd"]) for item in usage),
    )


def _valid_repair_authorization(
    authorization: Mapping[str, object] | None,
    request_sha256: str,
    policy_sha256: str,
    rate_table_sha256: str | None,
    ordinary: int | None,
    hard: int | None,
) -> bool:
    if authorization is None or not rate_table_sha256 or ordinary is None or hard is None:
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
    before, repair, after = cast(tuple[int, int, int], values)
    if (
        min(before, repair, after) < 0
        or before + repair != after
        or after > ordinary
        or after > hard
    ):
        return False
    document = {key: value for key, value in authorization.items() if key != "authorization_sha256"}
    return authorization.get("authorization_sha256") == canonical_payload_sha256(document)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _candidate_note_id(value: str) -> int | None:
    prefix, separator, raw = value.partition(":")
    if prefix != "note" or separator != ":" or not raw.isdecimal():
        return None
    note_id = int(raw)
    return note_id if note_id > 0 else None


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
