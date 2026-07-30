import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AblationName = Literal[
    "statement_only",
    "four_semantic_variants",
    "fts_only",
    "semantic_only",
    "fused",
    "fused_with_boosts",
]
ProfileKind = Literal["synthetic_regression", "copied_profile"]
GateStatus = Literal["pass", "fail", "pending"]

ABLATION_NAMES: tuple[AblationName, ...] = (
    "statement_only",
    "four_semantic_variants",
    "fts_only",
    "semantic_only",
    "fused",
    "fused_with_boosts",
)
REQUIRED_CATEGORIES = frozenset(
    {
        "easy_terminology",
        "paraphrase",
        "slide_only_wording",
        "transcript_only_wording",
        "multi_source",
        "genuine_gap",
        "hard_negative",
    }
)
SEMANTIC_COVERAGE_THRESHOLD = 0.995
_PRIMARY_QUERY_STRATEGY: AblationName = "fused_with_boosts"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvaluationProfile(_FrozenModel):
    kind: ProfileKind
    name: str = Field(min_length=1)
    label_provenance: str = Field(min_length=1)
    note_count: int = Field(gt=0)
    copied_via_supported_backup: bool
    production_profile_untouched: bool


class IndexObservation(_FrozenModel):
    model: str = Field(min_length=1)
    dimensions: int = Field(gt=0)
    search_engine: Literal["exact_numpy"]
    eligible_note_ids: tuple[int, ...]
    semantic_indexed_note_ids: tuple[int, ...]
    snapshot_size_bytes: int = Field(gt=0)
    full_refresh_ms: float = Field(gt=0)
    incremental_refresh_ms: float = Field(ge=0)
    peak_memory_bytes: int = Field(gt=0)

    @field_validator("eligible_note_ids", "semantic_indexed_note_ids")
    @classmethod
    def validate_note_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(note_id <= 0 for note_id in value):
            raise ValueError("index note IDs must be positive")
        if len(set(value)) != len(value):
            raise ValueError("index note IDs must be unique")
        return value


class GapProposalObservation(_FrozenModel):
    proposal_id: str = Field(min_length=1)
    evidence_ids: tuple[str, ...]

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("gap proposals require source evidence IDs")
        if len(set(value)) != len(value):
            raise ValueError("gap proposal evidence IDs must be unique")
        return value


class QueryObservation(_FrozenModel):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    relevant_note_ids: tuple[int, ...]
    eligible_note_ids: tuple[int, ...]
    rankings: dict[AblationName, tuple[int, ...]]
    latency_ms: dict[AblationName, float]
    ground_truth_covered: bool
    pass_1_predicted_covered: bool
    pass_2_expected_recoverable: bool
    pass_2_predicted_recovered: bool
    gap_proposal: GapProposalObservation | None = None

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        expected = set(ABLATION_NAMES)
        if set(self.rankings) != expected or set(self.latency_ms) != expected:
            raise ValueError(
                "each query must include all retrieval ablations"
            )
        if (
            any(note_id <= 0 for note_id in self.relevant_note_ids)
            or any(note_id <= 0 for note_id in self.eligible_note_ids)
            or len(set(self.relevant_note_ids))
            != len(self.relevant_note_ids)
            or len(set(self.eligible_note_ids))
            != len(self.eligible_note_ids)
        ):
            raise ValueError("query note IDs must be positive and unique")
        if not set(self.relevant_note_ids) <= set(self.eligible_note_ids):
            raise ValueError("relevant notes must be eligible")
        for ranking in self.rankings.values():
            if any(note_id <= 0 for note_id in ranking):
                raise ValueError("ranked note IDs must be positive")
            if len(set(ranking)) != len(ranking):
                raise ValueError("ranked note IDs must be unique")
        if any(
            not math.isfinite(latency) or latency < 0
            for latency in self.latency_ms.values()
        ):
            raise ValueError("query latencies must be finite and nonnegative")
        if self.ground_truth_covered != bool(self.relevant_note_ids):
            raise ValueError(
                "coverage labels must agree with relevant note IDs"
            )
        if self.pass_2_expected_recoverable and (
            not self.ground_truth_covered
            or self.pass_1_predicted_covered
        ):
            raise ValueError(
                "Pass 2 recovery must describe a covered Pass 1 miss"
            )
        return self


class ApplyAcceptanceObservation(_FrozenModel):
    protected_tag_mutation_count: int = Field(ge=0)
    duplicate_notes_after_retry: int = Field(ge=0)
    research_addons_unavailable: bool | None
    leading_sync_failure_no_writes: bool | None
    trailing_sync_failure_recorded: bool | None
    retry_completed_without_reapply: bool | None
    read_back_verified: bool | None


