import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from oms_hub.anki.judgment import CoverageJudgment
from oms_hub.anki.lcl import LectureConcept
from oms_hub.anki.source_index import SourceScope, SourceSearchHit
from oms_hub.anki.sources import SourcePassage
from oms_hub.llm.domain import ProviderName
from oms_hub.llm.structured import StructuredJSONResult

RescueSupport = Literal["supported", "partial", "unsupported"]
RescueOutcome = Literal[
    "recovered",
    "gap_supported",
    "unresolved_partial",
    "unsupported",
]
RescueQueryKind = Literal[
    "source_statement",
    "terminology",
    "clinical_rephrase",
]

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a",
    "after",
    "and",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "the",
    "to",
    "when",
    "with",
}


class LocalizationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    support: RescueSupport
    evidence_ids: tuple[
        str,
        ...,
    ]
    rationale: str = Field(min_length=1, max_length=4_000)


class RescueQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=4_000)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    kind: RescueQueryKind


@dataclass(frozen=True, slots=True)
class RescueLocalization:
    concept: LectureConcept
    support: RescueSupport
    evidence: tuple[SourcePassage, ...]
    rationale: str


class SourceRevisionStale(ValueError):
    """Source search returned evidence outside the selected revisions."""


class SourceEvidenceIndex(Protocol):
    async def search(
        self,
        query: str,
        source_scope: SourceScope,
        *,
        limit: int,
    ) -> list[SourceSearchHit]: ...


class StructuredLocalizationService(Protocol):
    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[LocalizationDecision],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[LocalizationDecision]: ...


class RescueService:
    def __init__(
        self,
        source_index: SourceEvidenceIndex,
        structured: StructuredLocalizationService,
        *,
        provider: ProviderName,
        model: str,
        prompt_version: str,
        candidate_limit: int = 12,
    ) -> None:
        if (
            not model.strip()
            or not prompt_version.strip()
            or candidate_limit < 1
        ):
            raise ValueError("rescue configuration is invalid")
        self.source_index = source_index
        self.structured = structured
        self.provider = provider
        self.model = model
        self.prompt_version = prompt_version
        self.candidate_limit = candidate_limit

    async def localize(
        self,
        concept: LectureConcept,
        source_scope: SourceScope,
    ) -> RescueLocalization:
        by_passage: dict[str, SourceSearchHit] = {}
        for query in concept.queries:
            hits = await self.source_index.search(
                query,
                source_scope,
                limit=self.candidate_limit,
            )
            for hit in hits:
                current = by_passage.get(hit.passage.passage_id)
                if current is None or hit.score > current.score:
                    by_passage[hit.passage.passage_id] = hit
        if not by_passage:
            return RescueLocalization(
                concept=concept,
                support="unsupported",
                evidence=(),
                rationale="No selected source passage matched the concept.",
            )
        hits = sorted(
            by_passage.values(),
            key=lambda hit: (-hit.score, hit.passage.passage_id),
        )[: self.candidate_limit]
        selected_revisions = set(source_scope.revision_ids)
        if selected_revisions and any(
            hit.passage.revision_id not in selected_revisions
            for hit in hits
        ):
            raise SourceRevisionStale(
                "source search returned a stale revision"
            )
        selected_kinds = set(source_scope.source_kinds)
        if selected_kinds and any(
            hit.passage.source_kind not in selected_kinds for hit in hits
        ):
            raise SourceRevisionStale(
                "source search returned evidence outside source scope"
            )
        generated = self.structured.generate_json(
            _localization_instruction(self.prompt_version),
            _localization_input(concept, hits),
            output_model=LocalizationDecision,
            provider=self.provider,
            model=self.model,
        )
        evidence_by_id = {
            hit.passage.passage_id: hit.passage for hit in hits
        }
        decision = generated.value
        if len(set(decision.evidence_ids)) != len(decision.evidence_ids):
            raise ValueError("rescue evidence IDs must be unique")
        if any(
            evidence_id not in evidence_by_id
            for evidence_id in decision.evidence_ids
        ):
            raise ValueError(
                "rescue evidence ID was not a localized source passage"
            )
        if decision.support == "unsupported":
            if decision.evidence_ids:
                raise ValueError(
                    "unsupported localization cannot cite evidence"
                )
        elif not decision.evidence_ids:
            raise ValueError(
                "supported localization requires source evidence"
            )
        return RescueLocalization(
            concept=concept,
            support=decision.support,
            evidence=tuple(
                evidence_by_id[evidence_id]
                for evidence_id in decision.evidence_ids
            ),
            rationale=decision.rationale.strip(),
        )

    def build_queries(
        self,
        localization: RescueLocalization,
    ) -> tuple[RescueQuery, RescueQuery, RescueQuery]:
        if (
            localization.support == "unsupported"
            or not localization.evidence
        ):
            raise ValueError(
                "rescue queries require localized source evidence"
            )
        evidence_ids = tuple(
            passage.passage_id for passage in localization.evidence
        )
        source_statement = " ".join(
            passage.text for passage in localization.evidence
        )
        terminology = " ".join(
            _distinct_terms(source_statement)[:20]
        )
        if not terminology:
            raise ValueError("source evidence has no searchable terms")
        return (
            RescueQuery(
                text=source_statement,
                evidence_ids=evidence_ids,
                kind="source_statement",
            ),
            RescueQuery(
                text=terminology,
                evidence_ids=evidence_ids,
                kind="terminology",
            ),
            RescueQuery(
                text=localization.concept.statement,
                evidence_ids=evidence_ids,
                kind="clinical_rephrase",
            ),
        )

    @staticmethod
    def finalize(
        localization: RescueLocalization,
        pass_2_judgment: CoverageJudgment | None,
    ) -> RescueOutcome:
        if localization.support == "unsupported":
            return "unsupported"
        if pass_2_judgment is not None and (
            pass_2_judgment.status == "covered"
        ):
            return "recovered"
        if (
            localization.support == "supported"
            and pass_2_judgment is not None
            and pass_2_judgment.status == "missing"
        ):
            return "gap_supported"
        return "unresolved_partial"


def _localization_instruction(prompt_version: str) -> str:
    return (
        f"Apply source rescue rubric {prompt_version}. Determine whether the "
        "candidate slide and transcript passages explicitly support, "
        "partially support, or do not support the lecture concept. Cite only "
        "candidate passage IDs. Do not create card text or add medical facts."
    )


def _localization_input(
    concept: LectureConcept,
    hits: Sequence[SourceSearchHit],
) -> str:
    return json.dumps(
        {
            "concept": concept.model_dump(mode="json"),
            "passages": [
                {
                    "passage_id": hit.passage.passage_id,
                    "revision_id": hit.passage.revision_id,
                    "source_kind": hit.passage.source_kind.value,
                    "locator": hit.passage.locator,
                    "citation": hit.passage.citation,
                    "text": hit.passage.text,
                }
                for hit in hits
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _distinct_terms(value: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for term in _TOKEN.findall(value.casefold()):
        if term in _STOPWORDS or len(term) < 2 or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms
