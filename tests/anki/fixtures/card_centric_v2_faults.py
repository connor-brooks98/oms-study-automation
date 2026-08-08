"""Deterministic fault doubles shared by the P4-B v2 fault matrices.

The doubles only fail at provider, semantic, or artifact boundaries.  Tests
continue to invoke the production exception classifier and stage contracts.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from oms_hub.anki.pipeline import PinnedInputChanged
from oms_hub.anki.semantic.domain import FloatMatrix, InputType
from oms_hub.llm.domain import (
    DiagnosticSource,
    GeneratedText,
    LLMRequestError,
    ProviderName,
)
from oms_hub.llm.structured import StructuredOutputError

RETRYABLE_PROVIDER_SOURCES = (
    DiagnosticSource.NETWORK,
    DiagnosticSource.QUOTA,
    DiagnosticSource.SERVICE,
)


def provider_fault(source: DiagnosticSource) -> LLMRequestError:
    """Create an explicitly classified provider-boundary failure."""
    return LLMRequestError(f"fixture provider failure: {source.value}", source=source)


def malformed_structured_output(*, provider: ProviderName, model: str) -> StructuredOutputError:
    """Create malformed structured output with the normal generation evidence."""
    generation = GeneratedText(
        text="{not valid JSON",
        provider=provider,
        model=model,
        request_id="fault-malformed-json",
        input_tokens=1,
        output_tokens=1,
        cost_microusd=1,
    )
    return StructuredOutputError(
        "fixture malformed structured output",
        raw_text=generation.text,
        generation=generation,
    )


@dataclass(slots=True)
class FaultingStructuredService:
    """Boundary double that either raises a supplied fault or returns a value."""

    value: Any | None = None
    fault: Exception | None = None
    request_id: str = "fault-fixture-request"

    def generate_json(
        self,
        _instruction: str,
        _input_text: str,
        **kwargs: Any,
    ) -> Any:
        if self.fault is not None:
            raise self.fault
        from oms_hub.llm.structured import StructuredJSONResult

        if self.value is None:
            raise AssertionError("fault fixture has neither a value nor a fault")
        provider = kwargs["provider"]
        model = kwargs["model"]
        return StructuredJSONResult(
            value=self.value,
            raw_text=self.value.model_dump_json(),
            provider=provider,
            model=model,
            request_id=self.request_id,
            input_tokens=1,
            output_tokens=1,
            cost_microusd=1,
        )


@dataclass(slots=True)
class GenerationSwitchingSemantic:
    """Semantic boundary double which records the generation it actually used."""

    before_search: str
    after_search: str
    hits: Mapping[str, list[object]]
    calls: list[dict[str, object]]

    async def search(self, queries: tuple[str, ...], **kwargs: Any) -> list[list[object]]:
        call = {"queries": queries, **kwargs, "generation": self.after_search}
        self.calls.append(call)
        expected_generation = kwargs.get("expected_generation")
        if expected_generation == self.before_search and self.after_search != expected_generation:
            raise PinnedInputChanged("Pinned semantic generation changed before residual search")
        return [self.hits.get(query, []) for query in queries]


@dataclass(frozen=True, slots=True)
class InvalidEmbeddingClient:
    """Return an explicit invalid semantic matrix at the embedding boundary."""

    matrix: tuple[tuple[float, ...], ...]

    async def embed(self, _texts: object, *, input_type: InputType) -> FloatMatrix:
        del input_type
        return np.asarray(self.matrix, dtype=np.float32)


@dataclass(frozen=True, slots=True)
class FaultingEmbeddingClient:
    """Raise a semantic-provider failure without replacing dedupe decisions."""

    fault: Exception

    async def embed(self, _texts: object, *, input_type: InputType) -> FloatMatrix:
        del input_type
        raise self.fault


@dataclass(slots=True)
class CountingOutageEmbeddingClient:
    """Count each real dedupe attempt while always preserving the outage fault."""

    fault: Exception
    calls: int = 0

    async def embed(self, _texts: object, *, input_type: InputType) -> FloatMatrix:
        del input_type
        self.calls += 1
        raise self.fault