class EvaluationDataset(_FrozenModel):
    schema_version: Literal[1]
    dataset_version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    profile: EvaluationProfile
    index: IndexObservation
    resolvable_evidence_ids: tuple[str, ...]
    queries: tuple[QueryObservation, ...]
    acceptance: ApplyAcceptanceObservation

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        if not self.queries:
            raise ValueError("the evaluation set cannot be empty")
        if len({query.id for query in self.queries}) != len(self.queries):
            raise ValueError("evaluation query IDs must be unique")
        missing = REQUIRED_CATEGORIES - {
            query.category for query in self.queries
        }
        if missing:
            raise ValueError(
                "evaluation set is missing required categories: "
                + ", ".join(sorted(missing))
            )
        if (
            len(set(self.resolvable_evidence_ids))
            != len(self.resolvable_evidence_ids)
            or any(
                not evidence_id.strip()
                for evidence_id in self.resolvable_evidence_ids
            )
        ):
            raise ValueError(
                "resolvable source evidence IDs must be unique and nonempty"
            )
        index_eligible = set(self.index.eligible_note_ids)
        if self.profile.note_count != len(index_eligible):
            raise ValueError(
                "profile note count must equal the eligible index universe"
            )
        if any(
            not set(query.eligible_note_ids) <= index_eligible
            for query in self.queries
        ):
            raise ValueError(
                "query eligibility must be within the index universe"
            )
        return self


class RetrievalMetrics(_FrozenModel):
    evaluated_queries: int
    recall_at_5: float
    recall_at_10: float
    mrr: float
    ndcg_at_10: float
    query_p50_ms: float
    query_p95_ms: float


class Pass1Metrics(_FrozenModel):
    covered_true_positives: int
    covered_false_positives: int
    covered_false_negatives: int
    coverage_precision: float
    coverage_recall: float


class Pass2Metrics(_FrozenModel):
    expected_recoverable: int
    recovered: int
    false_recoveries: int
    recovery_rate: float
    false_recovery_rate: float


class GapProposalMetrics(_FrozenModel):
    proposed: int
    correct: int
    precision: float


class SemanticMetrics(_FrozenModel):
    eligible_notes: int
    indexed_eligible_notes: int
    unexpected_index_rows: int
    coverage: float


class Extrapolated68kMetrics(_FrozenModel):
    snapshot_size_bytes: int
    full_refresh_ms: float
    incremental_refresh_ms: float
    query_p50_ms: float
    query_p95_ms: float
    peak_memory_bytes: int


class TimingMetrics(_FrozenModel):
    snapshot_size_bytes: int
    full_refresh_ms: float
    incremental_refresh_ms: float
    query_p50_ms: float
    query_p95_ms: float
    peak_memory_bytes: int
    extrapolated_68k: Extrapolated68kMetrics


class GuardrailMetrics(_FrozenModel):
    eligible_filter_leaks: int
    unsupported_gap_proposals: int
    protected_tag_mutations: int
    duplicate_notes_after_retry: int


class GateCheck(_FrozenModel):
    name: str
    status: GateStatus
    detail: str


class GateGroup(_FrozenModel):
    status: GateStatus
    checks: tuple[GateCheck, ...]


class EvaluationGates(_FrozenModel):
    automated: GateGroup
    copied_profile: GateGroup
    release_ready: bool


