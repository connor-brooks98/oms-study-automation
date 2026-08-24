"""Deterministic R3 scope generation over policy-authorized evidence only."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms_hub.anki.contracts import canonical_payload_sha256
from oms_hub.anki.course_policy import CourseCurationPolicy
from oms_hub.anki.domain import ResolvedStageModel, SourceKind, StageUsage
from oms_hub.anki.fidelity_audit import R2FidelityDiagnostic
from oms_hub.anki.prompts import AnkiPrompt, PromptMetadata
from oms_hub.anki.provider_attempts import ProviderCallHandle, provider_call_scope
from oms_hub.anki.scope_contracts import (
    LectureScope,
    ScopedConcept,
    ScopedFact,
    ScopeEvidenceReference,
)
from oms_hub.anki.sources import SourceEmphasisEvidence, SourcePassage
from oms_hub.llm.domain import GenerationOptions, ProviderName, ThinkingMode
from oms_hub.llm.structured import StructuredTextService

_SOURCE_BUNDLE_VERSION = "scope-source-bundle-v1"
_REQUEST_VERSION = "scope-request-v1"


class ScopeInputError(ValueError):
    """The frozen R3 inputs cannot safely reach a provider."""


@dataclass(frozen=True, slots=True)
class PinnedScopePrompt:
    """Full immutable prompt provenance, independent of a live prompt file."""

    id: str
    version: str
    content: str
    content_sha256: str
    metadata: PromptMetadata

    def __post_init__(self) -> None:
        content = self.content.strip()
        if (
            self.id != "card-centric-scope-v3"
            or self.version != self.metadata.version
            or self.id != self.metadata.id
            or not content
            or self.metadata.model is not None
            or self.metadata.response_format != "json"
            or self.metadata.schema_name != "scope_v3"
        ):
            raise ScopeInputError("pinned scope prompt is invalid")
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if self.content_sha256 != expected:
            raise ScopeInputError("pinned scope prompt content hash does not match")
        object.__setattr__(self, "content", content)

    @classmethod
    def from_prompt(cls, prompt: AnkiPrompt) -> PinnedScopePrompt:
        return cls(
            id=prompt.metadata.id,
            version=prompt.metadata.version,
            content=prompt.content,
            content_sha256=prompt.content_sha256,
            metadata=prompt.metadata,
        )


@dataclass(frozen=True, slots=True)
class ScopeReuseArtifact:
    scope: LectureScope
    scope_request_sha256: str

    def __post_init__(self) -> None:
        if not _is_sha256(self.scope_request_sha256):
            raise ScopeInputError("scope reuse request hash is invalid")


@dataclass(frozen=True, slots=True)
class ScopeGenerationResult:
    scope: LectureScope
    provider_input: dict[str, object]
    source_bundle: dict[str, object]
    source_bundle_sha256: str
    scope_request_sha256: str
    prompt_id: str
    prompt_version: str
    prompt_content_sha256: str
    route: dict[str, object]
    output_schema_sha256: str
    usage: StageUsage | None
    attempt_handle: ProviderCallHandle | None
    reused: bool


@dataclass(frozen=True, slots=True)
class _SelectedEvidence:
    evidence_type: Literal["colored_text", "transcript", "outline"]
    evidence_id: str
    source_id: str
    locator: str
    normalized_text: str
    content_sha256: str
    source_kind: SourceKind | None = None
    revision_id: int | None = None

    def document(self) -> dict[str, str]:
        document: dict[str, str] = {
            "evidence_type": self.evidence_type,
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "locator": self.locator,
            "normalized_text": self.normalized_text,
            "content_sha256": self.content_sha256,
        }
        if self.revision_id is not None:
            document["revision_id"] = str(self.revision_id)
        if self.source_kind is not None:
            document["source_kind"] = self.source_kind.value
        return document


class ScopeService:
    def __init__(self, structured: StructuredTextService) -> None:
        self.structured = structured

    def generate_scope(
        self,
        *,
        policy: CourseCurationPolicy,
        fidelity: R2FidelityDiagnostic,
        source_passages: Sequence[SourcePassage],
        emphasis_evidence: Sequence[SourceEmphasisEvidence],
        prompt: PinnedScopePrompt,
        route: ResolvedStageModel,
        model_config_sha256: str,
        existing: ScopeReuseArtifact | None = None,
        require_v3_provenance: bool = False,
    ) -> ScopeGenerationResult:
        if not _is_sha256(model_config_sha256):
            raise ScopeInputError("model configuration hash is invalid")
        if fidelity.policy_sha256 != policy.policy_sha256:
            raise ScopeInputError("fidelity policy identity changed")
        _validate_fidelity_inputs(policy, fidelity, source_passages, emphasis_evidence)
        selected, degraded_mode = _select_evidence(
            policy, fidelity, source_passages, emphasis_evidence, require_v3_provenance
        )
        evidence_document = [item.document() for item in selected]
        source_bundle: dict[str, object] = {
            "serialization_version": _SOURCE_BUNDLE_VERSION,
            "degraded_mode": degraded_mode,
            "evidence": evidence_document,
        }
        source_bundle_sha256 = canonical_payload_sha256(source_bundle)
        output_model = _scope_output_model({item.evidence_id for item in selected})
        output_schema_sha256 = canonical_payload_sha256(output_model.model_json_schema())
        route_document = _route_document(route)
        options = _generation_options(prompt, route)
        request_document = {
            "serialization_version": _REQUEST_VERSION,
            "policy_sha256": policy.policy_sha256,
            "source_bundle_sha256": source_bundle_sha256,
            "prompt_content_sha256": prompt.content_sha256,
            "route": route_document,
            "output_schema_sha256": output_schema_sha256,
            "generation_options": _options_document(options),
            "model_config_sha256": model_config_sha256,
        }
        scope_request_sha256 = canonical_payload_sha256(request_document)
        provider_input: dict[str, object] = {
            "serialization_version": "scope-provider-input-v1",
            "policy": policy.model_dump(mode="json"),
            "fidelity": {
                "status": fidelity.status,
                "degraded_mode": degraded_mode,
            },
            "source_bundle": source_bundle,
            "scope_instruction": policy.scope_instruction,
        }
        reused = _valid_reuse(
            existing,
            scope_request_sha256=scope_request_sha256,
            policy_sha256=policy.policy_sha256,
            source_bundle_sha256=source_bundle_sha256,
            degraded_mode=degraded_mode,
            generation_allowed=policy.generation_style_profile != "disabled",
        )
        if reused is not None:
            return ScopeGenerationResult(
                scope=reused,
                provider_input=provider_input,
                source_bundle=source_bundle,
                source_bundle_sha256=source_bundle_sha256,
                scope_request_sha256=scope_request_sha256,
                prompt_id=prompt.id,
                prompt_version=prompt.version,
                prompt_content_sha256=prompt.content_sha256,
                route=route_document,
                output_schema_sha256=output_schema_sha256,
                usage=None,
                attempt_handle=None,
                reused=True,
            )
        with provider_call_scope(batch_index=0):
            generated = self.structured.generate_json(
                prompt.content,
                _canonical_json(provider_input),
                output_model=output_model,
                provider=ProviderName(route.provider),
                model=route.model,
                options=options,
            )
        scope = _lecture_scope(
            generated.value,
            selected=selected,
            policy_sha256=policy.policy_sha256,
            source_bundle_sha256=source_bundle_sha256,
            scope_request_sha256=scope_request_sha256,
            degraded_mode=degraded_mode,
            generation_allowed=policy.generation_style_profile != "disabled",
        )
        return ScopeGenerationResult(
            scope=scope,
            provider_input=provider_input,
            source_bundle=source_bundle,
            source_bundle_sha256=source_bundle_sha256,
            scope_request_sha256=scope_request_sha256,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            prompt_content_sha256=prompt.content_sha256,
            route=route_document,
            output_schema_sha256=output_schema_sha256,
            usage=StageUsage(
                generated.request_id,
                generated.input_tokens,
                generated.output_tokens,
                generated.cost_microusd,
            ),
            attempt_handle=generated.attempt_handle,
            reused=False,
        )


def _select_evidence(
    policy: CourseCurationPolicy,
    fidelity: R2FidelityDiagnostic,
    source_passages: Sequence[SourcePassage],
    emphasis_evidence: Sequence[SourceEmphasisEvidence],
    require_v3_provenance: bool = False,
) -> tuple[tuple[_SelectedEvidence, ...], Literal["none", "transcript_outline"]]:
    if (
        fidelity.status
        in {
            "blocked",
            "confirmation_required",
            "blocked_fallback_unavailable",
        }
        or not fidelity.may_advance
    ):
        raise ScopeInputError("fidelity blocks scope generation")
    _validate_evidence_namespace(source_passages, emphasis_evidence)
    passages = tuple(
        _passage_evidence(passage) for passage in source_passages if passage.text.strip()
    )
    passages_by_identity: dict[tuple[str, str], SourcePassage] = {}
    for passage in source_passages:
        identity = (passage.source_id.strip(), passage.locator.strip())
        if identity in passages_by_identity:
            raise ScopeInputError("duplicate source passage identity is ambiguous")
        passages_by_identity[identity] = passage
    emphasis = tuple(
        _emphasis(
            item,
            policy.policy_sha256,
            passages_by_identity.get((item.source_id.strip(), item.locator.strip())),
            require_v3_provenance,
        )
        for item in emphasis_evidence
        if item.text.strip()
    )
    if fidelity.status == "continue_degraded":
        if policy.emphasis_mode not in {"colored_text", "combined"}:
            raise ScopeInputError("degraded fidelity conflicts with policy mode")
        selected = tuple(
            item
            for item in passages
            if item.source_kind in {SourceKind.TRANSCRIPT, SourceKind.SUMMARY}
        )
        degraded_mode: Literal["none", "transcript_outline"] = "transcript_outline"
    elif policy.emphasis_mode == "colored_text" and fidelity.status == "continue":
        selected = emphasis
        degraded_mode = "none"
    elif policy.emphasis_mode == "combined" and fidelity.status == "continue":
        selected = (
            *emphasis,
            *(
                item
                for item in passages
                if item.source_kind in {SourceKind.TRANSCRIPT, SourceKind.SUMMARY}
            ),
        )
        degraded_mode = "none"
    elif policy.emphasis_mode == "transcript_emphasis" and fidelity.status == "not_applicable":
        selected = tuple(item for item in passages if item.source_kind is SourceKind.TRANSCRIPT)
        degraded_mode = "none"
    elif policy.emphasis_mode == "outline_depth" and fidelity.status == "not_applicable":
        selected = tuple(item for item in passages if item.source_kind is SourceKind.SUMMARY)
        degraded_mode = "none"
    else:
        raise ScopeInputError("fidelity outcome does not authorize this policy mode")
    ordered = _unique_selected(selected)
    if not ordered:
        raise ScopeInputError("scope generation has no policy-authorized evidence")
    return ordered, degraded_mode


def _passage_evidence(passage: SourcePassage) -> _SelectedEvidence:
    text = _normalized_evidence_text(passage.text)
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != passage.content_hash:
        raise ScopeInputError("source passage content hash does not match")
    return _SelectedEvidence(
        "transcript" if passage.source_kind is SourceKind.TRANSCRIPT else "outline",
        passage.passage_id,
        passage.source_id.strip(),
        passage.locator.strip(),
        text,
        passage.content_hash,
        passage.source_kind,
        passage.revision_id,
    )


def _emphasis(
    item: SourceEmphasisEvidence,
    policy_sha256: str,
    source: SourcePassage | None,
    require_v3_provenance: bool,
) -> _SelectedEvidence:
    if item.policy_sha256 != policy_sha256 or not item.policy_match:
        raise ScopeInputError("emphasis evidence policy identity changed")
    if require_v3_provenance and (
        source is None
        or source.source_kind is not SourceKind.SLIDE
        or source.revision_id is None
        or item.source_kind is not SourceKind.SLIDE
        or item.revision_id is None
        or source.revision_id != item.revision_id
        or source.source_id != item.source_id
        or source.locator != item.locator
        or source.content_hash != item.normalized_text_sha256
    ):
        raise ScopeInputError("colored evidence lacks frozen slide provenance")
    return _SelectedEvidence(
        "colored_text",
        item.evidence_id,
        item.source_id.strip(),
        item.locator.strip(),
        _normalized_evidence_text(item.text),
        item.normalized_text_sha256,
        None if source is None else source.source_kind,
        None if source is None else source.revision_id,
    )


def _unique_selected(values: Sequence[_SelectedEvidence]) -> tuple[_SelectedEvidence, ...]:
    by_id: dict[str, _SelectedEvidence] = {}
    for value in values:
        if not value.evidence_id.strip() or not value.source_id or not value.locator:
            raise ScopeInputError("scope evidence identity is blank")
        previous = by_id.get(value.evidence_id)
        if previous is not None:
            if previous == value:
                raise ScopeInputError("duplicate scope evidence ID")
            raise ScopeInputError("conflicting scope evidence ID")
        by_id[value.evidence_id] = value
    return tuple(by_id[key] for key in sorted(by_id))


def _validate_evidence_namespace(
    passages: Sequence[SourcePassage],
    emphasis: Sequence[SourceEmphasisEvidence],
) -> None:
    identities: dict[str, tuple[object, ...]] = {}
    for passage in passages:
        identity = (
            "passage",
            passage.source_id,
            passage.locator,
            passage.text,
            passage.content_hash,
        )
        _record_evidence_identity(identities, passage.passage_id, identity)
    for item in emphasis:
        identity = (
            "emphasis",
            item.source_id,
            item.locator,
            item.text,
            item.normalized_text_sha256,
        )
        _record_evidence_identity(identities, item.evidence_id, identity)


def _record_evidence_identity(
    identities: dict[str, tuple[object, ...]],
    evidence_id: str,
    identity: tuple[object, ...],
) -> None:
    previous = identities.get(evidence_id)
    if previous is not None:
        if previous == identity:
            raise ScopeInputError("duplicate scope evidence ID")
        raise ScopeInputError("conflicting scope evidence ID")
    identities[evidence_id] = identity


def _validate_fidelity_inputs(
    policy: CourseCurationPolicy,
    fidelity: R2FidelityDiagnostic,
    passages: Sequence[SourcePassage],
    emphasis: Sequence[SourceEmphasisEvidence],
) -> None:
    nonblank_emphasis = tuple(item for item in emphasis if item.text.strip())
    transcript_count = sum(
        passage.source_kind is SourceKind.TRANSCRIPT and bool(passage.text.strip())
        for passage in passages
    )
    outline_count = sum(
        passage.source_kind is SourceKind.SUMMARY and bool(passage.text.strip())
        for passage in passages
    )
    if (
        len(nonblank_emphasis) != fidelity.matching_colored_count
        or transcript_count != fidelity.transcript_count
        or outline_count != fidelity.outline_count
        or any(
            item.policy_sha256 != policy.policy_sha256
            or item.source_sha256 != fidelity.source_sha256
            or item.sidecar_sha256 != fidelity.sidecar_sha256
            for item in emphasis
        )
    ):
        raise ScopeInputError("fidelity diagnostic does not match supplied evidence")


def _scope_output_model(allowed_evidence_ids: set[str]) -> type[BaseModel]:
    allowed = frozenset(allowed_evidence_ids)
    short_item = Annotated[str, Field(min_length=1, max_length=60)]
    query_item = Annotated[str, Field(min_length=1, max_length=120)]
    evidence_id = Annotated[str, Field(json_schema_extra={"enum": sorted(allowed)})]

    class SemanticFact(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        statement: str = Field(min_length=1, max_length=200)
        evidence_ids: tuple[evidence_id, ...] = Field(min_length=1, max_length=1)
        generation_allowed: bool
        forbidden_cloze_targets: tuple[short_item, ...] = Field(default=(), max_length=2)

        @field_validator("statement", mode="before")
        @classmethod
        def trim_statement(cls, value: object) -> str:
            return _trim(value)

        @field_validator("evidence_ids", "forbidden_cloze_targets", mode="before")
        @classmethod
        def ordered_values(cls, value: object) -> tuple[str, ...]:
            return _ordered_values(value)

        @model_validator(mode="after")
        def citations_close(self) -> SemanticFact:
            if not set(self.evidence_ids) <= allowed:
                raise ValueError("fact cites evidence outside the authorized bundle")
            return self

    class SemanticConcept(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        canonical_statement: str = Field(min_length=1, max_length=140)
        primary_entity: str = Field(min_length=1, max_length=60)
        aliases: tuple[short_item, ...] = Field(default=(), max_length=2)
        exact_terms: tuple[short_item, ...] = Field(default=(), max_length=2)
        depth_tier: int = Field(ge=0, le=20)
        priority: int = Field(ge=0, le=100)
        facts: tuple[SemanticFact, ...] = Field(min_length=1, max_length=2)
        source_evidence_ids: tuple[evidence_id, ...] = Field(min_length=1, max_length=2)
        retrieval_queries: tuple[query_item, ...] = Field(min_length=1, max_length=2)

        @field_validator("canonical_statement", "primary_entity", mode="before")
        @classmethod
        def trim_text(cls, value: object) -> str:
            return _trim(value)

        @field_validator(
            "aliases",
            "exact_terms",
            "source_evidence_ids",
            "retrieval_queries",
            mode="before",
        )
        @classmethod
        def ordered_values(cls, value: object) -> tuple[str, ...]:
            return _ordered_values(value)

        @model_validator(mode="after")
        def citations_close(self) -> SemanticConcept:
            if not set(self.source_evidence_ids) <= allowed:
                raise ValueError("concept cites evidence outside the authorized bundle")
            if any(
                not set(fact.evidence_ids) <= set(self.source_evidence_ids) for fact in self.facts
            ):
                raise ValueError("fact evidence escapes its concept evidence")
            return self

    class SemanticScope(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        concepts: tuple[SemanticConcept, ...] = Field(min_length=1, max_length=14)

        @model_validator(mode="after")
        def normalized_statements_are_unique(self) -> SemanticScope:
            concepts = [_normalized_key(item.canonical_statement) for item in self.concepts]
            facts = [
                _normalized_key(fact.statement)
                for concept in self.concepts
                for fact in concept.facts
            ]
            if len(concepts) != len(set(concepts)):
                raise ValueError("concept statements must be globally unique")
            if len(facts) != len(set(facts)):
                raise ValueError("fact statements must be globally unique")
            return self

    return SemanticScope


def _lecture_scope(
    semantic: BaseModel,
    *,
    selected: Sequence[_SelectedEvidence],
    policy_sha256: str,
    source_bundle_sha256: str,
    scope_request_sha256: str,
    degraded_mode: Literal["none", "transcript_outline"],
    generation_allowed: bool,
) -> LectureScope:
    """Mechanically project the already request-bound, validated semantic model."""
    concepts: list[ScopedConcept] = []
    for concept_index, semantic_concept in enumerate(semantic.concepts, start=1):  # type: ignore[attr-defined]
        concept_id = f"concept-{concept_index:08d}"
        facts = tuple(
            ScopedFact(
                fact_id=f"{concept_id}-fact-{fact_index:08d}",
                statement=fact.statement,
                evidence_ids=fact.evidence_ids,
                generation_allowed=generation_allowed,
                forbidden_cloze_targets=fact.forbidden_cloze_targets,
            )
            for fact_index, fact in enumerate(semantic_concept.facts, start=1)
        )
        concepts.append(
            ScopedConcept(
                concept_id=concept_id,
                canonical_statement=semantic_concept.canonical_statement,
                primary_entity=semantic_concept.primary_entity,
                aliases=semantic_concept.aliases,
                exact_terms=semantic_concept.exact_terms,
                depth_tier=semantic_concept.depth_tier,
                priority=semantic_concept.priority,
                reason=semantic_concept.canonical_statement,
                facts=facts,
                source_evidence_ids=semantic_concept.source_evidence_ids,
                professor_policy_basis=(),
                retrieval_queries=semantic_concept.retrieval_queries,
            )
        )
    evidence = tuple(
        ScopeEvidenceReference(
            evidence_id=item.evidence_id,
            source_id=item.source_id,
            revision_id=item.revision_id,
            source_kind=None if item.source_kind is None else item.source_kind.value,
            locator=item.locator,
            content_sha256=item.content_sha256,
        )
        for item in selected
    )
    return LectureScope(
        scope_id=f"scope-{scope_request_sha256}",
        policy_sha256=policy_sha256,
        source_bundle_sha256=source_bundle_sha256,
        degraded_mode=degraded_mode,
        evidence=evidence,
        concepts=tuple(concepts),
    )


def _valid_reuse(
    existing: ScopeReuseArtifact | None,
    *,
    scope_request_sha256: str,
    policy_sha256: str,
    source_bundle_sha256: str,
    degraded_mode: Literal["none", "transcript_outline"],
    generation_allowed: bool,
) -> LectureScope | None:
    if existing is None or existing.scope_request_sha256 != scope_request_sha256:
        return None
    try:
        scope = LectureScope.model_validate(existing.scope.model_dump(mode="json"))
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        scope.policy_sha256 != policy_sha256
        or scope.source_bundle_sha256 != source_bundle_sha256
        or scope.degraded_mode != degraded_mode
        or scope.scope_id != f"scope-{scope_request_sha256}"
        or any(
            fact.generation_allowed is not generation_allowed
            for concept in scope.concepts
            for fact in concept.facts
        )
    ):
        return None
    return scope


def _generation_options(prompt: PinnedScopePrompt, route: ResolvedStageModel) -> GenerationOptions:
    if route.thinking_mode == "default":
        raise ScopeInputError("scope route must declare enabled or disabled thinking")
    return GenerationOptions(
        cacheable_source_prefix=prompt.content if prompt.metadata.cache_prefix else None,
        thinking=(
            ThinkingMode.ENABLED if route.thinking_mode == "enabled" else ThinkingMode.DISABLED
        ),
        thinking_budget_tokens=1024,
        temperature=prompt.metadata.temperature if prompt.metadata.temperature is not None else 0.0,
        max_tokens=prompt.metadata.max_tokens if prompt.metadata.max_tokens is not None else 4096,
    )


def _route_document(route: ResolvedStageModel) -> dict[str, object]:
    return {
        "provider": route.provider,
        "model": route.model,
        "thinking_mode": route.thinking_mode,
        "fixture_validation_signature": route.fixture_validation_signature,
    }


def _options_document(options: GenerationOptions) -> dict[str, object]:
    return {
        "cacheable_source_prefix_sha256": (
            hashlib.sha256(options.cacheable_source_prefix.encode("utf-8")).hexdigest()
            if options.cacheable_source_prefix is not None
            else None
        ),
        "thinking": options.thinking.value,
        "thinking_budget_tokens": options.thinking_budget_tokens,
        "temperature": options.temperature,
        "max_tokens": options.max_tokens,
    }


def _ordered_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("set-like values must be an array")
    values = tuple(_trim(item) for item in value)
    keys = tuple(_normalized_key(item) for item in values)
    if not all(values) or len(keys) != len(set(keys)):
        raise ValueError("set-like values must be nonblank and unique")
    return tuple(sorted(values))


def _trim(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("text value must be a string")
    return value.strip()


def _normalized_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _normalized_evidence_text(value: str) -> str:
    return " ".join(value.split())


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
