"""Deterministic integer-only v3 cost math; no pricing lookup or dispatch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, cast

from oms_hub.anki.course_policy import CourseCurationPolicy

_MILLION = 1_000_000
RESERVED_INPUT_SAFETY_MULTIPLIER = 2


class CostKind(StrEnum):
    PREDICTED = "predicted"
    RESERVED = "reserved"
    OBSERVED = "observed"


ESTIMATOR_VERSION = "cost-estimator-v1"


class CostAuthorizationError(ValueError):
    """A v3 dispatch lacks an exact, frozen cost authorization."""


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

    def document(self) -> dict[str, object]:
        """The complete, caller-supplied R0 rate table; never look up prices."""
        return {
            "effective_at": self.effective_at.isoformat(),
            "source": self.source,
            "currency": self.currency,
            "rates": [
                {
                    "model": rate.model,
                    "input_microusd_per_million_tokens": (rate.input_microusd_per_million_tokens),
                    "cache_creation_microusd_per_million_tokens": (
                        rate.cache_creation_microusd_per_million_tokens
                    ),
                    "cache_read_microusd_per_million_tokens": (
                        rate.cache_read_microusd_per_million_tokens
                    ),
                    "output_microusd_per_million_tokens": (rate.output_microusd_per_million_tokens),
                    "embedding_microusd_per_million_tokens": (
                        rate.embedding_microusd_per_million_tokens
                    ),
                }
                for rate in self.rates
            ],
            "rate_table_sha256": self.rate_table_sha256,
        }

    @classmethod
    def from_document(cls, value: Mapping[str, object]) -> FrozenRateTable:
        required = {"effective_at", "source", "currency", "rates", "rate_table_sha256"}
        if set(value) != required or not isinstance(value.get("rates"), list):
            raise CostAuthorizationError("R0 requires a complete canonical frozen rate table")
        rates = cast(list[Mapping[str, object]], value["rates"])
        if not all(isinstance(item, Mapping) for item in rates):
            raise CostAuthorizationError("R0 frozen rate table is malformed")
        try:
            table = cls(
                rates=tuple(ModelRate(**cast(dict[str, Any], dict(item))) for item in rates),
                effective_at=datetime.fromisoformat(str(value["effective_at"])),
                source=str(value["source"]),
                currency=str(value["currency"]),
                rate_table_sha256=str(value["rate_table_sha256"]),
            )
        except (TypeError, ValueError) as exc:
            raise CostAuthorizationError("R0 frozen rate table is malformed") from exc
        if table.document() != dict(value):
            raise CostAuthorizationError("R0 frozen rate table is not canonical")
        return table


@dataclass(frozen=True, slots=True)
class HigherOrdinaryAuthorization:
    job_id: str
    policy_sha256: str
    rate_table_sha256: str
    estimator_version: str
    authorized_limit_microusd: int
    authorization_sha256: str

    def __post_init__(self) -> None:
        document = {
            "job_id": self.job_id,
            "policy_sha256": self.policy_sha256,
            "rate_table_sha256": self.rate_table_sha256,
            "estimator_version": self.estimator_version,
            "authorized_limit_microusd": self.authorized_limit_microusd,
        }
        expected = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            not self.job_id
            or self.estimator_version != ESTIMATOR_VERSION
            or self.authorized_limit_microusd < 0
            or self.authorization_sha256 != expected
        ):
            raise CostAuthorizationError("higher ordinary authorization is forged or stale")


class CostEstimator:
    """Frozen-table integer estimator and cumulative pre-dispatch gate."""

    version = ESTIMATOR_VERSION

    def __init__(self, rate_table: FrozenRateTable) -> None:
        self.rate_table = rate_table

    def estimate(self, kind: CostKind, *, model: str, usage: TokenUsage) -> CostEstimate:
        try:
            rate = self.rate_table.rate_for(model)
        except KeyError as exc:
            raise CostAuthorizationError(f"unknown frozen rate for model {model!r}") from exc
        return estimate_cost(kind, rate, usage)

    def authorize_dispatch(
        self,
        *,
        job_id: str,
        policy_sha256: str,
        ordinary_limit_microusd: int,
        hard_limit_microusd: int,
        current: CostLedgerEntry,
        prior: tuple[CostLedgerEntry, ...] = (),
        higher_authorization: HigherOrdinaryAuthorization | None = None,
    ) -> None:
        if not job_id or len(policy_sha256) != 64:
            raise CostAuthorizationError("job and policy identity are required before dispatch")
        if ordinary_limit_microusd < 0 or hard_limit_microusd < 0:
            raise CostAuthorizationError("cost limits cannot be negative")
        if higher_authorization is not None:
            if (
                higher_authorization.job_id != job_id
                or higher_authorization.policy_sha256 != policy_sha256
                or higher_authorization.rate_table_sha256 != self.rate_table.rate_table_sha256
            ):
                raise CostAuthorizationError("higher ordinary authorization identity changed")
            ordinary_limit_microusd = higher_authorization.authorized_limit_microusd
        predicted = sum(item.predicted.microusd for item in prior) + current.predicted.microusd
        reserved = sum(item.reserved.microusd for item in prior) + current.reserved.microusd
        if predicted > ordinary_limit_microusd:
            raise CostAuthorizationError("predicted ordinary cost exceeds authorization")
        if reserved > hard_limit_microusd:
            raise CostAuthorizationError("reserved exposure exceeds hard ceiling")


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


@dataclass(frozen=True, slots=True)
class CostLedgerEntry:
    call_id: str
    stage: str
    modality: str
    model: str
    request_sha256: str
    rate_table_sha256: str
    estimator_version: str
    predicted: CostEstimate
    reserved: CostEstimate
    observed: CostEstimate | None = None
    observed_estimated: bool = False

    def document(self) -> dict[str, object]:
        def estimate(value: CostEstimate | None) -> dict[str, object] | None:
            return (
                None
                if value is None
                else {
                    "kind": value.kind.value,
                    "microusd": value.microusd,
                    "usage": {
                        "input_tokens": value.usage.input_tokens,
                        "cache_creation_tokens": value.usage.cache_creation_tokens,
                        "cache_read_tokens": value.usage.cache_read_tokens,
                        "output_tokens": value.usage.output_tokens,
                        "embedding_tokens": value.usage.embedding_tokens,
                    },
                }
            )

        return {
            "call_id": self.call_id,
            "stage": self.stage,
            "modality": self.modality,
            "model": self.model,
            "request_sha256": self.request_sha256,
            "rate_table_sha256": self.rate_table_sha256,
            "estimator_version": self.estimator_version,
            "predicted": estimate(self.predicted),
            "reserved": estimate(self.reserved),
            "observed": estimate(self.observed),
            "observed_estimated": self.observed_estimated,
        }

    @classmethod
    def from_document(
        cls, value: Mapping[str, object], *, rate_table: FrozenRateTable | None = None
    ) -> CostLedgerEntry:
        def parse_estimate(raw: object, kind: CostKind) -> CostEstimate | None:
            if raw is None:
                return None
            if not isinstance(raw, Mapping) or raw.get("kind") != kind.value:
                raise CostAuthorizationError("cost ledger estimate is malformed")
            usage = raw.get("usage")
            if not isinstance(usage, Mapping):
                raise CostAuthorizationError("cost ledger usage is malformed")
            return CostEstimate(kind, int(raw["microusd"]), TokenUsage(**usage))

        try:
            entry = cls(
                call_id=str(value["call_id"]),
                stage=str(value["stage"]),
                modality=str(value["modality"]),
                model=str(value["model"]),
                request_sha256=str(value["request_sha256"]),
                rate_table_sha256=str(value["rate_table_sha256"]),
                estimator_version=str(value["estimator_version"]),
                predicted=parse_estimate(value.get("predicted"), CostKind.PREDICTED),  # type: ignore[arg-type]
                reserved=parse_estimate(value.get("reserved"), CostKind.RESERVED),  # type: ignore[arg-type]
                observed=parse_estimate(value.get("observed"), CostKind.OBSERVED),
                observed_estimated=bool(value.get("observed_estimated", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CostAuthorizationError("cost ledger entry is malformed") from exc
        if (
            not entry.call_id
            or not entry.stage
            or entry.modality not in {"structured", "embedding"}
            or len(entry.request_sha256) != 64
            or len(entry.rate_table_sha256) != 64
            or entry.estimator_version != ESTIMATOR_VERSION
            or entry.document() != dict(value)
        ):
            raise CostAuthorizationError("cost ledger entry is not canonical")
        if entry.predicted is None or entry.reserved is None:
            raise CostAuthorizationError("cost ledger requires predicted and reserved estimates")
        if rate_table is not None:
            try:
                rate = rate_table.rate_for(entry.model)
            except KeyError as exc:
                raise CostAuthorizationError(
                    "cost ledger model is absent from frozen table"
                ) from exc
            for estimate in (entry.predicted, entry.reserved, entry.observed):
                if (
                    estimate is not None
                    and estimate.microusd
                    != estimate_cost(estimate.kind, rate, estimate.usage).microusd
                ):
                    raise CostAuthorizationError("cost ledger price does not match frozen usage")
        return entry


class StageCostSession:
    """Exact-prefix cumulative ledger used by each v3 stage seam."""

    def __init__(
        self,
        estimator: CostEstimator,
        *,
        job_id: str,
        policy_sha256: str,
        ordinary_limit_microusd: int,
        hard_limit_microusd: int,
        entries: Sequence[CostLedgerEntry] = (),
        replay_reusable_call_ids: Sequence[str] = (),
        higher_authorization: HigherOrdinaryAuthorization | None = None,
    ) -> None:
        self.estimator, self.job_id, self.policy_sha256 = estimator, job_id, policy_sha256
        self.ordinary_limit_microusd, self.hard_limit_microusd = (
            ordinary_limit_microusd,
            hard_limit_microusd,
        )
        self.entries = list(entries)
        self.replay_reusable_call_ids = set(replay_reusable_call_ids)
        self.higher_authorization = higher_authorization

    @classmethod
    def from_prior(cls, context: Any, r0: Mapping[str, object]) -> StageCostSession:
        raw_table = r0.get("rate_table")
        if not isinstance(raw_table, Mapping):
            raise CostAuthorizationError("R0 requires a complete canonical frozen rate table")
        table = FrozenRateTable.from_document(raw_table)
        if r0.get("rate_table_sha256") != table.rate_table_sha256:
            raise CostAuthorizationError("R0 rate-table hash mismatch")
        empty_ledger: list[object] = []
        if (
            r0.get("cost_ledger") != empty_ledger
            or r0.get("cost_ledger_sha256")
            != hashlib.sha256(json.dumps(empty_ledger, separators=(",", ":")).encode()).hexdigest()
        ):
            raise CostAuthorizationError("R0 cost ledger must be canonical and empty")
        prior: list[CostLedgerEntry] = []
        for stage, payload in getattr(context, "prior_payloads", {}).items():
            if payload is r0:
                continue
            if not isinstance(payload, Mapping):
                raise CostAuthorizationError("prior stage payload is malformed")
            stage_name = getattr(stage, "value", str(stage))
            if (
                stage_name.startswith("v3_r")
                and stage_name
                not in {
                    "v3_r1_source_index",
                    "v3_r2_fidelity",
                    "v3_r4_index_verification",
                }
                and "cost_ledger" not in payload
            ):
                raise CostAuthorizationError("prior v3 cost ledger is missing")
            if "cost_ledger" not in payload:
                continue
            raw = payload["cost_ledger"]
            if not isinstance(raw, list):
                raise CostAuthorizationError("prior cost ledger is malformed")
            parsed = [
                CostLedgerEntry.from_document(item, rate_table=table)
                for item in raw
                if isinstance(item, Mapping)
            ]
            if len(parsed) != len(raw):
                raise CostAuthorizationError("prior cost ledger is malformed")
            if (
                payload.get("cost_ledger_sha256")
                != hashlib.sha256(
                    json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ):
                raise CostAuthorizationError("prior cost ledger hash changed")
            if any(
                entry.rate_table_sha256 != table.rate_table_sha256
                or entry.estimator_version != ESTIMATOR_VERSION
                for entry in parsed
            ):
                raise CostAuthorizationError("prior cost ledger table changed")
            if prior and parsed[: len(prior)] != prior:
                raise CostAuthorizationError("prior cost ledger prefix changed")
            if len(parsed) >= len(prior):
                prior = parsed
        durable_ids: list[str] = []
        for raw in getattr(context, "durable_cost_entries", ()):
            if not isinstance(raw, Mapping):
                raise CostAuthorizationError("durable cost entry is malformed")
            entry = CostLedgerEntry.from_document(raw, rate_table=table)
            existing = next((item for item in prior if item.call_id == entry.call_id), None)
            if existing is not None:
                if existing != entry:
                    raise CostAuthorizationError(
                        "durable cost entry conflicts with artifact ledger"
                    )
                continue
            prior.append(entry)
            durable_ids.append(entry.call_id)
        job = context.job
        try:
            policy = CourseCurationPolicy.model_validate(r0.get("policy"))
        except (TypeError, ValueError) as exc:
            raise CostAuthorizationError("R0 policy is malformed") from exc
        if r0.get("policy_sha256") != policy.policy_sha256 or policy.policy_sha256 != getattr(
            job, "policy_sha256", None
        ):
            raise CostAuthorizationError("R0 policy identity changed")
        ordinary = r0.get("ordinary_cost_limit_microusd")
        hard = r0.get("hard_stop_cost_limit_microusd")
        if type(ordinary) is not int or type(hard) is not int:
            raise CostAuthorizationError("R0 cost limits are malformed")
        if (
            ordinary != policy.ordinary_cost_limit_microusd
            or hard != policy.hard_stop_cost_limit_microusd
        ):
            raise CostAuthorizationError("R0 cost limits drift from the frozen policy")
        raw_authorization = r0.get("higher_ordinary_authorization")
        if raw_authorization is not None and not isinstance(raw_authorization, Mapping):
            raise CostAuthorizationError("higher ordinary authorization is malformed")
        try:
            higher_authorization = (
                None
                if raw_authorization is None
                else HigherOrdinaryAuthorization(**cast(dict[str, Any], dict(raw_authorization)))
            )
        except (TypeError, ValueError) as exc:
            raise CostAuthorizationError("higher ordinary authorization is malformed") from exc
        if (
            higher_authorization is not None
            and higher_authorization.authorized_limit_microusd > hard
        ):
            raise CostAuthorizationError("higher ordinary authorization exceeds hard ceiling")
        return cls(
            CostEstimator(table),
            job_id=str(job.id),
            policy_sha256=str(job.policy_sha256),
            ordinary_limit_microusd=ordinary,
            hard_limit_microusd=hard,
            entries=prior,
            replay_reusable_call_ids=durable_ids,
            higher_authorization=higher_authorization,
        )

    def reserve(
        self,
        *,
        stage: str,
        modality: str,
        model: str,
        request_sha256: str,
        predicted_usage: TokenUsage,
        reserved_usage: TokenUsage,
    ) -> CostLedgerEntry:
        reusable = [
            item
            for item in self.entries
            if item.call_id in self.replay_reusable_call_ids
            and item.stage == stage
            and item.modality == modality
            and item.model == model
            and item.request_sha256 == request_sha256
        ]
        if len(reusable) > 1:
            raise CostAuthorizationError("durable replay reservation is ambiguous")
        if reusable:
            existing = reusable[0]
            expected_predicted = self.estimator.estimate(
                CostKind.PREDICTED, model=model, usage=predicted_usage
            )
            expected_reserved = self.estimator.estimate(
                CostKind.RESERVED, model=model, usage=reserved_usage
            )
            if existing.predicted != expected_predicted or existing.reserved != expected_reserved:
                raise CostAuthorizationError("durable replay reservation changed")
            self.replay_reusable_call_ids.remove(existing.call_id)
            return existing
        call_id = hashlib.sha256(
            f"{stage}\0{modality}\0{model}\0{request_sha256}\0{len(self.entries)}".encode()
        ).hexdigest()
        entry = CostLedgerEntry(
            call_id=call_id,
            stage=stage,
            modality=modality,
            model=model,
            request_sha256=request_sha256,
            rate_table_sha256=self.estimator.rate_table.rate_table_sha256,
            estimator_version=ESTIMATOR_VERSION,
            predicted=self.estimator.estimate(
                CostKind.PREDICTED, model=model, usage=predicted_usage
            ),
            reserved=self.estimator.estimate(CostKind.RESERVED, model=model, usage=reserved_usage),
        )
        self.estimator.authorize_dispatch(
            job_id=self.job_id,
            policy_sha256=self.policy_sha256,
            ordinary_limit_microusd=self.ordinary_limit_microusd,
            hard_limit_microusd=self.hard_limit_microusd,
            current=entry,
            prior=tuple(self.entries),
            higher_authorization=self.higher_authorization,
        )
        self.entries.append(entry)
        return entry

    def observe(self, call_id: str, usage: TokenUsage, *, estimated: bool = False) -> None:
        for index, item in enumerate(self.entries):
            if item.call_id != call_id:
                continue
            observed = self.estimator.estimate(CostKind.OBSERVED, model=item.model, usage=usage)
            if item.observed is not None:
                if item.observed != observed or item.observed_estimated != estimated:
                    raise CostAuthorizationError("cost observation changed after durable recovery")
                return
            updated = replace(item, observed=observed, observed_estimated=estimated)
            self.entries[index] = updated
            if sum(value.reserved.microusd for value in self.entries) > self.hard_limit_microusd:
                raise CostAuthorizationError("reserved exposure exceeds hard ceiling")
            if (
                sum(value.observed.microusd for value in self.entries if value.observed is not None)
                > self.hard_limit_microusd
            ):
                raise CostAuthorizationError("observed cost exceeds hard ceiling")
            return
        raise CostAuthorizationError("cost observation does not match a reservation")

    def seal(self, payload: dict[str, object]) -> dict[str, object]:
        ledger = [item.document() for item in self.entries]
        payload["cost_ledger"] = ledger
        payload["cost_ledger_sha256"] = hashlib.sha256(
            json.dumps(ledger, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return payload


def _ceil_cost(tokens: int, microusd_per_million_tokens: int) -> int:
    return (tokens * microusd_per_million_tokens + _MILLION - 1) // _MILLION


def estimate_cost(kind: CostKind, rate: ModelRate, usage: TokenUsage) -> CostEstimate:
    cache_reserve_rate = max(
        rate.input_microusd_per_million_tokens,
        rate.cache_creation_microusd_per_million_tokens,
        rate.cache_read_microusd_per_million_tokens,
    )
    microusd = sum(
        _ceil_cost(tokens, rate_value)
        for tokens, rate_value in (
            (
                usage.input_tokens,
                cache_reserve_rate
                if kind is CostKind.RESERVED
                else rate.input_microusd_per_million_tokens,
            ),
            (
                usage.cache_creation_tokens,
                cache_reserve_rate
                if kind is CostKind.RESERVED
                else rate.cache_creation_microusd_per_million_tokens,
            ),
            (
                usage.cache_read_tokens,
                cache_reserve_rate
                if kind is CostKind.RESERVED
                else rate.cache_read_microusd_per_million_tokens,
            ),
            (usage.output_tokens, rate.output_microusd_per_million_tokens),
            (usage.embedding_tokens, rate.embedding_microusd_per_million_tokens),
        )
    )
    return CostEstimate(kind=kind, microusd=microusd, usage=usage)
