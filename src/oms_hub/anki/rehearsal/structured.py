from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from oms_hub.anki.provider_attempts import (
    ProviderAttemptIdentity,
    current_provider_attempt_identity,
    provider_replay_identity_document,
)
from oms_hub.llm.domain import (
    DEFAULT_GENERATION_OPTIONS,
    GeneratedText,
    GenerationOptions,
    ProviderName,
)


class StructuredReplayMiss(RuntimeError):
    """A deterministic structured call was not captured by the capsule."""


@dataclass(slots=True)
class StructuredReplayEvidence:
    hits: int = 0
    misses: int = 0
    live_calls: int = 0


class ReplayStructuredTextGenerator:
    """Content-addressed structured responses with no live-provider fallback."""

    def __init__(self, manifest_path: Path, *, require_attempt_identity: bool = False) -> None:
        self.manifest_path = manifest_path
        self.require_attempt_identity = require_attempt_identity
        self.evidence = StructuredReplayEvidence()

    def generate_text(
        self,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
        provider: ProviderName,
        model: str,
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> GeneratedText:
        identity = current_provider_attempt_identity()
        if self.require_attempt_identity and identity is None:
            raise StructuredReplayMiss("structured replay call has no durable attempt identity")
        key = structured_request_key(
            instruction,
            input_text,
            output_schema=output_schema,
            provider=provider,
            model=model,
            options=options,
            attempt_identity=identity,
        )
        records = self._records()
        raw = records.get(key)
        if not isinstance(raw, dict):
            self.evidence.misses += 1
            raise StructuredReplayMiss(f"missing structured replay response {key}")
        try:
            text = str(raw["text"])
            if hashlib.sha256(text.encode()).hexdigest() != raw["text_sha256"]:
                raise ValueError("response hash changed")
            result = GeneratedText(
                text=text,
                provider=ProviderName(str(raw["provider"])),
                model=str(raw["model"]),
                request_id=str(raw["request_id"]),
                input_tokens=int(raw["input_tokens"]),
                output_tokens=int(raw["output_tokens"]),
                cost_microusd=int(raw["cost_microusd"]),
                cache_creation_input_tokens=int(raw.get("cache_creation_input_tokens", 0)),
                cache_read_input_tokens=int(raw.get("cache_read_input_tokens", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            self.evidence.misses += 1
            raise StructuredReplayMiss(f"invalid structured replay response {key}") from exc
        if result.provider is not provider or result.model != model:
            self.evidence.misses += 1
            raise StructuredReplayMiss(f"structured replay route changed for {key}")
        self.evidence.hits += 1
        return result

    def _records(self) -> dict[str, object]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StructuredReplayMiss("structured replay manifest is unavailable") from exc
        if not isinstance(payload, dict):
            raise StructuredReplayMiss("structured replay manifest is invalid")
        return payload


def structured_request_key(
    instruction: str,
    input_text: str,
    *,
    output_schema: dict[str, object],
    provider: ProviderName,
    model: str,
    options: GenerationOptions,
    attempt_identity: ProviderAttemptIdentity | None = None,
) -> str:
    return structured_request_key_from_hashes(
        provider=provider.value,
        model=model,
        instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest(),
        input_sha256=hashlib.sha256(input_text.encode()).hexdigest(),
        output_schema_sha256=_canonical_sha256(output_schema),
        cache_prefix_sha256=(
            hashlib.sha256(options.cacheable_source_prefix.encode()).hexdigest()
            if options.cacheable_source_prefix is not None
            else None
        ),
        generation_parameters={
            "thinking": options.thinking.value,
            "thinking_budget_tokens": options.thinking_budget_tokens,
            "temperature": options.temperature,
            "max_tokens": options.max_tokens,
        },
        attempt_identity=(
            provider_replay_identity_document(attempt_identity)
            if attempt_identity is not None
            else None
        ),
    )


def structured_request_key_from_hashes(
    *,
    provider: str,
    model: str,
    instruction_sha256: str,
    input_sha256: str,
    output_schema_sha256: str,
    cache_prefix_sha256: str | None,
    generation_parameters: dict[str, object],
    attempt_identity: dict[str, object] | None = None,
) -> str:
    document = {
        "provider": provider,
        "model": model,
        "instruction_sha256": instruction_sha256,
        "input_sha256": input_sha256,
        "output_schema_sha256": output_schema_sha256,
        "cache_prefix_sha256": cache_prefix_sha256,
        "generation_parameters": generation_parameters,
        "attempt_identity": attempt_identity,
    }
    return _canonical_sha256(document)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