class EvaluationReport(_FrozenModel):
    schema_version: Literal[1] = 1
    dataset_version: str
    dataset_sha256: str
    evaluated_at: datetime
    profile: EvaluationProfile
    retrieval: dict[AblationName, RetrievalMetrics]
    pass_1: Pass1Metrics
    pass_2: Pass2Metrics
    gap_proposals: GapProposalMetrics
    semantic: SemanticMetrics
    timing: TimingMetrics
    guardrails: GuardrailMetrics
    gates: EvaluationGates

    def to_markdown(self) -> str:
        lines = [
            f"# Anki retrieval evaluation — {self.dataset_version}",
            "",
            (
                f"Profile: `{self.profile.kind}` · "
                f"{self.profile.note_count:,} eligible notes · "
                f"dataset `{self.dataset_sha256[:12]}`"
            ),
            "",
            "| Ablation | Recall@5 | Recall@10 | MRR | nDCG@10 | p50 ms | p95 ms |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name in ABLATION_NAMES:
            metrics = self.retrieval[name]
            lines.append(
                f"| {name.replace('_', ' ')} "
                f"| {metrics.recall_at_5:.3f} "
                f"| {metrics.recall_at_10:.3f} "
                f"| {metrics.mrr:.3f} "
                f"| {metrics.ndcg_at_10:.3f} "
                f"| {metrics.query_p50_ms:.2f} "
                f"| {metrics.query_p95_ms:.2f} |"
            )
        lines.extend(
            [
                "",
                "| Pipeline metric | Result |",
                "|---|---:|",
                (
                    "| Pass 1 coverage precision / recall "
                    f"| {self.pass_1.coverage_precision:.3f} / "
                    f"{self.pass_1.coverage_recall:.3f} |"
                ),
                (
                    "| Pass 2 recovery / false recovery "
                    f"| {self.pass_2.recovery_rate:.3f} / "
                    f"{self.pass_2.false_recovery_rate:.3f} |"
                ),
                (
                    "| Gap proposal precision "
                    f"| {self.gap_proposals.precision:.3f} |"
                ),
                (
                    "| Semantic coverage "
                    f"| {self.semantic.coverage:.3%} |"
                ),
                (
                    "| Full / incremental refresh "
                    f"| {self.timing.full_refresh_ms:.1f} / "
                    f"{self.timing.incremental_refresh_ms:.1f} ms |"
                ),
                (
                    "| Exact query p50 / p95 "
                    f"| {self.timing.query_p50_ms:.2f} / "
                    f"{self.timing.query_p95_ms:.2f} ms |"
                ),
                "",
                "| Gate | Status |",
                "|---|---|",
                f"| Automated thresholds | {self.gates.automated.status} |",
                (
                    "| Copied-profile acceptance "
                    f"| {self.gates.copied_profile.status} |"
                ),
                (
                    "| Release ready "
                    f"| {'yes' if self.gates.release_ready else 'no'} |"
                ),
                "",
            ]
        )
        return "\n".join(lines)


def evaluate_dataset(dataset: EvaluationDataset) -> EvaluationReport:
    retrieval = {
        name: _retrieval_metrics(dataset.queries, name)
        for name in ABLATION_NAMES
    }
    pass_1 = _pass_1_metrics(dataset.queries)
    pass_2 = _pass_2_metrics(dataset.queries)
    gap_proposals = _gap_proposal_metrics(dataset.queries)
    semantic = _semantic_metrics(dataset.index)
    timing = _timing_metrics(dataset)
    guardrails = _guardrails(dataset)
    gates = _gates(dataset, semantic, guardrails)
    canonical = json.dumps(
        dataset.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return EvaluationReport(
        dataset_version=dataset.dataset_version,
        dataset_sha256=hashlib.sha256(canonical).hexdigest(),
        evaluated_at=datetime.now(UTC),
        profile=dataset.profile,
        retrieval=retrieval,
        pass_1=pass_1,
        pass_2=pass_2,
        gap_proposals=gap_proposals,
        semantic=semantic,
        timing=timing,
        guardrails=guardrails,
        gates=gates,
    )


def _retrieval_metrics(
    queries: tuple[QueryObservation, ...],
    name: AblationName,
) -> RetrievalMetrics:
    labeled = [query for query in queries if query.relevant_note_ids]
    recalls_at_5: list[float] = []
    recalls_at_10: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for query in labeled:
        ranking = query.rankings[name]
        relevant = set(query.relevant_note_ids)
        recalls_at_5.append(
            len(relevant.intersection(ranking[:5])) / len(relevant)
        )
        recalls_at_10.append(
            len(relevant.intersection(ranking[:10])) / len(relevant)
        )
        first_rank = next(
            (
                rank
                for rank, note_id in enumerate(ranking, start=1)
                if note_id in relevant
            ),
            None,
        )
        reciprocal_ranks.append(
            0.0 if first_rank is None else 1.0 / first_rank
        )
        ideal_count = min(len(relevant), 10)
        ideal_dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank in range(1, ideal_count + 1)
        )
        dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank, note_id in enumerate(ranking[:10], start=1)
            if note_id in relevant
        )
        ndcgs.append(dcg / ideal_dcg)
    latencies = [query.latency_ms[name] for query in queries]
    return RetrievalMetrics(
        evaluated_queries=len(labeled),
        recall_at_5=_mean(recalls_at_5),
        recall_at_10=_mean(recalls_at_10),
        mrr=_mean(reciprocal_ranks),
        ndcg_at_10=_mean(ndcgs),
        query_p50_ms=_percentile(latencies, 50),
        query_p95_ms=_percentile(latencies, 95),
    )


