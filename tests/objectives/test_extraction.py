from dataclasses import asdict, replace

import pytest

from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevision,
    SourceRevisionState,
)
from oms_hub.objectives.extraction import (
    OBJECTIVE_EXTRACTION_PROMPT_VERSION,
    ConsolidationCandidate,
    ObjectiveEvidenceInput,
    ObjectiveExtractor,
    ProposedObjective,
    SuggestedObjectiveLink,
)
from oms_hub.objectives.models import ObjectiveEdgeType
from oms_hub.providers.contracts import AuthorityClass


def _revision(state: SourceRevisionState = SourceRevisionState.READY) -> SourceRevision:
    return SourceRevision("source-1", "revision-1", "a" * 64, state)


def _evidence(
    evidence_id: str = "evidence-1",
    *,
    authority: AuthorityClass = AuthorityClass.COURSE_MATERIAL,
    course_id: str = "heme",
    exam_id: str | None = "exam-2",
    lecture_id: str | None = "lecture-13",
) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        source_revision_id="revision-1",
        authority_class=authority,
        course_id=course_id,
        exam_id=exam_id,
        lecture_id=lecture_id,
        locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, "12"),
        normalized_text="Heparin exposure can precede thrombocytopenia.",
        content_sha256="b" * 64,
        created_at="2026-08-26T12:00:00+00:00",
    )


class Knowledge:
    def __init__(
        self,
        revision: SourceRevision | None = None,
        evidence: tuple[EvidenceUnit, ...] | None = None,
    ) -> None:
        self.revision = revision or _revision()
        self.evidence = evidence or (_evidence(),)
        self.requested_revisions: list[str] = []

    def get_revision(self, revision_id: str) -> SourceRevision | None:
        self.requested_revisions.append(revision_id)
        return self.revision if revision_id == self.revision.source_revision_id else None

    def list_evidence(self, revision_id: str) -> tuple[EvidenceUnit, ...]:
        return self.evidence if revision_id == self.revision.source_revision_id else ()


def _proposal(**overrides: object) -> ProposedObjective:
    values: dict[str, object] = {
        "observable_verb": "differentiate",
        "concept": "heparin-induced thrombocytopenia",
        "description": "Differentiate HIT from other causes of thrombocytopenia.",
        "course_id": "heme",
        "exam_id": "exam-2",
        "lecture_ids": ("lecture-13",),
        "source_revision_ids": ("revision-1",),
        "evidence_ids": ("evidence-1",),
        "suggested_links": (
            SuggestedObjectiveLink(
                ObjectiveEdgeType.CONTRASTS_WITH,
                "immune-thrombocytopenia",
            ),
        ),
    }
    values.update(overrides)
    return ProposedObjective(**values)  # type: ignore[arg-type]


class Generator:
    def __init__(self, proposals: tuple[ProposedObjective, ...]) -> None:
        self.proposals = proposals
        self.calls: list[tuple[str, tuple[ObjectiveEvidenceInput, ...]]] = []

    def propose(
        self,
        prompt_version: str,
        evidence: tuple[ObjectiveEvidenceInput, ...],
    ) -> tuple[ProposedObjective, ...]:
        self.calls.append((prompt_version, evidence))
        return self.proposals


class Consolidator:
    def __init__(self, decision: bool = True) -> None:
        self.decision = decision
        self.calls: list[tuple[ConsolidationCandidate, ConsolidationCandidate]] = []

    def should_merge(
        self,
        left: ConsolidationCandidate,
        right: ConsolidationCandidate,
    ) -> bool:
        self.calls.append((left, right))
        return self.decision


def test_proposed_objective_requires_observable_testable_grounded_shape() -> None:
    with pytest.raises(ValueError, match="observable verb"):
        _proposal(observable_verb="understand")
    with pytest.raises(ValueError, match="evidence_ids"):
        _proposal(evidence_ids=())
    with pytest.raises(ValueError, match="suggested link"):
        _proposal(
            suggested_links=(SuggestedObjectiveLink(ObjectiveEdgeType.PART_OF, "coagulation"),)
        )


def test_exact_duplicates_collapse_before_model_consolidation() -> None:
    generator = Generator(
        (
            _proposal(),
            _proposal(
                concept="Heparin induced thrombocytopenia",
                description="Differentiate heparin induced thrombocytopenia.",
            ),
        )
    )
    consolidator = Consolidator()

    result = ObjectiveExtractor(Knowledge(), generator, consolidator).extract(("revision-1",))

    assert len(result) == 1
    assert consolidator.calls == []
    assert generator.calls[0][0] == OBJECTIVE_EXTRACTION_PROMPT_VERSION
    assert tuple(asdict(item) for item in generator.calls[0][1]) == (
        {
            "evidence_id": "evidence-1",
            "source_revision_id": "revision-1",
            "text": "Heparin exposure can precede thrombocytopenia.",
            "course_id": "heme",
            "exam_id": "exam-2",
            "lecture_id": "lecture-13",
        },
    )


