"""Deterministic S1/S3 and batched S4 services for card_centric_v1."""

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from oms_hub.anki.card_centric_contracts import (
    CardCentricPassage,
    CardCentricSourceIndex,
    CardClassification,
    CardClassificationBatchOutput,
    CardConcept,
    CardConceptLedger,
    CardConceptRepairBatch,
    CardRecord,
    CensusTrust,
    ClassifierBatchAudit,
    ClassifierResult,
    ClassifierTelemetry,
    FastCardClassification,
    GeneratedCardResolution,
    QualitySelectionResult,
    SnapshotCensus,
    TagScopeResult,
    card_fact_structure_defects,
    card_ledger_concept_defects,
)
from oms_hub.anki.correction_contracts import (
    CanonicalJsonObject,
    EvidenceQuality,
    MarginalValueReason,
    SelectionMetadata,
    SelectionTier,
)
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.normalize import normalize_html
from oms_hub.anki.provider_attempts import emit_provider_event, provider_call_scope
from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.anthropic import resolve_anthropic_model_capabilities
from oms_hub.llm.domain import (
    GeneratedText,
    GenerationOptions,
    LLMRequestError,
    ProviderCapabilities,
    ProviderName,
    ThinkingMode,
)
from oms_hub.llm.structured import (
    StructuredJSONResult,
    StructuredOutputError,
    StructuredTextService,
)


class CardCentricValidationError(ValueError):
    """A card-centric artifact or model response cannot be trusted."""


@dataclass(frozen=True, slots=True)
class CardCentricLedgerResult:
    ledger: CardConceptLedger
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    cache_prefix_sha256: str
    generation_parameters: dict[str, object]
    generation_parameters_sha256: str
    request_ids: tuple[str, ...]
    repair_manifest: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _LedgerRepairContext:
    document: dict[str, object]
    raw_concepts: tuple[dict[str, object], ...]
    index_to_id: dict[int, str]
    defects_by_id: dict[str, tuple[str, ...]]
    required_additions: tuple[dict[str, str], ...]
    available_addition_ids: tuple[str, ...]
    unchanged_hashes: dict[str, str]
    scope_limit: int
    primary_response_sha256: str


@dataclass(frozen=True, slots=True)
class CardCentricLedgerAttempt:
    """One auditable provider invocation inside the bounded S2 operation."""

    call_index: Literal[1, 2]
    kind: Literal["primary", "repair"]
    outcome: Literal["accepted", "validation_failed", "transport_failed"]
    provider: ProviderName
    model: str
    instruction_sha256: str
    generation_parameters: dict[str, object]
    generation_parameters_sha256: str
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    validation_error: str | None
    invalid_response_sha256: str | None
    invalid_response: str | None
    diagnostic_source: str | None = None
    http_status: int | None = None


_S2_GENERATION_OPTIONS = GenerationOptions(
    thinking=ThinkingMode.DISABLED,
    temperature=0,
    max_tokens=7000,
)
_S2_INVALID_RESPONSE_LIMIT = 12_000