def _pass_1_metrics(
    queries: tuple[QueryObservation, ...],
) -> Pass1Metrics:
    true_positives = sum(
        query.ground_truth_covered
        and query.pass_1_predicted_covered
        for query in queries
    )
    false_positives = sum(
        not query.ground_truth_covered
        and query.pass_1_predicted_covered
        for query in queries
    )
    false_negatives = sum(
        query.ground_truth_covered
        and not query.pass_1_predicted_covered
        for query in queries
    )
    predicted_positive = true_positives + false_positives
    actual_positive = true_positives + false_negatives
    return Pass1Metrics(
        covered_true_positives=true_positives,
        covered_false_positives=false_positives,
        covered_false_negatives=false_negatives,
        coverage_precision=_classification_rate(
            true_positives,
            predicted_positive,
            has_expected=actual_positive > 0,
        ),
        coverage_recall=_classification_rate(
            true_positives,
            actual_positive,
            has_expected=actual_positive > 0,
        ),
    )


def _pass_2_metrics(
    queries: tuple[QueryObservation, ...],
) -> Pass2Metrics:
    recoverable = sum(
        query.pass_2_expected_recoverable for query in queries
    )
    recovered = sum(
        query.pass_2_expected_recoverable
        and query.pass_2_predicted_recovered
        for query in queries
    )
    nonrecoverable = sum(
        not query.pass_2_expected_recoverable for query in queries
    )
    false_recoveries = sum(
        not query.pass_2_expected_recoverable
        and query.pass_2_predicted_recovered
        for query in queries
    )
    return Pass2Metrics(
        expected_recoverable=recoverable,
        recovered=recovered,
        false_recoveries=false_recoveries,
        recovery_rate=(
            recovered / recoverable if recoverable else 1.0
        ),
        false_recovery_rate=(
            false_recoveries / nonrecoverable
            if nonrecoverable
            else 0.0
        ),
    )


def _gap_proposal_metrics(
    queries: tuple[QueryObservation, ...],
) -> GapProposalMetrics:
    proposed = [query for query in queries if query.gap_proposal is not None]
    correct = sum(not query.ground_truth_covered for query in proposed)
    return GapProposalMetrics(
        proposed=len(proposed),
        correct=correct,
        precision=correct / len(proposed) if proposed else 1.0,
    )


def _semantic_metrics(index: IndexObservation) -> SemanticMetrics:
    eligible = set(index.eligible_note_ids)
    indexed = set(index.semantic_indexed_note_ids)
    indexed_eligible = eligible & indexed
    return SemanticMetrics(
        eligible_notes=len(eligible),
        indexed_eligible_notes=len(indexed_eligible),
        unexpected_index_rows=len(indexed - eligible),
        coverage=(
            len(indexed_eligible) / len(eligible) if eligible else 1.0
        ),
    )


def _timing_metrics(dataset: EvaluationDataset) -> TimingMetrics:
    primary_latencies = [
        query.latency_ms[_PRIMARY_QUERY_STRATEGY]
        for query in dataset.queries
    ]
    p50 = _percentile(primary_latencies, 50)
    p95 = _percentile(primary_latencies, 95)
    scale = 68_000 / dataset.profile.note_count
    index = dataset.index
    return TimingMetrics(
        snapshot_size_bytes=index.snapshot_size_bytes,
        full_refresh_ms=index.full_refresh_ms,
        incremental_refresh_ms=index.incremental_refresh_ms,
        query_p50_ms=p50,
        query_p95_ms=p95,
        peak_memory_bytes=index.peak_memory_bytes,
        extrapolated_68k=Extrapolated68kMetrics(
            snapshot_size_bytes=round(index.snapshot_size_bytes * scale),
            full_refresh_ms=index.full_refresh_ms * scale,
            incremental_refresh_ms=index.incremental_refresh_ms * scale,
            query_p50_ms=p50 * scale,
            query_p95_ms=p95 * scale,
            peak_memory_bytes=round(index.peak_memory_bytes * scale),
        ),
    )


def _guardrails(dataset: EvaluationDataset) -> GuardrailMetrics:
    leaks = sum(
        note_id not in set(query.eligible_note_ids)
        for query in dataset.queries
        for ranking in query.rankings.values()
        for note_id in ranking
    )
    resolvable = set(dataset.resolvable_evidence_ids)
    unsupported = sum(
        proposal is not None
        and (
            not proposal.evidence_ids
            or not set(proposal.evidence_ids) <= resolvable
        )
        for proposal in (
            query.gap_proposal for query in dataset.queries
        )
    )
    return GuardrailMetrics(
        eligible_filter_leaks=leaks,
        unsupported_gap_proposals=unsupported,
        protected_tag_mutations=(
            dataset.acceptance.protected_tag_mutation_count
        ),
        duplicate_notes_after_retry=(
            dataset.acceptance.duplicate_notes_after_retry
        ),
    )


def _gates(
    dataset: EvaluationDataset,
    semantic: SemanticMetrics,
    guardrails: GuardrailMetrics,
) -> EvaluationGates:
    automated_checks = (
        _check(
            "semantic_coverage",
            semantic.coverage >= SEMANTIC_COVERAGE_THRESHOLD,
            (
                f"{semantic.coverage:.3%} observed; "
                f"{SEMANTIC_COVERAGE_THRESHOLD:.3%} required"
            ),
        ),
        _check(
            "eligible_filter_leaks",
            guardrails.eligible_filter_leaks == 0,
            f"{guardrails.eligible_filter_leaks} leaked ranked rows",
        ),
        _check(
            "source_evidence",
            guardrails.unsupported_gap_proposals == 0,
            (
                f"{guardrails.unsupported_gap_proposals} proposals lack "
                "resolvable evidence"
            ),
        ),
        _check(
            "protected_tags",
            guardrails.protected_tag_mutations == 0,
            (
                f"{guardrails.protected_tag_mutations} protected-tag "
                "mutations"
            ),
        ),
        _check(
            "duplicate_retry",
            guardrails.duplicate_notes_after_retry == 0,
            (
                f"{guardrails.duplicate_notes_after_retry} duplicate notes "
                "after retry"
            ),
        ),
    )
    automated = GateGroup(
        status=_aggregate_status(automated_checks),
        checks=automated_checks,
    )

    copied_checks: tuple[GateCheck, ...]
    if dataset.profile.kind != "copied_profile":
        copied_checks = (
            GateCheck(
                name="copied_profile",
                status="pending",
                detail=(
                    "run against a supported backup/export copy on the NUC"
                ),
            ),
        )
        copied = GateGroup(status="pending", checks=copied_checks)
    else:
        acceptance = dataset.acceptance
        copied_checks = (
            _check(
                "manual_labels",
                dataset.profile.label_provenance
                == "manual_copied_profile",
                dataset.profile.label_provenance,
            ),
            _check(
                "supported_profile_copy",
                dataset.profile.copied_via_supported_backup,
                "copy must come from Anki backup/export with Anki closed",
            ),
            _check(
                "production_profile_untouched",
                dataset.profile.production_profile_untouched,
                "production profile must remain untouched",
            ),
            _optional_check(
                "research_addons_unavailable",
                acceptance.research_addons_unavailable,
            ),
            _optional_check(
                "leading_sync_failure_no_writes",
                acceptance.leading_sync_failure_no_writes,
            ),
            _optional_check(
                "trailing_sync_failure_recorded",
                acceptance.trailing_sync_failure_recorded,
            ),
            _optional_check(
                "retry_completed_without_reapply",
                acceptance.retry_completed_without_reapply,
            ),
            _optional_check(
                "read_back_verified",
                acceptance.read_back_verified,
            ),
        )
        copied = GateGroup(
            status=_aggregate_status(copied_checks),
            checks=copied_checks,
        )
    return EvaluationGates(
        automated=automated,
        copied_profile=copied,
        release_ready=(
            automated.status == "pass" and copied.status == "pass"
        ),
    )


def _check(name: str, passed: bool, detail: str) -> GateCheck:
    return GateCheck(
        name=name,
        status="pass" if passed else "fail",
        detail=detail,
    )


def _optional_check(name: str, value: bool | None) -> GateCheck:
    if value is None:
        return GateCheck(
            name=name,
            status="pending",
            detail="scenario not recorded",
        )
    return GateCheck(
        name=name,
        status="pass" if value else "fail",
        detail="scenario passed" if value else "scenario failed",
    )


def _aggregate_status(checks: tuple[GateCheck, ...]) -> GateStatus:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "pending" for check in checks):
        return "pending"
    return "pass"


def _classification_rate(
    numerator: int,
    denominator: int,
    *,
    has_expected: bool,
) -> float:
    if denominator:
        return numerator / denominator
    return 0.0 if has_expected else 1.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (
        ordered[upper] - ordered[lower]
    ) * fraction
