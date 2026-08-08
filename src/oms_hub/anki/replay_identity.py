"""Canonical prompt/model replay identities for card-centric v2.

I0 integration hook (serial, after S0 preflight): resolve
``AnkiPromptCatalogService.load_card_centric_v2_snapshot()`` once, persist each
returned exact content/hash, and build one ``ResolvedStageModelIdentity`` for
each provider-backed stage from those persisted values.  S4c and S6 must read
the persisted ``card-centric-classifier`` entry rather than a live asset.  S6
must also pass the job's persisted ``semantic_generation`` to
``SemanticIndexService.search(expected_generation=...)``; a
``SemanticGenerationMismatchError`` maps to the existing nonretryable
``PinnedInputChanged`` terminal outcome when found before stage work.

This module deliberately does not resolve providers, models, batch sizes, or
concurrency policy.  Its callers supply the already-persisted resolved values.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence

from oms_hub.anki.correction_contracts import (
    CanonicalJsonObject,
    PromptSnapshotIdentity,
    ResolvedStageModelIdentity,
)
from oms_hub.anki.domain import CurationStage
from oms_hub.anki.prompts import AnkiPrompt


def prompt_snapshot_identity(prompt: AnkiPrompt) -> PromptSnapshotIdentity:
    """Convert an already-resolved prompt into the frozen S0 identity form."""
    return PromptSnapshotIdentity(
        prompt_id=prompt.metadata.id,
        prompt_version=prompt.metadata.version,
        content=prompt.content,
        content_sha256=prompt.content_sha256,
    )


def build_resolved_stage_model_identity(
    *,
    stage: CurationStage,
    prompts: Sequence[AnkiPrompt | PromptSnapshotIdentity],
    provider: str,
    model: str,
    generation_parameters: Mapping[str, object],
    batch_size: int | None = None,
    concurrency: int | None = None,
) -> ResolvedStageModelIdentity:
    """Hash supplied, resolved stage inputs without choosing any policy.

    The resulting canonical JSON is finite, recursively order-stable for
    mappings, and includes every supplied generation parameter plus applicable
    persisted ``batch_size`` and ``concurrency`` values. Prompt sequence order
    is preserved because it is the provider execution order.
    """
    if not provider.strip() or provider != provider.strip():
        raise ValueError("provider must be a nonblank, trimmed resolved value")
    if not model.strip() or model != model.strip():
        raise ValueError("model must be a nonblank, trimmed resolved value")
    if not prompts:
        raise ValueError("at least one resolved prompt is required")
    parameters = _finite_json_object(generation_parameters)
    if batch_size is not None:
        _positive_int("batch_size", batch_size)
        _merge_resolved_integer(parameters, "batch_size", batch_size)
    elif "batch_size" in parameters:
        _positive_int("batch_size", parameters["batch_size"])
    if concurrency is not None:
        _positive_int("concurrency", concurrency)
        _merge_resolved_integer(parameters, "concurrency", concurrency)
    elif "concurrency" in parameters:
        _positive_int("concurrency", parameters["concurrency"])
    canonical_parameters = CanonicalJsonObject.from_mapping(parameters)
    prompt_identities = tuple(
        prompt_snapshot_identity(prompt)
        if isinstance(prompt, AnkiPrompt)
        else prompt
        for prompt in prompts
    )
    payload = {
        "stage": stage.value,
        "provider": provider,
        "model": model,
        "prompts": [prompt.model_dump(mode="json") for prompt in prompt_identities],
        "generation_parameters": canonical_parameters.as_dict(),
    }
    return ResolvedStageModelIdentity(
        stage=stage,
        provider=provider,
        model=model,
        prompts=prompt_identities,
        generation_parameters=canonical_parameters,
        identity_sha256=_sha256(payload),
    )


def _positive_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer when supplied")


def _merge_resolved_integer(
    parameters: dict[str, object],
    name: str,
    value: int,
) -> None:
    existing = parameters.get(name)
    if name in parameters and existing != value:
        raise ValueError(f"{name} conflicts with the supplied generation parameters")
    parameters[name] = value


def _finite_json_object(value: Mapping[str, object]) -> dict[str, object]:
    normalized = _finite_json_value(value)
    if not isinstance(normalized, dict):  # pragma: no cover - Mapping guarantees this
        raise AssertionError("generation parameters are not a JSON object")
    return normalized


def _finite_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("replay identity values must be finite JSON data")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("replay identity values must be finite JSON data")
        return {key: _finite_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_json_value(item) for item in value]
    raise ValueError("replay identity values must be finite JSON data")


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