def test_only_ambiguous_near_duplicates_reach_bounded_consolidation() -> None:
    generator = Generator(
        (
            _proposal(),
            _proposal(
                concept="heparin-induced thrombocytopenia syndrome",
                description="Differentiate the clinical HIT syndrome.",
            ),
        )
    )
    consolidator = Consolidator()

    result = ObjectiveExtractor(Knowledge(), generator, consolidator).extract(("revision-1",))

    assert len(result) == 1
    assert len(consolidator.calls) == 1
    left, right = consolidator.calls[0]
    assert set(asdict(left)) == {"text", "evidence_ids"}
    assert set(asdict(right)) == {"text", "evidence_ids"}
    assert "Heparin exposure" not in left.text
    assert left.evidence_ids == right.evidence_ids == ("evidence-1",)


def test_all_exact_duplicates_collapse_before_ambiguous_consolidation() -> None:
    second = replace(_evidence("evidence-2"), normalized_text="HIT may cause thrombosis.")
    generator = Generator(
        (
            _proposal(),
            _proposal(
                concept="heparin-induced thrombocytopenia syndrome",
                description="Differentiate the clinical HIT syndrome.",
            ),
            _proposal(
                concept="Heparin induced thrombocytopenia syndrome",
                description="Differentiate the clinical HIT syndrome.",
                evidence_ids=("evidence-2",),
            ),
        )
    )
    consolidator = Consolidator(decision=False)

    result = ObjectiveExtractor(
        Knowledge(evidence=(_evidence(), second)),
        generator,
        consolidator,
    ).extract(("revision-1",))

    assert len(result) == 2
    assert len(consolidator.calls) == 1
    assert consolidator.calls[0][1].evidence_ids == ("evidence-1", "evidence-2")


def test_extractor_fails_closed_for_unready_or_invented_source_evidence() -> None:
    unready_generator = Generator((_proposal(),))
    with pytest.raises(ValueError, match="ready"):
        ObjectiveExtractor(
            Knowledge(revision=_revision(SourceRevisionState.STAGED)),
            unready_generator,
        ).extract(("revision-1",))
    assert unready_generator.calls == []

    unsupported_generator = Generator((_proposal(),))
    with pytest.raises(ValueError, match="requires evidence"):
        ObjectiveExtractor(
            Knowledge(evidence=(_evidence(authority=AuthorityClass.GENERATED_ARTIFACT),)),
            unsupported_generator,
        ).extract(("revision-1",))
    assert unsupported_generator.calls == []

    with pytest.raises(KeyError, match="invented"):
        ObjectiveExtractor(
            Knowledge(),
            Generator((_proposal(evidence_ids=("invented",)),)),
        ).extract(("revision-1",))


def test_extractor_reuses_gate2a_scope_policy_and_ids_are_deterministic() -> None:
    generator = Generator((_proposal(course_id="cardio"),))
    extractor = ObjectiveExtractor(Knowledge(), generator)

    with pytest.raises(ValueError, match="course scope"):
        extractor.extract(("revision-1",))

    valid = ObjectiveExtractor(Knowledge(), Generator((_proposal(),)))
    assert (
        valid.extract(("revision-1",))[0].proposal_id
        == valid.extract(("revision-1",))[0].proposal_id
    )


def test_extraction_limits_reject_work_before_repository_or_generator_use() -> None:
    knowledge = Knowledge()
    generator = Generator((_proposal(),))
    with pytest.raises(ValueError, match="at most"):
        ObjectiveExtractor(knowledge, generator).extract(
            tuple(f"revision-{number}" for number in range(33))
        )
    assert knowledge.requested_revisions == []
    assert generator.calls == []

    oversized = replace(_evidence(), normalized_text="x" * 20_001)
    with pytest.raises(ValueError, match="evidence text"):
        ObjectiveExtractor(
            Knowledge(evidence=(oversized,)),
            generator,
        ).extract(("revision-1",))
    assert generator.calls == []


def test_proposal_limits_bound_fields_links_and_generator_output() -> None:
    with pytest.raises(ValueError, match="concept.*at most"):
        _proposal(concept="x" * 201)
    with pytest.raises(ValueError, match="description.*at most"):
        _proposal(description="x" * 1_001)
    with pytest.raises(ValueError, match="suggested_links.*at most"):
        _proposal(
            suggested_links=tuple(
                SuggestedObjectiveLink(
                    ObjectiveEdgeType.CONTRASTS_WITH,
                    f"concept-{number}",
                )
                for number in range(33)
            )
        )

    generator = Generator(tuple(_proposal() for _ in range(501)))
    with pytest.raises(ValueError, match="at most 500 proposals"):
        ObjectiveExtractor(Knowledge(), generator).extract(("revision-1",))