@dataclass(slots=True)
class CardCentricLedgerService:
    """One cached-prefix S2 call.  The returned ledger is not a retrieval index."""

    structured: StructuredTextService
    instruction: str

    def generate(
        self,
        *,
        source_index: CardCentricSourceIndex,
        provider: ProviderName,
        model: str,
        record_attempt: Callable[[CardCentricLedgerAttempt], None] | None = None,
    ) -> CardCentricLedgerResult:
        summary_prefix = "\n\n".join(
            f'<passage id="{passage.passage_id}">\n{passage.text}\n</passage>'
            for passage in source_index.passages
            if passage.authority == "summary"
        )
        if not summary_prefix:
            raise CardCentricValidationError("ledger requires summary passages")
        depth_control_evidence = _depth_control_evidence(source_index)
        input_text = json.dumps(
            {
                "summary_passages": [
                    {"passage_id": passage.passage_id, "text": passage.text}
                    for passage in source_index.passages
                    if passage.authority == "summary"
                ],
                "depth_control_evidence": depth_control_evidence,
                "contract": "coverage_checklist_only",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        options = GenerationOptions(
            cacheable_source_prefix=summary_prefix,
            thinking=_S2_GENERATION_OPTIONS.thinking,
            temperature=_S2_GENERATION_OPTIONS.temperature,
            max_tokens=_S2_GENERATION_OPTIONS.max_tokens,
        )
        parameters = s2_generation_parameters(provider, model)
        parameters_sha256 = _canonical_sha256(parameters)
        request_ids: tuple[str, ...]
        repair_manifest = None
        try:
            with provider_call_scope(batch_index=0, subcall_ordinal=0):
                result = self.structured.generate_json(
                    self.instruction,
                    input_text,
                    output_model=CardConceptLedger,
                    provider=provider,
                    model=model,
                    # S2 caches the summary and receives only bounded passages that
                    # substantiate entities named by summary depth controls.
                    options=options,
                )
                _validate_ledger_depth_controls(result, source_index)
                ledger = result.value
        except StructuredOutputError as error:
            _record_ledger_attempt(
                record_attempt,
                call_index=1,
                kind="primary",
                outcome="validation_failed",
                provider=provider,
                model=model,
                instruction=self.instruction,
                parameters=parameters,
                parameters_sha256=parameters_sha256,
                error=error,
            )
            try:
                repair_context = _ledger_repair_context(
                    error.raw_text,
                    source_index,
                    depth_control_evidence,
                )
            except CardCentricValidationError:
                raise error from None
            repair_instruction = _ledger_repair_instruction(self.instruction)
            repair_input = json.dumps(
                _repair_request_document(
                    repair_context, source_index, depth_control_evidence
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
            try:
                with provider_call_scope(batch_index=0, kind="repair", subcall_ordinal=0):
                    repair_result = self.structured.generate_json(
                        repair_instruction,
                        repair_input,
                        output_model=CardConceptRepairBatch,
                        provider=provider,
                        model=model,
                        options=options,
                    )
                    ledger, repair_manifest = _merge_targeted_ledger_repair(
                        repair_context,
                        repair_result,
                        source_index,
                        depth_control_evidence,
                    )
            except StructuredOutputError as repair_error:
                _record_ledger_attempt(
                    record_attempt,
                    call_index=2,
                    kind="repair",
                    outcome="validation_failed",
                    provider=provider,
                    model=model,
                    instruction=repair_instruction,
                    parameters=parameters,
                    parameters_sha256=parameters_sha256,
                    error=repair_error,
                )
                raise
            except Exception as repair_error:
                _record_ledger_attempt(
                    record_attempt,
                    call_index=2,
                    kind="repair",
                    outcome="transport_failed",
                    provider=provider,
                    model=model,
                    instruction=repair_instruction,
                    parameters=parameters,
                    parameters_sha256=parameters_sha256,
                    transport_error=repair_error,
                )
                raise
            _record_ledger_attempt(
                record_attempt,
                call_index=2,
                kind="repair",
                outcome="accepted",
                provider=provider,
                model=model,
                instruction=repair_instruction,
                parameters=parameters,
                parameters_sha256=parameters_sha256,
                result=repair_result,
            )
            request_ids = (error.generation.request_id, repair_result.request_id)
            request_id = _combined_request_id(request_ids)
            input_tokens = error.generation.input_tokens + repair_result.input_tokens
            output_tokens = error.generation.output_tokens + repair_result.output_tokens
            cost_microusd = error.generation.cost_microusd + repair_result.cost_microusd
        except Exception as primary_error:
            _record_ledger_attempt(
                record_attempt,
                call_index=1,
                kind="primary",
                outcome="transport_failed",
                provider=provider,
                model=model,
                instruction=self.instruction,
                parameters=parameters,
                parameters_sha256=parameters_sha256,
                transport_error=primary_error,
            )
            raise
        else:
            _record_ledger_attempt(
                record_attempt,
                call_index=1,
                kind="primary",
                outcome="accepted",
                provider=provider,
                model=model,
                instruction=self.instruction,
                parameters=parameters,
                parameters_sha256=parameters_sha256,
                result=result,
            )
            request_ids = (result.request_id,)
            request_id = result.request_id
            input_tokens = result.input_tokens
            output_tokens = result.output_tokens
            cost_microusd = result.cost_microusd
        return CardCentricLedgerResult(
            ledger=ledger,
            request_id=request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
            cache_prefix_sha256=hashlib.sha256(summary_prefix.encode()).hexdigest(),
            generation_parameters=parameters,
            generation_parameters_sha256=parameters_sha256,
            request_ids=request_ids,
            repair_manifest=repair_manifest,
        )


_DEPTH_CONTROL = re.compile(
    r"^\s*(?P<entities>[^:\n]+):\s*(?P<depth>DEEP|MEDIUM|SURFACE)\b",
    re.IGNORECASE,
)
_AWARENESS_SIGNAL = re.compile(
    r"\b(?:keep (?:these|them|it) in mind|"
    r"know (?:that )?(?:they|these|it) exist)\b",
    re.IGNORECASE,
)
_LIST_THRESHOLD = re.compile(r"(?:\bat least\b|≥)\s*\d+", re.IGNORECASE)


def _validate_ledger_depth_controls(
    result: StructuredJSONResult[CardConceptLedger],
    source_index: CardCentricSourceIndex,
) -> None:
    """Require every entity named by summary depth controls at that depth."""
    missing = _missing_ledger_depth_controls(result.value, source_index)
    awareness_defects = _ledger_awareness_defects(
        result.value,
        _depth_control_evidence(source_index),
    )
    if not missing and not awareness_defects:
        return
    messages = []
    if missing:
        messages.append("ledger omitted named depth-control entities: " + ", ".join(missing))
    messages.extend(awareness_defects)
    message = "; ".join(messages)
    raise StructuredOutputError(
        message,
        raw_text=result.raw_text,
        generation=GeneratedText(
            text=result.raw_text,
            provider=result.provider,
            model=result.model,
            request_id=result.request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_microusd=result.cost_microusd,
            cache_creation_input_tokens=result.cache_creation_input_tokens,
            cache_read_input_tokens=result.cache_read_input_tokens,
        ),
        attempt_handle=result.attempt_handle,
    )


def _depth_controls(
    source_index: CardCentricSourceIndex,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (entity.strip(), match.group("depth").casefold())
        for passage in source_index.passages
        if passage.authority == "summary"
        if (match := _DEPTH_CONTROL.match(passage.text)) is not None
        for entity in match.group("entities").split(",")
        if entity.strip()
    )


def _depth_control_evidence(
    source_index: CardCentricSourceIndex,
) -> list[dict[str, str]]:
    evidence = []
    for entity, depth in _depth_controls(source_index):
        needle = _normalized_entity_text(entity)
        if not re.search(r"\b(?:warning signs?|checklists?|criteria)\b", needle):
            continue
        matches = sorted(
            (
                passage
                for passage in source_index.passages
                if passage.authority != "summary"
                and set(needle.split())
                <= set(_normalized_entity_text(passage.text).split())
            ),
            key=lambda passage: (passage.authority != "slide", passage.passage_id),
        )[:2]
        representation = (
            "awareness_only"
            if any(_AWARENESS_SIGNAL.search(passage.text) for passage in matches)
            else "item_recall"
        )
        evidence.extend(
            {
                "required_entity": entity,
                "depth": depth,
                "passage_id": passage.passage_id,
                "text": passage.text,
                "representation": representation,
            }
            for passage in matches
        )
    return evidence


def _normalized_entity_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _awareness_only_entities(
    depth_control_evidence: Sequence[Mapping[str, str]],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item["required_entity"]
            for item in depth_control_evidence
            if item.get("representation") == "awareness_only"
        )
    )


def _awareness_concept_defects(
    *,
    concept_id: str,
    corpus: str,
    fact_descriptions: tuple[str, ...],
    depth_control_evidence: Sequence[Mapping[str, str]],
) -> tuple[str, ...]:
    for entity in _awareness_only_entities(depth_control_evidence):
        if _normalized_entity_text(entity) not in _normalized_entity_text(corpus):
            continue
        enumerated = any(
            len(_LIST_THRESHOLD.findall(fact)) >= 2 or fact.count(",") >= 4
            for fact in fact_descriptions
        )
        if len(fact_descriptions) != 1 or enumerated:
            return (
                f"{concept_id}: awareness-only list must be one compact recognition "
                "fact without enumerated items or thresholds",
            )
    return ()


def _ledger_awareness_defects(
    ledger: CardConceptLedger,
    depth_control_evidence: Sequence[Mapping[str, str]],
) -> tuple[str, ...]:
    return tuple(
        defect
        for concept in ledger.concepts
        for defect in _awareness_concept_defects(
            concept_id=concept.concept_id,
            corpus=" ".join(
                (
                    concept.primary_entity,
                    *concept.aliases,
                    concept.canonical_statement,
                    *concept.fact_descriptions,
                )
            ),
            fact_descriptions=concept.fact_descriptions,
            depth_control_evidence=depth_control_evidence,
        )
    )


def _missing_ledger_depth_controls(
    ledger: CardConceptLedger,
    source_index: CardCentricSourceIndex,
) -> tuple[str, ...]:
    return tuple(
        f"{entity} ({depth})"
        for entity, depth in _depth_controls(source_index)
        if not any(
            concept.depth == depth
            and _normalized_entity_text(entity)
            in _normalized_entity_text(
                " ".join(
                    (
                        concept.primary_entity,
                        *concept.aliases,
                        concept.canonical_statement,
                        *concept.fact_descriptions,
                    )
                )
            )
            for concept in ledger.concepts
        )
    )


_RAW_JSON_FENCE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CONCEPT_ID = re.compile(r"^C[0-9]{2,4}$")


def _canonical_object_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _raw_concept_corpus(raw: Mapping[str, object]) -> str:
    values = []
    for name in ("primary_entity", "canonical_statement"):
        value = raw.get(name)
        if isinstance(value, str):
            values.append(value)
    for name in ("aliases", "fact_descriptions"):
        value = raw.get(name)
        if isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    return _normalized_entity_text(" ".join(values))


def _parse_targetable_ledger(
    raw_text: str,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    match = _RAW_JSON_FENCE.fullmatch(raw_text)
    text = match.group("body") if match is not None else raw_text
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CardCentricValidationError("invalid JSON cannot be targeted for repair") from exc
    if not isinstance(document, dict) or set(document) - {
        "contract_version",
        "concepts",
        "lecture_entity_count",
        "forbidden_cloze_targets",
    }:
        raise CardCentricValidationError("ledger routing fields are not safely targetable")
    concepts = document.get("concepts")
    count = document.get("lecture_entity_count")
    targets = document.get("forbidden_cloze_targets", [])
    if (
        not isinstance(concepts, list)
        or not concepts
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        or not isinstance(targets, list)
        or any(not isinstance(value, str) or not value.strip() for value in targets)
        or len({value.casefold() for value in targets}) != len(targets)
        or document.get("contract_version", 1) != 1
        or any(not isinstance(concept, dict) for concept in concepts)
    ):
        raise CardCentricValidationError("ledger routing fields are not safely targetable")
    return document, tuple(concepts)


def _raw_fact_descriptions(raw: Mapping[str, object]) -> tuple[str, ...]:
    value = raw.get("fact_descriptions")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    canonical = raw.get("canonical_statement")
    if value is None and isinstance(canonical, str):
        return (canonical,)
    return ()


def _ledger_repair_context(
    raw_text: str,
    source_index: CardCentricSourceIndex,
    depth_control_evidence: list[dict[str, str]],
) -> _LedgerRepairContext:
    document, raw_concepts = _parse_targetable_ledger(raw_text)
    index_to_id = {}
    defects: dict[str, list[str]] = {}
    valid_concepts: dict[str, CardConcept] = {}
    for index, raw in enumerate(raw_concepts):
        concept_id = raw.get("concept_id")
        if not isinstance(concept_id, str) or _CONCEPT_ID.fullmatch(concept_id) is None:
            raise CardCentricValidationError("malformed concept IDs cannot be targeted")
        if concept_id in index_to_id.values():
            raise CardCentricValidationError("duplicate concept IDs cannot be targeted")
        index_to_id[index] = concept_id
        try:
            valid_concepts[concept_id] = CardConcept.model_validate(raw)
        except ValidationError as exc:
            defects.setdefault(concept_id, []).append(str(exc)[:2_000])
        descriptions = _raw_fact_descriptions(raw)
        defects.setdefault(concept_id, []).extend(card_fact_structure_defects(descriptions))
        defects.setdefault(concept_id, []).extend(
            card_ledger_concept_defects(
                concept_id=concept_id,
                fact_descriptions=descriptions,
            )
        )
        defects.setdefault(concept_id, []).extend(
            _awareness_concept_defects(
                concept_id=concept_id,
                corpus=_raw_concept_corpus(raw),
                fact_descriptions=descriptions,
                depth_control_evidence=depth_control_evidence,
            )
        )
    stable_key_owners: dict[str, list[str]] = {}
    for concept_id, concept in valid_concepts.items():
        for stable_key in concept.stable_fact_keys:
            stable_key_owners.setdefault(stable_key, []).append(concept_id)
    for owners in stable_key_owners.values():
        if len(owners) > 1:
            for concept_id in owners:
                defects.setdefault(concept_id, []).append(
                    "ledger facts must be distinct after normalization"
                )
    defects = {
        concept_id: list(dict.fromkeys(messages))
        for concept_id, messages in defects.items()
        if messages
    }
    missing_controls = [
        (entity, depth)
        for entity, depth in _depth_controls(source_index)
        if not any(
            raw.get("depth") == depth
            and _normalized_entity_text(entity) in _raw_concept_corpus(raw)
            for raw in raw_concepts
        )
    ]
    next_number = max(int(concept_id[1:]) for concept_id in index_to_id.values()) + 1
    required_additions = tuple(
        {
            "concept_id": f"C{next_number + offset:02d}",
            "primary_entity": entity,
            "depth": depth,
        }
        for offset, (entity, depth) in enumerate(missing_controls)
    )
    if any(int(item["concept_id"][1:]) > 9_999 for item in required_additions):
        raise CardCentricValidationError("ledger has no safe sequential concept ID")
    scope_limit = min(3, max(2, math.ceil(len(raw_concepts) * 0.20)))
    if not defects and not required_additions:
        raise CardCentricValidationError("ledger failure has no targetable concept defect")
    if len(defects) + len(required_additions) > scope_limit:
        raise CardCentricValidationError("ledger repair scope exceeds the localized-damage cap")
    available_addition_ids = tuple(
        f"C{next_number + offset:02d}"
        for offset in range(scope_limit - len(defects))
    )
    unchanged_hashes = {
        concept_id: _canonical_object_sha256(raw_concepts[index])
        for index, concept_id in index_to_id.items()
        if concept_id not in defects
    }
    return _LedgerRepairContext(
        document=document,
        raw_concepts=raw_concepts,
        index_to_id=index_to_id,
        defects_by_id={key: tuple(value) for key, value in sorted(defects.items())},
        required_additions=required_additions,
        available_addition_ids=available_addition_ids,
        unchanged_hashes=unchanged_hashes,
        scope_limit=scope_limit,
        primary_response_sha256=hashlib.sha256(raw_text.encode()).hexdigest(),
    )


def _repair_request_document(
    context: _LedgerRepairContext,
    source_index: CardCentricSourceIndex,
    depth_control_evidence: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "defects": [
            {
                "array_index": index,
                "concept_id": concept_id,
                "messages": list(context.defects_by_id[concept_id]),
                "raw_concept": context.raw_concepts[index],
                "supporting_passages": _repair_supporting_passages(
                    context.raw_concepts[index], source_index
                ),
            }
            for index, concept_id in context.index_to_id.items()
            if concept_id in context.defects_by_id
        ],
        "required_additions": list(context.required_additions),
        "available_addition_ids": list(context.available_addition_ids),
        "depth_control_evidence": depth_control_evidence,
        "constraints": {
            "replacement_ids": list(context.defects_by_id),
            "awareness_only_entities": list(
                _awareness_only_entities(depth_control_evidence)
            ),
            "no_deletions": True,
            "one_repair_call": True,
            "scope_limit": context.scope_limit,
        },
    }


def _repair_supporting_passages(
    raw_concept: Mapping[str, object],
    source_index: CardCentricSourceIndex,
) -> list[dict[str, str]]:
    aliases = raw_concept.get("aliases", ())
    if not isinstance(aliases, (list, tuple)):
        aliases = ()
    terms = {
        _normalized_entity_text(str(value))
        for value in (
            raw_concept.get("primary_entity", ""),
            *aliases,
        )
        if _normalized_entity_text(str(value))
    }
    matches = sorted(
        (
            passage
            for passage in source_index.passages
            if passage.authority != "summary"
            and any(
                set(term.split())
                <= set(_normalized_entity_text(passage.text).split())
                for term in terms
            )
        ),
        key=lambda passage: (passage.authority != "slide", passage.passage_id),
    )[:4]
    return [
        {
            "passage_id": passage.passage_id,
            "authority": passage.authority,
            "text": passage.text,
        }
        for passage in matches
    ]


def _repair_result_error(
    message: str,
    result: StructuredJSONResult[CardConceptRepairBatch],
) -> StructuredOutputError:
    return StructuredOutputError(
        message,
        raw_text=result.raw_text,
        generation=GeneratedText(
            text=result.raw_text,
            provider=result.provider,
            model=result.model,
            request_id=result.request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_microusd=result.cost_microusd,
            cache_creation_input_tokens=result.cache_creation_input_tokens,
            cache_read_input_tokens=result.cache_read_input_tokens,
        ),
        attempt_handle=result.attempt_handle,
    )


def _merge_targeted_ledger_repair(
    context: _LedgerRepairContext,
    repair: StructuredJSONResult[CardConceptRepairBatch],
    source_index: CardCentricSourceIndex,
    depth_control_evidence: list[dict[str, str]],
) -> tuple[CardConceptLedger, dict[str, object]]:
    replacements = {item.concept_id: item for item in repair.value.replacements}
    additions = tuple(repair.value.additions)
    if set(replacements) != set(context.defects_by_id):
        raise _repair_result_error(
            "targeted ledger repair must replace every and only defective concept",
            repair,
        )
    addition_ids = tuple(item.concept_id for item in additions)
    if addition_ids != context.available_addition_ids[: len(additions)]:
        raise _repair_result_error(
            "targeted ledger additions must use the next sequential concept IDs",
            repair,
        )
    required_by_id = {
        item["concept_id"]: item for item in context.required_additions
    }
    if not set(required_by_id) <= set(addition_ids):
        raise _repair_result_error(
            "targeted ledger repair omitted a required depth-control addition",
            repair,
        )
    replacement_entities = {
        _normalized_entity_text(concept.primary_entity)
        for concept in replacements.values()
    }
    for addition in additions:
        required = required_by_id.get(addition.concept_id)
        corpus = _normalized_entity_text(
            " ".join(
                (
                    addition.primary_entity,
                    *addition.aliases,
                    addition.canonical_statement,
                    *addition.fact_descriptions,
                )
            )
        )
        if required is not None:
            if (
                addition.depth != required["depth"]
                or _normalized_entity_text(required["primary_entity"]) not in corpus
            ):
                raise _repair_result_error(
                    "targeted ledger addition does not satisfy its required entity/depth",
                    repair,
                )
        elif _normalized_entity_text(addition.primary_entity) not in replacement_entities:
            raise _repair_result_error(
                "targeted ledger continuation does not match a repaired entity",
                repair,
            )
    merged_concepts = []
    for index, raw in enumerate(context.raw_concepts):
        concept_id = context.index_to_id[index]
        merged_concepts.append(
            replacements[concept_id].model_dump(mode="json")
            if concept_id in replacements
            else raw
        )
    merged_concepts.extend(item.model_dump(mode="json") for item in additions)
    for index, concept_id in context.index_to_id.items():
        if concept_id in replacements:
            continue
        if _canonical_object_sha256(merged_concepts[index]) != context.unchanged_hashes[concept_id]:
            raise _repair_result_error(
                "targeted ledger repair changed an unrelated concept",
                repair,
            )
    document = dict(context.document)
    document["concepts"] = merged_concepts
    document["lecture_entity_count"] = int(document["lecture_entity_count"]) + len(
        context.required_additions
    )
    try:
        ledger = CardConceptLedger.model_validate(document)
    except ValidationError as exc:
        raise _repair_result_error(
            "targeted ledger merge failed full validation: " + str(exc),
            repair,
        ) from exc
    missing = _missing_ledger_depth_controls(ledger, source_index)
    if missing:
        raise _repair_result_error(
            "targeted ledger merge omitted named depth-control entities: "
            + ", ".join(missing),
            repair,
        )
    awareness_defects = _ledger_awareness_defects(ledger, depth_control_evidence)
    if awareness_defects:
        raise _repair_result_error(
            "targeted ledger merge retained invalid awareness-list depth: "
            + "; ".join(awareness_defects),
            repair,
        )
    manifest = {
        "primary_response_sha256": context.primary_response_sha256,
        "index_to_concept_id": {
            str(index): concept_id for index, concept_id in context.index_to_id.items()
        },
        "defects": {
            concept_id: list(messages)
            for concept_id, messages in context.defects_by_id.items()
        },
        "replacement_ids": sorted(replacements),
        "addition_ids": list(addition_ids),
        "unchanged_concept_sha256": dict(sorted(context.unchanged_hashes.items())),
        "evidence_passage_ids": [item["passage_id"] for item in depth_control_evidence],
        "repair_response_sha256": hashlib.sha256(repair.raw_text.encode()).hexdigest(),
        "final_ledger_sha256": _canonical_object_sha256(ledger.model_dump(mode="json")),
        "repaired_concept_count": len(replacements) + len(additions),
        "repair_scope_limit": context.scope_limit,
    }
    return ledger, manifest


def _combined_request_id(request_ids: tuple[str, ...]) -> str:
    """Make repaired S2 usage identifiable without pretending it is one request."""
    document = json.dumps(request_ids, separators=(",", ":"))
    return f"card_ledger:{hashlib.sha256(document.encode()).hexdigest()[:24]}"


def _ledger_repair_instruction(instruction: str) -> str:
    return (
        instruction
        + "\n\n# Validation repair\n"
        "The prior response and the exact validator error are in the user input. "
        "Correct only the reported validation defects. Return only the requested "
        "concept replacements and additions; unchanged concepts are not in the output "
        "schema and cannot be edited. Never emit lecture-depth commentary or "
        "semicolon-bundled facts, and split distinct expression locations into separate "
        "facts. State the exact gene or defect and phenotype; never say only 'basic "
        "gene defect,' 'characteristic presentation,' or 'noted in lecture.' Use each "
        "defect's supporting_passages to replace placeholders with source-entailed "
        "facts. Apply "
        "depth_control_evidence using the base instruction's lecture-depth "
        "rule for named lists. Every entity in constraints.awareness_only_entities must "
        "have exactly one compact recognition fact with no enumerated items or thresholds. "
        "Return JSON only."
    )


def _record_ledger_attempt(
    recorder: Callable[[CardCentricLedgerAttempt], None] | None,
    *,
    call_index: Literal[1, 2],
    kind: Literal["primary", "repair"],
    outcome: Literal["accepted", "validation_failed", "transport_failed"],
    provider: ProviderName,
    model: str,
    instruction: str,
    parameters: dict[str, object],
    parameters_sha256: str,
    result: (
        StructuredJSONResult[CardConceptLedger]
        | StructuredJSONResult[CardConceptRepairBatch]
        | None
    ) = None,
    error: StructuredOutputError | None = None,
    transport_error: Exception | None = None,
) -> None:
    if recorder is None:
        return
    if sum(value is not None for value in (result, error, transport_error)) != 1:
        raise AssertionError("ledger attempt must have exactly one outcome payload")
    if result is not None:
        recorder(
            CardCentricLedgerAttempt(
                call_index=call_index,
                kind=kind,
                outcome=outcome,
                # The immutable attempt identity is the requested pinned S2
                # route.  Providers may report a versioned or aliased response
                # model, which must not silently rewrite the route or its
                # generation-parameter hash.
                provider=provider,
                model=model,
                instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
                generation_parameters=parameters,
                generation_parameters_sha256=parameters_sha256,
                request_id=result.request_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_microusd=result.cost_microusd,
                validation_error=None,
                invalid_response_sha256=None,
                invalid_response=None,
            )
        )
        return
    if transport_error is not None:
        recorder(
            CardCentricLedgerAttempt(
                call_index=call_index,
                kind=kind,
                outcome=outcome,
                provider=provider,
                model=model,
                instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
                generation_parameters=parameters,
                generation_parameters_sha256=parameters_sha256,
                request_id=_transport_provider_request_id(transport_error),
                input_tokens=0,
                output_tokens=0,
                cost_microusd=0,
                validation_error=_redacted_invalid_response(str(transport_error))[:2_000],
                invalid_response_sha256=None,
                invalid_response=None,
                diagnostic_source=_transport_diagnostic_source(transport_error),
                http_status=_transport_http_status(transport_error),
            )
        )
        return
    assert error is not None
    invalid_response = _redacted_invalid_response(error.raw_text)
    recorder(
        CardCentricLedgerAttempt(
            call_index=call_index,
            kind=kind,
            outcome=outcome,
            provider=provider,
            model=model,
            instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
            generation_parameters=parameters,
            generation_parameters_sha256=parameters_sha256,
            request_id=error.generation.request_id,
            input_tokens=error.generation.input_tokens,
            output_tokens=error.generation.output_tokens,
            cost_microusd=error.generation.cost_microusd,
            validation_error=str(error),
            # This is the SHA-256 of the bounded, redacted bytes stored below;
            # raw model output is neither persisted nor content-addressed.
            invalid_response_sha256=hashlib.sha256(invalid_response.encode()).hexdigest(),
            invalid_response=invalid_response,
        )
    )


def _redacted_invalid_response(value: str) -> str:
    """Bound and redact malformed provider output before durable storage.

    The returned bytes are the sole persisted diagnostic payload and are what
    ``invalid_response_sha256`` identifies. Raw provider output remains only in
    memory long enough to construct the bounded repair request.
    """
    clipped = value[:_S2_INVALID_RESPONSE_LIMIT]
    quoted_field = re.compile(
        r'(?is)("(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|token|authorization)"\s*:\s*")[^"]*(")'
    )
    redacted = quoted_field.sub(r"\1[REDACTED]\2", clipped)
    single_quoted_field = re.compile(
        r"(?is)('(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|token|authorization)'\s*:\s*')[^']*(')"
    )
    redacted = single_quoted_field.sub(r"\1[REDACTED]\2", redacted)
    redacted = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;}\]]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(\bbearer\s+)[a-z0-9._~+/=-]{8,}",
        r"\1[REDACTED]",
        redacted,
    )
    return re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|token)\b\s*[:=]\s*)[^\s,;}\]]+",
        r"\1[REDACTED]",
        redacted,
    )


def _canonical_sha256(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _transport_provider_request_id(error: Exception) -> str:
    """Keep only an explicit, bounded provider request identifier."""
    if not isinstance(error, LLMRequestError):
        return ""
    request_id = error.provider_request_id
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        return ""
    return request_id


def _transport_diagnostic_source(error: Exception) -> str | None:
    return error.source.value if isinstance(error, LLMRequestError) else None


def _transport_http_status(error: Exception) -> int | None:
    if not isinstance(error, LLMRequestError):
        return None
    status = error.http_status
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _s2_generation_parameters(
    provider: ProviderName,
    model: str,
    options: GenerationOptions,
) -> dict[str, object]:
    """Describe precisely what the S2 transports send for the current call."""
    transmitted_cache = provider is ProviderName.ANTHROPIC
    anthropic_capabilities = (
        resolve_anthropic_model_capabilities(model)
        if provider is ProviderName.ANTHROPIC
        else None
    )
    adaptive_anthropic = (
        anthropic_capabilities is not None
        and anthropic_capabilities.thinking_capability.value == "adaptive"
    )
    temperature: dict[str, object]
    if anthropic_capabilities is not None and not anthropic_capabilities.temperature:
        temperature = {
            "requested": options.temperature,
            "transmission": "not_transmitted",
            "provider_default": "unknown_provider_default",
        }
    else:
        temperature = {"value": options.temperature, "transmission": "transmitted"}
    return {
        "provider": provider.value,
        "model": model,
        "temperature": temperature,
        "max_tokens": {"value": options.max_tokens, "transmission": "transmitted"},
        "thinking": {
            "requested": options.thinking.value,
            "transmission": (
                "transmitted_disabled" if adaptive_anthropic else "not_transmitted"
            ),
            **(
                {}
                if adaptive_anthropic
                else {"provider_default": "unknown_provider_default"}
            ),
        },
        "cache": {
            "requested": "summary_prefix",
            "transmission": "anthropic_ephemeral" if transmitted_cache else "prompt_context_only",
        },
    }


def s2_generation_parameters(provider: ProviderName, model: str) -> dict[str, object]:
    """Return the canonical, transport-truthful S2 generation identity."""
    return _s2_generation_parameters(provider, model, _S2_GENERATION_OPTIONS)


def _legacy_s2_generation_parameters(
    provider: ProviderName,
    model: str,
) -> dict[str, object]:
    """Return the pre-remediation v24 document without rewriting its evidence."""
    parameters = s2_generation_parameters(provider, model)
    temperature = parameters["temperature"]
    if isinstance(temperature, dict) and temperature.get("transmission") == "not_transmitted":
        parameters["temperature"] = {"value": 0, "transmission": "transmitted"}
    return parameters


def validate_s2_generation_parameters(
    provider: ProviderName,
    model: str,
    parameters: dict[str, object],
) -> None:
    """Require the complete, exact S2 transport identity document.

    Only adaptive Anthropic models receive an explicit disabled-thinking
    control. All other S2 routes truthfully record no transmitted control and
    an unknown provider default.
    """
    expected = s2_generation_parameters(provider, model)
    if _canonical_json(parameters) != _canonical_json(expected):
        raise ValueError("card-ledger generation parameters are not the canonical S2 document")


def validate_persisted_s2_generation_parameters(
    provider: ProviderName,
    model: str,
    parameters: dict[str, object],
) -> None:
    """Accept immutable v24 evidence as well as the current write contract."""
    expected = s2_generation_parameters(provider, model)
    legacy = _legacy_s2_generation_parameters(provider, model)
    if _canonical_json(parameters) not in {
        _canonical_json(expected),
        _canonical_json(legacy),
    }:
        raise ValueError("card-ledger generation parameters are not canonical evidence")


_SOURCE_ORDER = {"summary": 0, "transcript": 1, "slide": 2}
CARD_CENTRIC_SYSTEM_TOKENS = (
    "cardio",
    "endo",
    "gi",
    "heme",
    "msk",
    "neuro",
    "onc",
    "psych",
    "renal",
    "resp",
)
_UNTAGGED_SAFE_RATE = 0.03
CARD_CENTRIC_UNCONDITIONAL_RESIDUAL_RATE = 0.15
_SYSTEM_ALIASES = {
    "cardio": frozenset({"cardio", "cardiology", "cardiovascular"}),
    "endo": frozenset({"endo", "endocrine", "endocrinology"}),
    "gi": frozenset({"gi", "gastrointestinal", "gastroenterology"}),
    "heme": frozenset({"heme", "hematology", "haematology", "heme lymph", "hematology lymph"}),
    "msk": frozenset({"msk", "musculoskeletal", "orthopedics", "orthopaedics"}),
    "neuro": frozenset({"neuro", "neurology", "neuroscience"}),
    "onc": frozenset({"onc", "oncology"}),
    "psych": frozenset({"psych", "psychiatry", "behavioral science"}),
    "renal": frozenset({"renal", "nephrology"}),
    "resp": frozenset({"resp", "respiratory", "pulmonology", "pulmonary"}),
}


def resolve_card_centric_scope(
    *, tag_allowlist: tuple[str, ...], subject: str, topic: str
) -> tuple[str, ...]:
    """Pin an explicit scope or one unambiguous system token from lecture metadata."""
    explicit = tuple(token.strip() for token in tag_allowlist if token.strip())
    if explicit:
        return explicit
    subject_matches = _recognized_system_tokens(subject)
    matches = subject_matches or _recognized_system_tokens(topic)
    if len(matches) != 1:
        raise CardCentricValidationError(
            "Could not resolve exactly one card-centric system from this lecture. "
            "Enter Existing-card tag scope before queueing."
        )
    return matches


def _recognized_system_tokens(value: str) -> tuple[str, ...]:
    normalized = " ".join(value.casefold().replace("/", " ").replace("-", " ").split())
    if not normalized:
        return ()
    padded = f" {normalized} "
    matches = tuple(
        token
        for token in CARD_CENTRIC_SYSTEM_TOKENS
        if any(f" {alias} " in padded for alias in _SYSTEM_ALIASES[token])
    )
    return matches


@dataclass(frozen=True, slots=True)
class _CompletedBatch:
    results: tuple[CardClassification, ...]
    audit: ClassifierBatchAudit


def build_source_index(
    passages: Iterable[SourcePassage],
    *,
    snapshot_id: str,
    source_revision_hashes: dict[int, str],
    summary_outline_sha256: str | None = None,
) -> CardCentricSourceIndex:
    """Build the immutable S1 prefix: summary, transcript, then slides."""
    converted = [_to_card_passage(passage) for passage in passages]
    ordered = tuple(
        sorted(
            converted,
            key=lambda passage: (
                _SOURCE_ORDER[passage.authority],
                passage.source_id,
                passage.passage_id,
            ),
        )
    )
    if not ordered:
        raise CardCentricValidationError("source index has no usable passages")
    if len({passage.passage_id for passage in ordered}) != len(ordered):
        raise CardCentricValidationError("source index has duplicate passage IDs")
    prefix = "\n\n".join(
        "<passage"
        f' id="{passage.passage_id}" source_id="{passage.source_id}"'
        f' authority="{passage.authority}">\n{passage.text}\n</passage>'
        for passage in ordered
    )
    document = {
        "snapshot_id": snapshot_id,
        "source_revision_hashes": dict(sorted(source_revision_hashes.items())),
        "summary_outline_sha256": summary_outline_sha256,
        "passages": [passage.model_dump(mode="json") for passage in ordered],
        "prefix": prefix,
    }
    return CardCentricSourceIndex(
        snapshot_id=snapshot_id,
        source_revision_hashes=source_revision_hashes,
        summary_outline_sha256=summary_outline_sha256,
        passages=ordered,
        prefix=prefix,
        source_sha256=_sha(document),
    )


def build_snapshot_census(
    cards: Sequence[CardRecord],
    *,
    deck_allowlist: tuple[str, ...],
    scope_tokens: tuple[str, ...],
    snapshot_id: str = "companion_snapshot",
    system_tokens: tuple[str, ...] = CARD_CENTRIC_SYSTEM_TOKENS,
) -> SnapshotCensus:
    _unique_card_ids(cards)
    normalized_decks = tuple(sorted({deck.casefold() for deck in deck_allowlist}))
    normalized_scope = _scope_tokens(scope_tokens)
    system_universe = tuple(sorted(set(_scope_tokens(system_tokens)) | set(normalized_scope)))
    mapping: dict[
        int,
        Literal[
            "target_tagged",
            "other_system_excluded",
            "untagged",
            "deck_excluded",
        ],
    ] = {}
    for card in cards:
        eligible = not normalized_decks or bool(
            {deck.casefold() for deck in card.deck_names} & set(normalized_decks)
        )
        if not eligible:
            mapping[card.note_id] = "deck_excluded"
        elif _matches_scope(card.tags, normalized_scope):
            mapping[card.note_id] = "target_tagged"
        elif _matches_scope(card.tags, system_universe):
            mapping[card.note_id] = "other_system_excluded"
        else:
            mapping[card.note_id] = "untagged"
    tagged = sum(value == "target_tagged" for value in mapping.values())
    other_system = sum(value == "other_system_excluded" for value in mapping.values())
    untagged = sum(value == "untagged" for value in mapping.values())
    deck_excluded = sum(value == "deck_excluded" for value in mapping.values())
    denominator = tagged + other_system + untagged
    rate = 0.0 if denominator == 0 else untagged / denominator
    trusted = denominator > 0 and rate < _UNTAGGED_SAFE_RATE
    return SnapshotCensus(
        snapshot_id=snapshot_id,
        denominator_count=denominator,
        tagged_count=tagged,
        other_system_tagged_count=other_system,
        untagged_count=untagged,
        deck_excluded_count=deck_excluded,
        excluded_count=other_system + deck_excluded,
        mapping={note_id: mapping[note_id] for note_id in sorted(mapping)},
        filters_sha256=_sha(
            {
                "snapshot_id": snapshot_id,
                "deck_allowlist": normalized_decks,
                "scope_tokens": normalized_scope,
                "system_tokens": system_universe,
                "cards": [
                    {"note_id": card.note_id, "content_sha256": card.content_sha256}
                    for card in sorted(cards, key=lambda card: card.note_id)
                ],
            }
        ),
        trust=CensusTrust(
            decision="trusted" if trusted else "blocked",
            reason=(
                "untagged rate is within the card_centric_v1 tag-scope threshold"
                if trusted
                else (
                    "no deck-eligible notes are available for tag-scope trust"
                    if denominator == 0
                    else (
                        "untagged rate requires an unconditional whole-deck residual sweep"
                        if rate >= CARD_CENTRIC_UNCONDITIONAL_RESIDUAL_RATE
                        else "untagged rate exceeds the card_centric_v1 tag-scope threshold; "
                        "the whole-deck residual safety net is required"
                    )
                )
            ),
            untagged_rate=rate,
            safe_untagged_rate=_UNTAGGED_SAFE_RATE,
        ),
    )


def scope_cards(
    cards: Sequence[CardRecord],
    *,
    census: SnapshotCensus,
    scope_tokens: tuple[str, ...],
) -> TagScopeResult:
    _unique_card_ids(cards)
    ids = {card.note_id for card in cards}
    if ids != set(census.mapping):
        raise CardCentricValidationError("census mapping does not match snapshot cards")
    tokens = _scope_tokens(scope_tokens)
    scoped = tuple(
        sorted(
            card.note_id
            for card in cards
            if census.mapping[card.note_id] == "target_tagged" and _matches_scope(card.tags, tokens)
        )
    )
    unscoped = tuple(sorted(ids - set(scoped)))
    if set(scoped) & set(unscoped) or set(scoped) | set(unscoped) != ids:
        raise CardCentricValidationError("tag scope does not partition snapshot cards")
    return TagScopeResult(
        snapshot_id=census.snapshot_id,
        filters_sha256=census.filters_sha256,
        scoped_note_ids=scoped,
        unscoped_note_ids=unscoped,
    )


@dataclass(slots=True)
class CardCentricClassifier:
    structured: StructuredTextService
    instruction: str = (
        "Classify every supplied Anki card against the cached lecture sources. "
        "Return YES, MAYBE, or NO. YES requires cited source support. "
        "Do not invent IDs; return exactly one result per card."
    )
    # These are the S0/v1 defaults. V2 explicitly supplies its frozen
    # execution settings at the stage boundary.
    batch_size: int = 40
    concurrency: int = 8
    retry_attempts: int = 1
    thinking_budget_tokens: int | None = None
    require_nonblank_reason: bool = False
    capabilities: ProviderCapabilities = ProviderCapabilities()

    def __post_init__(self) -> None:
        if (
            self.batch_size < 1
            or self.concurrency < 1
            or self.retry_attempts < 1
            or (
                self.thinking_budget_tokens is not None
                and self.thinking_budget_tokens < 1024
            )
        ):
            raise ValueError("classifier batch size and concurrency must be positive")

    async def classify(
        self,
        cards: Sequence[CardRecord],
        *,
        source_index: CardCentricSourceIndex,
        concept_ids: tuple[str, ...],
        concepts: Sequence[CardConcept] = (),
        provider: ProviderName,
        model: str,
    ) -> ClassifierResult:
        _unique_card_ids(cards)
        concept_definitions = tuple(
            {
                "concept_id": concept.concept_id,
                "canonical_statement": concept.canonical_statement,
                "primary_entity": concept.primary_entity,
                "aliases": list(concept.aliases),
                "fact_descriptions": list(concept.fact_descriptions),
                "facts": [
                    {"fact_id": fact_id, "statement": statement}
                    for fact_id, statement in zip(
                        concept.fact_ids, concept.fact_descriptions, strict=True
                    )
                ],
            }
            for concept in sorted(concepts, key=lambda item: item.concept_id)
        )
        if concept_definitions and tuple(
            item["concept_id"] for item in concept_definitions
        ) != tuple(sorted(concept_ids)):
            raise CardCentricValidationError(
                "classifier concept definitions do not match allowed concept IDs"
            )
        batches = tuple(_batches(cards, self.batch_size))
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(
            batch_index: int,
            batch: tuple[CardRecord, ...],
        ) -> _CompletedBatch:
            async with semaphore:
                return await asyncio.to_thread(
                    self._classify_batch,
                    batch,
                    batch_index=batch_index,
                    source_index=source_index,
                    concept_ids=concept_ids,
                    concept_definitions=concept_definitions,
                    provider=provider,
                    model=model,
                )

        completed = await asyncio.gather(
            *(one(index, batch) for index, batch in enumerate(batches))
        )
        results = tuple(
            sorted(
                (item for batch in completed for item in batch.results),
                key=lambda item: item.note_id,
            )
        )
        audits = tuple(
            sorted(
                (batch.audit for batch in completed),
                key=lambda item: item.batch_index,
            )
        )
        request_ids = tuple(audit.request_id for audit in audits)
        if (
            concept_definitions
            and any(item.verdict == "YES" for item in results)
            and not any(item.covered_concept_ids for item in results)
        ):
            raise CardCentricValidationError(
                "classifier mapped no YES cards to allowed concepts"
            )
        return ClassifierResult(
            results=results,
            telemetry=ClassifierTelemetry(
                batch_count=len(batches),
                cache_prefix_sha256=hashlib.sha256(source_index.prefix.encode()).hexdigest(),
                cache_mode=(
                    "ephemeral" if self.capabilities.prompt_prefix_caching else "ordinary_prefix"
                ),
                provider=provider.value,
                model=model,
                request_ids=request_ids,
                batches=audits,
            ),
        )

    def _classify_batch(
        self,
        cards: tuple[CardRecord, ...],
        *,
        batch_index: int,
        source_index: CardCentricSourceIndex,
        concept_ids: tuple[str, ...],
        concept_definitions: tuple[dict[str, object], ...],
        provider: ProviderName,
        model: str,
    ) -> _CompletedBatch:
        request = json.dumps(
            {
                "cards": [card.model_dump(mode="json") for card in cards],
                "concept_definitions": list(concept_definitions),
                "allowed_concept_ids": list(concept_ids),
                "allowed_supporting_passage_ids": [
                    passage.passage_id for passage in source_index.passages
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        attempts: list[StructuredJSONResult[CardClassificationBatchOutput]] = []
        note_ids = tuple(card.note_id for card in cards)
        fact_ids_by_concept = {
            str(concept["concept_id"]): tuple(
                str(fact["fact_id"])
                for fact in concept["facts"]
                if isinstance(fact, dict)
            )
            for concept in concept_definitions
        }
        partition_instruction = (
            "Return exactly one result for each of these note IDs and no other note IDs: "
            f"{json.dumps(note_ids, separators=(',', ':'))}. Copy every ID exactly. "
            "For each card, copy every fact ID whose statement the card directly answers "
            "into covered_fact_ids. For every covered fact, return covered_fact_evidence "
            "with the exact shortest Text or Extra substring carrying that proposition; "
            "lecture passages cannot serve as card-field evidence. Then copy exactly those "
            "facts' parent concept IDs into "
            "covered_concept_ids. Use both empty lists only when the card is lecture-supported "
            "but answers none of the supplied facts. If Text or Extra conflicts with the "
            "lecture, add a field_review excluding that field from fact evidence; do not use "
            "a CardFlag for an otherwise usable field. Copy supporting passage, fact, and "
            "concept IDs verbatim from their allowed lists; "
            "never synthesize IDs. If an exact supporting passage ID is uncertain, omit it "
            "and do not return YES."
        )
        try:
            with provider_call_scope(batch_index=batch_index, batch_note_ids=note_ids):
                first = self._request(
                    f"{self.instruction}\n\n{partition_instruction}",
                    request,
                    provider=provider,
                    model=model,
                    source_index=source_index,
                )
            attempts.append(first)
            validated = self._validate_attempt(
                first,
                cards=cards,
                source_index=source_index,
                concept_ids=concept_ids,
                fact_ids_by_concept=fact_ids_by_concept,
            )
        except (StructuredOutputError, CardCentricValidationError) as first_error:
            if self.retry_attempts < 2:
                raise
            raw = (
                first_error.raw_text
                if isinstance(first_error, StructuredOutputError)
                else first.raw_text
            )
            repair_payload = {
                "classification_input": json.loads(request),
                "invalid_response": raw,
                "validation_error": str(first_error),
            }
            if isinstance(first_error, CardCentricValidationError):
                expected = {card.note_id for card in cards}
                observed = [item.note_id for item in first.value.results]
                repair_payload["partition_diagnostics"] = {
                    "missing_note_ids": sorted(expected - set(observed)),
                    "extra_note_ids": sorted(set(observed) - expected),
                    "duplicate_note_ids": sorted(
                        {note_id for note_id in observed if observed.count(note_id) > 1}
                    ),
                }
            repair_input = json.dumps(
                repair_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            with provider_call_scope(
                batch_index=batch_index,
                batch_note_ids=tuple(card.note_id for card in cards),
                kind="repair",
            ):
                repaired = self._request(
                    f"{self.instruction}\n\nRepair the invalid classifier batch. "
                    "Correct only the reported defect and return the complete batch.\n\n"
                    f"{partition_instruction}",
                    repair_input,
                    provider=provider,
                    model=model,
                    source_index=source_index,
                )
            attempts.append(repaired)
            validated = self._validate_attempt(
                repaired,
                cards=cards,
                source_index=source_index,
                concept_ids=concept_ids,
                fact_ids_by_concept=fact_ids_by_concept,
            )
        result = attempts[-1]
        return _CompletedBatch(
            results=validated,
            audit=ClassifierBatchAudit(
                batch_index=batch_index,
                note_ids=tuple(card.note_id for card in cards),
                request_id=result.request_id,
                input_tokens=sum(attempt.input_tokens for attempt in attempts),
                output_tokens=sum(attempt.output_tokens for attempt in attempts),
                cost_microusd=sum(attempt.cost_microusd for attempt in attempts),
                cache_creation_input_tokens=sum(
                    attempt.cache_creation_input_tokens for attempt in attempts
                ),
                cache_read_input_tokens=sum(
                    attempt.cache_read_input_tokens for attempt in attempts
                ),
            ),
        )

    def _request(
        self,
        instruction: str,
        input_text: str,
        *,
        provider: ProviderName,
        model: str,
        source_index: CardCentricSourceIndex,
    ) -> StructuredJSONResult[CardClassificationBatchOutput]:
        options = GenerationOptions(cacheable_source_prefix=source_index.prefix)
        if self.thinking_budget_tokens is not None:
            options = GenerationOptions(
                cacheable_source_prefix=source_index.prefix,
                thinking_budget_tokens=self.thinking_budget_tokens,
            )
        return self.structured.generate_json(
            instruction,
            input_text,
            output_model=CardClassificationBatchOutput,
            provider=provider,
            model=model,
            options=options,
        )

    def _validate_attempt(
        self,
        result: StructuredJSONResult[CardClassificationBatchOutput],
        *,
        cards: Sequence[CardRecord],
        source_index: CardCentricSourceIndex,
        concept_ids: tuple[str, ...],
        fact_ids_by_concept: Mapping[str, tuple[str, ...]],
    ) -> tuple[CardClassification, ...]:
        try:
            return self.validate_output(
                result.value,
                cards=cards,
                source_index=source_index,
                concept_ids=concept_ids,
                fact_ids_by_concept=fact_ids_by_concept,
            )
        except CardCentricValidationError as exc:
            expected = {card.note_id for card in cards}
            observed = [item.note_id for item in result.value.results]
            emit_provider_event(
                result.attempt_handle,
                "contract_failed",
                error=str(exc),
                missing_note_ids=tuple(expected - set(observed)),
                extra_note_ids=tuple(set(observed) - expected),
                duplicate_note_ids=tuple(
                    sorted({note_id for note_id in observed if observed.count(note_id) > 1})
                ),
            )
            raise

    def validate_output(
        self,
        output: CardClassificationBatchOutput,
        *,
        cards: Sequence[CardRecord],
        source_index: CardCentricSourceIndex,
        concept_ids: tuple[str, ...],
        fact_ids_by_concept: Mapping[str, tuple[str, ...]] | None = None,
    ) -> tuple[CardClassification, ...]:
        expected = {card.note_id for card in cards}
        observed = [result.note_id for result in output.results]
        if len(observed) != len(set(observed)) or set(observed) != expected:
            raise CardCentricValidationError(
                "classifier output does not exactly partition batch cards"
            )
        passages = {passage.passage_id: passage for passage in source_index.passages}
        allowed_concepts = set(concept_ids)
        fact_ids_by_concept = fact_ids_by_concept or {}
        fact_to_concept = {
            fact_id: concept_id
            for concept_id, fact_ids in fact_ids_by_concept.items()
            for fact_id in fact_ids
        }
        validated: list[CardClassification] = []
        cards_by_id = {card.note_id: card for card in cards}
        for result in output.results:
            if self.require_nonblank_reason and not result.reason.strip():
                raise CardCentricValidationError("classifier returned a blank reason")
            if not set(result.covered_concept_ids) <= allowed_concepts:
                raise CardCentricValidationError("classifier invented a concept ID")
            if fact_to_concept:
                if not set(result.covered_fact_ids) <= set(fact_to_concept):
                    raise CardCentricValidationError("classifier invented a fact ID")
                evidence_fact_ids = {
                    evidence.fact_id for evidence in result.covered_fact_evidence
                }
                if evidence_fact_ids != set(result.covered_fact_ids):
                    raise CardCentricValidationError(
                        "classifier fact coverage lacks exact card-field evidence"
                    )
                excluded_fields = {review.field for review in result.field_reviews}
                card = cards_by_id[result.note_id]
                for evidence in result.covered_fact_evidence:
                    if evidence.field in excluded_fields:
                        raise CardCentricValidationError(
                            "classifier used an excluded card field as fact evidence"
                        )
                    field_text = normalize_html(getattr(card, evidence.field)).casefold()
                    span = normalize_html(evidence.span).casefold()
                    if not span or span not in field_text:
                        raise CardCentricValidationError(
                            "classifier fact evidence does not occur in the named card field"
                        )
                expected_concepts = {
                    fact_to_concept[fact_id] for fact_id in result.covered_fact_ids
                }
                if set(result.covered_concept_ids) != expected_concepts:
                    raise CardCentricValidationError(
                        "classifier concept coverage does not match fact coverage"
                    )
            if not set(result.supporting_passage_ids) <= set(passages):
                result = result.model_copy(
                    update={"verdict": "MAYBE", "supporting_passage_ids": ()}
                )
            if result.verdict == "YES" and not result.supporting_passage_ids:
                raise CardCentricValidationError("classifier returned an ungrounded YES")
            validated.append(result)
        return tuple(sorted(validated, key=lambda item: item.note_id))


def selection_eligible(
    result: CardClassification,
    source_index: CardCentricSourceIndex,
) -> bool:
    """Encode future S5 coverage eligibility now, before S5 exists."""
    passages = {passage.passage_id: passage for passage in source_index.passages}
    return (
        result.verdict == "YES"
        and not result.flags
        and any(
            passage_id in passages and passages[passage_id].authority != "summary"
            for passage_id in result.supporting_passage_ids
        )
    )


def selection_eligible_v2(
    result: CardClassification,
    source_index: CardCentricSourceIndex,
) -> bool:
    """V2 admits grounded summary evidence; v1 keeps its stricter rule."""
    return evidence_quality_v2(result, source_index) is not None


def evidence_quality_v2(
    result: CardClassification,
    source_index: CardCentricSourceIndex,
) -> EvidenceQuality | None:
    """Return v2 grounding quality only for an eligible thorough classification."""
    if result.verdict != "YES" or result.flags or not result.supporting_passage_ids:
        return None
    passages = {passage.passage_id: passage for passage in source_index.passages}
    cited = tuple(passages.get(passage_id) for passage_id in result.supporting_passage_ids)
    if any(passage is None for passage in cited):
        return None
    if any(passage.authority != "summary" for passage in cited if passage is not None):
        return EvidenceQuality.PRIMARY_SOURCE
    return EvidenceQuality.SUMMARY_GROUNDED


def fast_selection_eligible_v2(
    result: FastCardClassification,
    source_index: CardCentricSourceIndex,
) -> bool:
    """Fast-pass YES rows are grounded enough for v2 coverage and selection."""
    passages = {passage.passage_id for passage in source_index.passages}
    return (
        result.verdict == "LIKELY_YES"
        and not result.flags
        and bool(result.grounded_concept_ids)
        and any(passage_id in passages for passage_id in result.supporting_passage_ids)
    )


@dataclass(frozen=True, slots=True)
class _QualitySelectionCandidate:
    identity: str
    kind: Literal["existing", "generated"]
    existing_note_id: int | None
    generated_card_id: str | None
    tier: SelectionTier
    evidence_quality: EvidenceQuality
    coverage: frozenset[tuple[str, str]]
    concept_coverage: frozenset[str]
    priority: int
    mandatory: bool
    duplicate_target: bool
    split: bool
    flag_count: int


def _selection_identity_for_note(note_id: int) -> str:
    return f"existing:{note_id}"


def _selection_identity_for_generated(card_id: str) -> str:
    return f"generated:{card_id}"


def select_high_yield_v2(
    classifications: Sequence[CardClassification],
    *,
    fast_classifications: Sequence[FastCardClassification],
    ledger: CardConceptLedger,
    source_index: CardCentricSourceIndex,
    generated_cards: Sequence[GeneratedCardResolution],
    fast_fallback_note_ids: Sequence[int] = (),
    semantic_review_required_card_ids: Sequence[str] = (),
    overflow_acknowledgement: dict[str, object] | CanonicalJsonObject | None = None,
    target: int = 65,
    cap: int = 70,
    minimum: int = 60,
) -> QualitySelectionResult:
    """Select the smallest deterministic, quality-first v2 card set.

    T6 is a last-resort source of independently grounded cards below the warning
    floor.  It never turns an unrecovered semantic fallback into a candidate.
    """
    if not minimum <= target <= cap:
        raise CardCentricValidationError("selection target/cap is invalid")
    concepts = {concept.concept_id: concept for concept in ledger.concepts}
    source_authority = {
        passage.passage_id: passage.authority for passage in source_index.passages
    }
    semantic_review_ids = tuple(sorted(set(semantic_review_required_card_ids)))
    semantic_review_set = set(semantic_review_ids)
    duplicate_target_note_ids = {
        generated_row.duplicate_of_existing_note_id
        for generated_row in generated_cards
        if generated_row.status == "duplicate_of_existing"
        and generated_row.duplicate_of_existing_note_id is not None
    }
    duplicate_target_generated_card_ids = {
        generated_row.duplicate_of_generated_card_id
        for generated_row in generated_cards
        if generated_row.status == "duplicate_of_existing"
        and generated_row.duplicate_of_generated_card_id is not None
    }

    def priority(concept_id: str) -> tuple[int, str]:
        concept = concepts[concept_id]
        return (
            0
            if concept.emphasis_flag or concept.importance == "high"
            else 1
            if concept.importance == "medium"
            else 2,
            concept_id,
        )

    def evidence_quality(passage_ids: Sequence[str], *, fast: bool = False) -> EvidenceQuality:
        if fast:
            return EvidenceQuality.FAST_PASS
        if any(
            source_authority.get(passage_id) in {"slide", "transcript"}
            for passage_id in passage_ids
        ):
            return EvidenceQuality.PRIMARY_SOURCE
        return EvidenceQuality.SUMMARY_GROUNDED

    def covered_priority(concept_ids: Sequence[str]) -> int:
        return min(
            (priority(concept_id)[0] for concept_id in concept_ids if concept_id in concepts),
            default=3,
        )

    candidates: list[_QualitySelectionCandidate] = []
    selectable_generated_ids: set[str] = set()
    for generated_row in generated_cards:
        is_duplicate_target = generated_row.card_id in duplicate_target_generated_card_ids
        tier_priority = (
            priority(generated_row.concept_id)[0]
            if generated_row.concept_id in concepts
            else 3
        )
        tier = (
            SelectionTier.T1
            if tier_priority == 0
            else SelectionTier.T2
            if tier_priority == 1
            else SelectionTier.T4
        )
        candidates.append(
            _QualitySelectionCandidate(
                identity=_selection_identity_for_generated(generated_row.card_id),
                kind="generated",
                existing_note_id=None,
                generated_card_id=generated_row.card_id,
                tier=tier,
                evidence_quality=evidence_quality(generated_row.source_passage_ids),
                coverage=frozenset({("fact", generated_row.fact_id)}),
                concept_coverage=frozenset(
                    {generated_row.concept_id}
                    if generated_row.concept_id in concepts
                    else set()
                ),
                priority=tier_priority,
                # An S8 terminal can name a prior generated card.  As with a
                # named existing note, only an independently eligible generated
                # row may be conserved, but its exact identity then remains
                # mandatory through selection and S9.
                mandatory=tier_priority == 0 or is_duplicate_target,
                duplicate_target=is_duplicate_target,
                split=generated_row.split,
                flag_count=0,
            )
        )
        if (
            generated_row.status == "generated"
            and generated_row.concept_id in concepts
            and any(
                passage_id in source_authority
                for passage_id in generated_row.source_passage_ids
            )
        ):
            selectable_generated_ids.add(generated_row.card_id)

    for classification in classifications:
        fact_ids = set(classification.covered_fact_ids)
        concept_ids = {
            *classification.covered_concept_ids,
            *(fact_id.rpartition("-M")[0] for fact_id in fact_ids),
        }
        tier_priority = covered_priority(tuple(concept_ids))
        coverage = frozenset(
            (("fact", fact_id) for fact_id in fact_ids)
            if fact_ids
            else (
                ("note", str(classification.note_id)),
                *(
                    ("concept", concept_id)
                    for concept_id in classification.covered_concept_ids
                    if concept_id in concepts
                ),
            )
        )
        if selection_eligible_v2(classification, source_index):
            tier = SelectionTier.T3 if tier_priority == 0 else SelectionTier.T5
        else:
            continue
        candidates.append(
            _QualitySelectionCandidate(
                identity=_selection_identity_for_note(classification.note_id),
                kind="existing",
                existing_note_id=classification.note_id,
                generated_card_id=None,
                tier=tier,
                evidence_quality=evidence_quality(classification.supporting_passage_ids),
                coverage=coverage,
                concept_coverage=frozenset(
                    concept_id for concept_id in concept_ids if concept_id in concepts
                ),
                priority=tier_priority,
                duplicate_target=classification.note_id in duplicate_target_note_ids,
                # S8's terminal duplicate identity is a conservation contract:
                # selecting an equivalent card is insufficient for S9 because
                # the exact named existing target proves the duplicate outcome.
                mandatory=(tier is SelectionTier.T3 and tier_priority == 0)
                or classification.note_id in duplicate_target_note_ids,
                split=False,
                flag_count=len(classification.flags),
            )
        )

    for fast_classification in fast_classifications:
        if not fast_selection_eligible_v2(fast_classification, source_index):
            continue
        coverage = frozenset(
            ("concept", concept_id)
            for concept_id in fast_classification.grounded_concept_ids
            if concept_id in concepts
        )
        candidates.append(
            _QualitySelectionCandidate(
                identity=_selection_identity_for_note(fast_classification.note_id),
                kind="existing",
                existing_note_id=fast_classification.note_id,
                generated_card_id=None,
                tier=SelectionTier.T6,
                evidence_quality=EvidenceQuality.FAST_PASS,
                coverage=coverage,
                concept_coverage=frozenset(
                    concept_id
                    for concept_id in fast_classification.grounded_concept_ids
                    if concept_id in concepts
                ),
                priority=covered_priority(fast_classification.grounded_concept_ids),
                # Fast-pass rows are T6 only.  Never let an incoming S8
                # duplicate target elevate one: historical or malformed
                # artifacts must not bypass the warning-floor policy.
                mandatory=False,
                duplicate_target=False,
                split=False,
                flag_count=len(fast_classification.flags),
            )
        )

    existing_candidate_ids = tuple(
        sorted(
            {
                *(row.note_id for row in classifications),
                *(row.note_id for row in fast_classifications),
                *fast_fallback_note_ids,
            }
        )
    )
    generated_candidate_ids = tuple(sorted({row.card_id for row in generated_cards}))
    if len(generated_candidate_ids) != len(generated_cards):
        raise CardCentricValidationError("generated selection card IDs must be unique")
    if not semantic_review_set <= set(generated_candidate_ids):
        raise CardCentricValidationError("semantic review IDs must be generated candidates")

    # Preserve every input identity in the frozen partition but only make
    # structurally valid, independently grounded rows selectable.
    candidate_by_identity: dict[str, _QualitySelectionCandidate] = {}
    for candidate in candidates:
        current = candidate_by_identity.get(candidate.identity)
        if current is None or _candidate_static_key(candidate) < _candidate_static_key(current):
            candidate_by_identity[candidate.identity] = candidate
    selectable = [
        candidate
        for candidate in candidate_by_identity.values()
        if candidate.generated_card_id not in semantic_review_set
        and candidate.existing_note_id not in set(fast_fallback_note_ids)
        and (
            candidate.kind == "existing"
            or candidate.generated_card_id in selectable_generated_ids
        )
    ]
    selectable = _without_dominated_candidates(selectable)
    duplicate_target_identities = {
        candidate.identity for candidate in selectable if candidate.duplicate_target
    }

    selected: list[_QualitySelectionCandidate] = []
    selected_coverage: set[tuple[str, str]] = set()
    selected_concept_coverage: set[str] = set()
    selected_identities: set[str] = set()
    for tier in SelectionTier:
        tier_candidates = [candidate for candidate in selectable if candidate.tier is tier]
        while tier_candidates:
            candidate = min(
                tier_candidates,
                key=lambda item: _candidate_selection_key(item, selected_coverage),
            )
            tier_candidates.remove(candidate)
            count = len(selected)
            remaining_duplicate_targets = tuple(
                target_candidate
                for target_candidate in selectable
                if target_candidate.identity in duplicate_target_identities
                and target_candidate.identity not in selected_identities
            )
            # Reserve capacity only by replacing coverage that is already
            # selected or that the pending exact targets will also cover.  A
            # later S8 identity must never evict unique selected coverage just
            # to remain below the soft cap; that genuine conflict is mandatory
            # overflow and retains the normal acknowledgement requirement.
            if (
                not candidate.mandatory
                and count >= cap - len(remaining_duplicate_targets)
                and _redundant_with_duplicate_targets(
                    candidate,
                    selected_concept_coverage,
                    remaining_duplicate_targets,
                )
            ):
                continue
            if tier is SelectionTier.T6 and count >= minimum and not candidate.mandatory:
                break
            marginal_reason = _marginal_reason(candidate, selected_coverage, concepts)
            if (
                count >= target
                and count < cap
                and marginal_reason is None
                and not candidate.mandatory
            ):
                continue
            if count >= cap:
                if not candidate.mandatory:
                    continue
                if candidate.duplicate_target:
                    replacement = _selected_duplicate_target_replacement(
                        selected,
                        candidate,
                        remaining_duplicate_targets,
                    )
                    if replacement is not None:
                        selected.remove(replacement)
                        selected_identities.remove(replacement.identity)
                        selected_coverage = {
                            coverage
                            for selected_candidate in selected
                            for coverage in selected_candidate.coverage
                        }
                        selected_concept_coverage = {
                            concept_id
                            for selected_candidate in selected
                            for concept_id in selected_candidate.concept_coverage
                        }
            selected.append(candidate)
            selected_identities.add(candidate.identity)
            selected_coverage.update(candidate.coverage)
            selected_concept_coverage.update(candidate.concept_coverage)

    metadata = tuple(
        SelectionMetadata(
            identity=candidate.identity,
            selected_position=index,
            tier=candidate.tier,
            evidence_quality=candidate.evidence_quality,
            mandatory=candidate.mandatory,
            marginal_value_reason=(
                _marginal_reason(candidate, set(), concepts) if 66 <= index <= cap else None
            ),
            overflow_reason=(
                "validated mandatory high-value nonredundant coverage" if index > cap else None
            ),
            manual_acknowledgement_required=index > cap,
        )
        for index, candidate in enumerate(selected, start=1)
    )
    selected_existing = tuple(
        candidate.existing_note_id
        for candidate in selected
        if candidate.existing_note_id is not None
    )
    selected_generated = tuple(
        candidate.generated_card_id
        for candidate in selected
        if candidate.generated_card_id is not None
    )
    selected_existing_set = set(selected_existing)
    selected_generated_set = set(selected_generated)
    acknowledgement = (
        overflow_acknowledgement
        if isinstance(overflow_acknowledgement, CanonicalJsonObject)
        else CanonicalJsonObject.from_mapping(overflow_acknowledgement)
        if overflow_acknowledgement is not None
        else None
    )
    return QualitySelectionResult(
        existing_candidate_note_ids=existing_candidate_ids,
        generated_candidate_card_ids=generated_candidate_ids,
        selected_existing_note_ids=selected_existing,
        selected_generated_card_ids=selected_generated,
        excluded_existing_note_ids=tuple(
            note_id for note_id in existing_candidate_ids if note_id not in selected_existing_set
        ),
        excluded_generated_card_ids=tuple(
            card_id for card_id in generated_candidate_ids if card_id not in selected_generated_set
        ),
        selection_metadata=metadata,
        below_warning_floor=len(selected) < minimum,
        target=target,
        cap=cap,
        minimum_target=minimum,
        mandatory_note_ids=tuple(
            sorted(
                candidate.existing_note_id
                for candidate in selected
                if candidate.mandatory and candidate.existing_note_id is not None
            )
        ),
        mandatory_generated_card_ids=tuple(
            sorted(
                candidate.generated_card_id
                for candidate in selected
                if candidate.mandatory and candidate.generated_card_id is not None
            )
        ),
        semantic_review_required_card_ids=semantic_review_ids,
        overflow_acknowledgement=acknowledgement,
    )


def _candidate_static_key(
    candidate: _QualitySelectionCandidate,
) -> tuple[int, int, int, int, int, str]:
    """The stable portion of the quality order, used for ties and dominance."""
    return (
        -int(candidate.mandatory),
        -_evidence_quality_rank(candidate.evidence_quality),
        candidate.priority,
        candidate.flag_count,
        len(candidate.coverage),
        candidate.identity,
    )


def _redundant_with_duplicate_targets(
    candidate: _QualitySelectionCandidate,
    selected_concept_coverage: set[str],
    pending_duplicate_targets: Sequence[_QualitySelectionCandidate],
) -> bool:
    """Whether withholding a candidate preserves all selected concept coverage."""
    covered_by_selected_or_targets = set(selected_concept_coverage)
    for target in pending_duplicate_targets:
        covered_by_selected_or_targets.update(target.concept_coverage)
    return candidate.concept_coverage <= covered_by_selected_or_targets


def _selected_duplicate_target_replacement(
    selected: Sequence[_QualitySelectionCandidate],
    target: _QualitySelectionCandidate,
    pending_duplicate_targets: Sequence[_QualitySelectionCandidate],
) -> _QualitySelectionCandidate | None:
    """Find the least-preferred selected card safely replaceable by an S8 target."""
    replacements = []
    for candidate in selected:
        if candidate.mandatory:
            continue
        other_concepts = {
            concept_id
            for other in selected
            if other.identity != candidate.identity
            for concept_id in other.concept_coverage
        }
        other_concepts.update(target.concept_coverage)
        other_concepts.update(
            concept_id
            for pending in pending_duplicate_targets
            if pending.identity != target.identity
            for concept_id in pending.concept_coverage
        )
        if candidate.concept_coverage <= other_concepts:
            replacements.append(candidate)
    return max(replacements, key=_candidate_static_key, default=None)


def _candidate_quality_key(candidate: _QualitySelectionCandidate) -> tuple[int, int, int, int]:
    return (
        -int(candidate.mandatory),
        -_evidence_quality_rank(candidate.evidence_quality),
        candidate.priority,
        candidate.flag_count,
    )


def _candidate_selection_key(
    candidate: _QualitySelectionCandidate,
    selected_coverage: set[tuple[str, str]],
) -> tuple[int, int, int, int, int, int, str]:
    return (
        -int(candidate.mandatory),
        -_evidence_quality_rank(candidate.evidence_quality),
        -len(candidate.coverage - selected_coverage),
        candidate.priority,
        candidate.flag_count,
        len(candidate.coverage & selected_coverage),
        candidate.identity,
    )


def _evidence_quality_rank(evidence_quality: EvidenceQuality) -> int:
    return {
        EvidenceQuality.PRIMARY_SOURCE: 2,
        EvidenceQuality.SUMMARY_GROUNDED: 1,
        EvidenceQuality.FAST_PASS: 0,
    }[evidence_quality]


def _without_dominated_candidates(
    candidates: Sequence[_QualitySelectionCandidate],
) -> list[_QualitySelectionCandidate]:
    kept: list[_QualitySelectionCandidate] = []
    for candidate in candidates:
        if candidate.split or candidate.duplicate_target:
            kept.append(candidate)
            continue
        quality = _candidate_quality_key(candidate)
        dominated = any(
            not other.split
            and candidate.coverage <= other.coverage
            and _candidate_quality_key(other) <= quality
            and (
                candidate.coverage != other.coverage
                or _candidate_quality_key(other) != quality
                or other.identity < candidate.identity
            )
            for other in candidates
            if other.identity != candidate.identity
        )
        if not dominated:
            kept.append(candidate)
    return kept


def _marginal_reason(
    candidate: _QualitySelectionCandidate,
    selected_coverage: set[tuple[str, str]],
    concepts: dict[str, CardConcept],
) -> MarginalValueReason | None:
    # The S8 terminal requires this exact selected identity for S9 duplicate
    # conservation even if another card covers the same concept.  It is therefore
    # a governed required-fact reason at positions 66-70.
    if candidate.duplicate_target:
        return MarginalValueReason.ONLY_VALID_REQUIRED_FACT
    if candidate.split:
        return MarginalValueReason.VALIDATED_NECESSARY_SPLIT
    uncovered = candidate.coverage - selected_coverage
    if (
        candidate.kind == "generated" and candidate.priority <= 1 and uncovered
    ) or any(
        kind == "concept"
        and concept_id in concepts
        and concepts[concept_id].importance in {"high", "medium"}
        for kind, concept_id in uncovered
    ):
        return MarginalValueReason.ONLY_VALID_REQUIRED_FACT
    if any(
        kind == "concept"
        and concept_id in concepts
        and concepts[concept_id].emphasis_flag
        and (kind, concept_id) not in selected_coverage
        for kind, concept_id in candidate.coverage
    ):
        return MarginalValueReason.UNIQUE_EMPHASIZED_DISTINCTION
    return None


def select_high_yield(
    classifications: Sequence[CardClassification],
    *,
    ledger: CardConceptLedger,
    source_index: CardCentricSourceIndex,
    generated_card_ids: Sequence[str] = (),
    overflow_acknowledgement: dict[str, str] | None = None,
    target: int = 65,
    cap: int = 70,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[str, ...]]:
    """Stable, evidence-first selection without a minimum-padding rule.

    A clean grounded YES is eligible.  Deep/emphasized/high concepts are protected
    before ordinary cards; then coverage diversity and note ID make every tie
    deterministic.  Generated cards are not used to manufacture a 60-card floor.
    """
    if not 1 <= target <= cap:
        raise CardCentricValidationError("selection target/cap is invalid")
    concepts = {concept.concept_id: concept for concept in ledger.concepts}
    eligible = [item for item in classifications if selection_eligible(item, source_index)]
    if len({item.note_id for item in eligible}) != len(eligible):
        raise CardCentricValidationError("eligible selection note IDs are not unique")

    def rank(item: CardClassification) -> tuple[int, int, int, int, int]:
        covered = [concepts[value] for value in item.covered_concept_ids if value in concepts]
        emphasis = int(any(concept.emphasis_flag for concept in covered))
        high = int(any(concept.importance == "high" for concept in covered))
        depth = max(
            ({"deep": 2, "medium": 1, "surface": 0}[concept.depth] for concept in covered),
            default=0,
        )
        return (-emphasis, -high, -depth, -len(covered), item.note_id)

    ordered = sorted(eligible, key=rank)
    # Mandatory means evidence-backed clean cards covering an emphasized/high
    # concept. They are selected before ordinary target truncation.
    mandatory = [
        item
        for item in ordered
        if any(
            concepts[c].emphasis_flag or concepts[c].importance == "high"
            for c in item.covered_concept_ids
            if c in concepts
        )
    ]
    if len(mandatory) > cap:
        # The stage issues the durable server acknowledgement after it has the
        # canonical selection.  Do not truncate mandatory evidence merely to
        # fit the ordinary cap.
        return (
            tuple(sorted(item.note_id for item in mandatory)),
            tuple(sorted(item.note_id for item in eligible if item not in mandatory)),
            tuple(sorted(set(generated_card_ids))),
        )
    selected: list[CardClassification] = list(mandatory)
    seen_concepts: set[str] = set()
    for item in ordered:
        if item not in selected and (
            any(concept_id not in seen_concepts for concept_id in item.covered_concept_ids)
            and len(selected) < cap
        ):
            selected.append(item)
            seen_concepts.update(item.covered_concept_ids)
    for item in ordered:
        if item not in selected and len(selected) < min(target, cap):
            selected.append(item)
    selected_ids = tuple(sorted(item.note_id for item in selected))
    excluded = tuple(
        sorted(item.note_id for item in eligible if item.note_id not in set(selected_ids))
    )
    # Generated cards are source-grounded coverage candidates, not padding.  If
    # capacity remains, retain a stable subset; the S9 cap invariant prevents
    # accidental expansion during review/envelope construction.
    generated = tuple(sorted(set(generated_card_ids)))[: max(0, cap - len(selected_ids))]
    return selected_ids, excluded, generated


def _to_card_passage(passage: SourcePassage) -> CardCentricPassage:
    if passage.source_kind is SourceKind.SUMMARY:
        authority: Literal["summary", "transcript", "slide"] = "summary"
    elif passage.source_kind is SourceKind.TRANSCRIPT:
        authority = "transcript"
    elif passage.source_kind in {SourceKind.SLIDE, SourceKind.SPEAKER_NOTES}:
        authority = "slide"
    else:
        raise CardCentricValidationError(
            f"source kind {passage.source_kind.value} is not usable in card-centric prefix"
        )
    return CardCentricPassage(
        passage_id=f"{passage.source_id}:P:{passage.passage_id[:16]}",
        source_id=passage.source_id,
        source_kind=authority,
        authority=authority,
        revision_id=passage.revision_id,
        content_sha256=passage.content_hash,
        text=passage.text,
    )


def _scope_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(sorted({value.strip().casefold() for value in tokens if value.strip()}))
    if not values:
        raise CardCentricValidationError("tag scope has no resolved tokens")
    return values


def _matches_scope(tags: Sequence[str], tokens: tuple[str, ...]) -> bool:
    return any(token in tag.casefold() for token in tokens for tag in tags)


def _unique_card_ids(cards: Sequence[CardRecord]) -> None:
    if len({card.note_id for card in cards}) != len(cards):
        raise CardCentricValidationError("snapshot contains duplicate note IDs")


def _batches(cards: Sequence[CardRecord], size: int) -> Iterable[tuple[CardRecord, ...]]:
    for start in range(0, len(cards), size):
        yield tuple(cards[start : start + size])


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
