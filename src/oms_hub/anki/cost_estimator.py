"""Deterministic integer-only v3 cost math; no pricing lookup or dispatch."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

_MILLION = 1_000_000


class CostKind(StrEnum):
    PREDICTED = "predicted"
    RESERVED = "reserved"
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class ModelRate:
    model: str
    input_microusd_per_million_tokens: int
    cache_creation_microusd_per_million_tokens: int
    cache_read_microusd_per_million_tokens: int
    output_microusd_per_million_tokens: int
    embedding_microusd_per_million_tokens: int

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model rate needs a nonblank model identity")
        if self.model != self.model.strip():
            object.__setattr__(self, "model", self.model.strip())
        if any(
            type(value) is not int or value < 0
            for value in (
                self.input_microusd_per_million_tokens,
                self.cache_creation_microusd_per_million_tokens,
                self.cache_read_microusd_per_million_tokens,
                self.output_microusd_per_million_tokens,
                self.embedding_microusd_per_million_tokens,
            )
        ):
            raise ValueError("model rate must have a name and nonnegative integer prices")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.input_tokens,
                self.cache_creation_tokens,
                self.cache_read_tokens,
                self.output_tokens,
                self.embedding_tokens,
            )
        ):
            raise ValueError("token usage cannot be negative")


@dataclass(frozen=True, slots=True)
class FrozenRateTable:
    rates: tuple[ModelRate, ...]
    effective_at: datetime
    source: str
    currency: str = "USD"
    rate_table_sha256: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.rates, tuple):
            object.__setattr__(self, "rates", tuple(self.rates))
        if not all(isinstance(rate, ModelRate) for rate in self.rates):
            raise ValueError("rate table entries must be model rates")
        if self.effective_at.tzinfo is None or self.effective_at.utcoffset() is None:
            raise ValueError("rate table effective_at must be timezone-aware")
        if not self.source.strip() or self.currency != "USD":
            raise ValueError("rate table requires a nonblank source and USD currency")
        if self.source != self.source.strip():
            object.__setattr__(self, "source", self.source.strip())
        models = tuple(rate.model for rate in self.rates)
        if not models or models != tuple(sorted(models)) or len(models) != len(set(models)):
            raise ValueError(
                "rate table model identities must be uniquely and deterministically ordered"
            )
        payload = json.dumps(
            {
                "effective_at": self.effective_at.isoformat(),
                "source": self.source,
                "currency": self.currency,
                "rates": [
                    {
                        "model": rate.model,
                        "input_microusd_per_million_tokens": rate.input_microusd_per_million_tokens,
                        "cache_creation_microusd_per_million_tokens": (
                            rate.cache_creation_microusd_per_million_tokens
                        ),
                        "cache_read_microusd_per_million_tokens": (
                            rate.cache_read_microusd_per_million_tokens
                        ),
                        "output_microusd_per_million_tokens": (
                            rate.output_microusd_per_million_tokens
                        ),
                        "embedding_microusd_per_million_tokens": (
                            rate.embedding_microusd_per_million_tokens
                        ),
                    }
                    for rate in self.rates
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(payload.encode()).hexdigest()
        if self.rate_table_sha256 not in {"", expected}:
            raise ValueError("rate table hash does not match its canonical rates")
        if not self.rate_table_sha256:
            object.__setattr__(self, "rate_table_sha256", expected)

    def rate_for(self, model: str) -> ModelRate:
        for rate in self.rates:
            if rate.model == model:
                return rate
        raise KeyError(model)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    kind: CostKind
    microusd: int
    usage: TokenUsage


@dataclass(frozen=True, slots=True)
class CostEstimates:
    predicted: CostEstimate
    reserved: CostEstimate
    observed: CostEstimate

    def __post_init__(self) -> None:
        if (self.predicted.kind, self.reserved.kind, self.observed.kind) != tuple(CostKind):
            raise ValueError(
                "cost estimates must retain predicted, reserved, and observed separately"
            )


def _ceil_cost(tokens: int, microusd_per_million_tokens: int) -> int:
    return (tokens * microusd_per_million_tokens + _MILLION - 1) // _MILLION


def estimate_cost(kind: CostKind, rate: ModelRate, usage: TokenUsage) -> CostEstimate:
    microusd = sum(
        _ceil_cost(tokens, rate_value)
        for tokens, rate_value in (
            (usage.input_tokens, rate.input_microusd_per_million_tokens),
            (usage.cache_creation_tokens, rate.cache_creation_microusd_per_million_tokens),
            (usage.cache_read_tokens, rate.cache_read_microusd_per_million_tokens),
            (usage.output_tokens, rate.output_microusd_per_million_tokens),
            (usage.embedding_tokens, rate.embedding_microusd_per_million_tokens),
        )
    )
    return CostEstimate(kind=kind, microusd=microusd, usage=usage)
