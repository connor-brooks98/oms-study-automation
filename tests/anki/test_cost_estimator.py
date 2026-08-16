from datetime import UTC, datetime

import pytest

from oms_hub.anki.cost_estimator import (
    CostEstimates,
    CostKind,
    FrozenRateTable,
    ModelRate,
    TokenUsage,
    estimate_cost,
)


def test_cost_rounds_each_category_up_with_integer_math() -> None:
    rate = ModelRate("model", 1, 2, 3, 4, 5)
    estimate = estimate_cost(
        CostKind.PREDICTED,
        rate,
        TokenUsage(
            input_tokens=1,
            cache_creation_tokens=1,
            cache_read_tokens=1,
            output_tokens=1,
            embedding_tokens=1,
        ),
    )
    assert estimate.microusd == 5


def test_predicted_reserved_and_observed_remain_distinct() -> None:
    rate = ModelRate("model", 1_000_000, 0, 0, 0, 0)
    predicted = estimate_cost(CostKind.PREDICTED, rate, TokenUsage(input_tokens=1))
    reserved = estimate_cost(CostKind.RESERVED, rate, TokenUsage(input_tokens=2))
    observed = estimate_cost(CostKind.OBSERVED, rate, TokenUsage(input_tokens=3))
    estimates = CostEstimates(predicted, reserved, observed)
    assert (
        estimates.predicted.microusd,
        estimates.reserved.microusd,
        estimates.observed.microusd,
    ) == (1, 2, 3)


def test_rate_table_is_hash_bound_and_ordered() -> None:
    rate = ModelRate("model", 1, 2, 3, 4, 5)
    table = FrozenRateTable((rate,), datetime(2026, 8, 16, tzinfo=UTC), "manual")
    assert table.rate_for("model") == rate
    assert (
        table.rate_table_sha256
        != FrozenRateTable(
            (ModelRate("model", 2, 2, 3, 4, 5),), datetime(2026, 8, 16, tzinfo=UTC), "manual"
        ).rate_table_sha256
    )


def test_rate_table_detaches_mutable_caller_sequence() -> None:
    rates = [ModelRate("model", 1, 2, 3, 4, 5)]
    table = FrozenRateTable(rates, datetime(2026, 8, 16, tzinfo=UTC), "manual")  # type: ignore[arg-type]
    original_hash = table.rate_table_sha256
    rates.append(ModelRate("other", 1, 2, 3, 4, 5))
    assert table.rates == (ModelRate("model", 1, 2, 3, 4, 5),)
    assert table.rate_table_sha256 == original_hash
    assert table.rate_for("model") == rates[0]


@pytest.mark.parametrize("value", (True, 1.0))
def test_cost_contracts_reject_non_integer_numbers(value: object) -> None:
    with pytest.raises(ValueError):
        ModelRate("model", value, 0, 0, 0, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TokenUsage(input_tokens=value)  # type: ignore[arg-type]
