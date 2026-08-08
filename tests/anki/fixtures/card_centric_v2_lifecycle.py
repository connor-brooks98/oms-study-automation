"""Deterministic support for the real-handler card-centric v2 lifecycle fixture.

P4 extends this foundation into the full S2--S9 lifecycle.  The script keeps
provider outputs explicit and ordered while the harness invokes the production
``CurationServicesRunner`` dispatch instead of replacing stage handlers.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from oms_hub.anki.domain import CurationJob, CurationStage
from oms_hub.anki.pipeline import StageContext, StageProduct
from oms_hub.anki.semantic.domain import FloatMatrix, InputType
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.llm.domain import (
    DEFAULT_GENERATION_OPTIONS,
    GeneratedText,
    GenerationOptions,
    ProviderName,
)


@dataclass(frozen=True, slots=True)
class LifecycleProviderScript:
    """All deterministic provider and terminal-stage inputs needed by P4."""

    ledger: Mapping[str, object]
    fast_batches: tuple[Mapping[str, object], ...]
    thorough_batches: tuple[Mapping[str, object], ...]
    gap_batches: tuple[Mapping[str, object], ...]
    embeddings: Mapping[str, tuple[float, ...]]
    selection_payload: Mapping[str, object]
    reconciliation_payload: Mapping[str, object]

    def structured_responses(self) -> tuple[Mapping[str, object], ...]:
        return (
            self.ledger,
            *self.fast_batches,
            *self.thorough_batches,
            *self.gap_batches,
        )


class DeterministicStructuredGenerator:
    """FIFO structured-output double for S2, S4b, S4c/S6, and S7."""

    def __init__(self, responses: Sequence[Mapping[str, object]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, ProviderName, str]] = []

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
        del output_schema, options
        if not self._responses:
            raise AssertionError("deterministic structured response script is exhausted")
        self.calls.append((instruction, input_text, provider, model))
        response = self._responses.pop(0)
        return GeneratedText(
            text=json.dumps(response, sort_keys=True, separators=(",", ":")),
            provider=provider,
            model=model,
            request_id=f"fixture-{len(self.calls):03d}",
            input_tokens=0,
            output_tokens=0,
            cost_microusd=0,
        )


class DeterministicEmbeddingClient:
    """Exact-vector embedding double for S8 and semantic lifecycle stages."""

    def __init__(self, vectors: Mapping[str, tuple[float, ...]]) -> None:
        self._vectors = dict(vectors)
        self.calls: list[tuple[InputType, tuple[str, ...]]] = []

    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        ordered = tuple(texts)
        self.calls.append((input_type, ordered))
        try:
            vectors = [self._vectors[text] for text in ordered]
        except KeyError as exc:
            raise AssertionError(f"missing deterministic embedding for {exc.args[0]!r}") from exc
        return np.asarray(vectors, dtype=np.float32)


@dataclass(slots=True)
class CardCentricV2LifecycleHarness:
    """Invoke production stage dispatch and retain every exposed stage product."""

    runner: CurationServicesRunner
    products: dict[CurationStage, StageProduct] = field(default_factory=dict)

    async def invoke(
        self,
        *,
        job: CurationJob,
        stage: CurationStage,
        prior_payloads: Mapping[CurationStage, dict[str, Any]],
    ) -> StageProduct:
        product = await self.runner.run(
            StageContext(
                job=job,
                stage=stage,
                input_sha256="f" * 64,
                prior_artifacts=(),
                prior_payloads=prior_payloads,
            )
        )
        self.products[stage] = product
        return product

    def exposed_payloads(self) -> dict[CurationStage, dict[str, Any]]:
        return {stage: product.payload for stage, product in self.products.items()}
