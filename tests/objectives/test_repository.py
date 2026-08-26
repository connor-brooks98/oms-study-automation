import pytest

from oms_hub.db import Database
from oms_hub.knowledge.ids import evidence_id, sha256_text, source_revision_id
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevisionState,
)
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.objectives.models import (
    LearningObjective,
    ObjectiveEdge,
    ObjectiveEdgeType,
    ObjectiveStatus,
)
from oms_hub.objectives.repository import ObjectiveRepository
from oms_hub.providers.contracts import AuthorityClass


def _repositories() -> tuple[Database, KnowledgeRepository, ObjectiveRepository]:
    database = Database("sqlite://")
    knowledge = KnowledgeRepository(database)
    knowledge.initialize()
    objectives = ObjectiveRepository(database, knowledge)
    objectives.initialize()
    return database, knowledge, objectives


def _seed_evidence(
    knowledge: KnowledgeRepository,
    *,
    suffix: str = "hit",
    authority: AuthorityClass = AuthorityClass.COURSE_MATERIAL,
    state: SourceRevisionState = SourceRevisionState.READY,
    course_id: str | None = "heme",
    exam_id: str | None = "exam-2",
    lecture_id: str | None = "lecture-13",
) -> tuple[str, str]:
    source_id = f"source-{suffix}"
    file_hash = sha256_text(f"file-{suffix}")
    revision_id = source_revision_id(source_id, file_hash)
    text = f"Supported fact {suffix}."
    unit_id = evidence_id(revision_id, "slide:1", sha256_text(text))
    knowledge.create_source(source_id, authority)
    knowledge.create_revision(source_id, file_hash, state)
    knowledge.put_evidence_units(
        revision_id,
        (
            EvidenceUnit(
                evidence_id=unit_id,
                source_revision_id=revision_id,
                authority_class=authority,
                course_id=course_id,
                exam_id=exam_id,
                lecture_id=lecture_id,
                locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, "1"),
                normalized_text=text,
                content_sha256=sha256_text(text),
                created_at="2026-08-25T12:00:00+00:00",
            ),
        ),
    )
    return revision_id, unit_id


def _objective(
    revision_id: str,
    unit_id: str,
    **overrides: object,
) -> LearningObjective:
    values: dict[str, object] = {
        "objective_id": "obj-hit",
        "display_name": "Recognize HIT",
        "concept_key": "recognize-hit",
        "description": "Distinguish HIT from other thrombocytopenias.",
        "course_id": "heme",
        "exam_id": "exam-2",
        "lecture_ids": ("lecture-13",),
        "status": ObjectiveStatus.APPROVED,
        "source_revision_ids": (revision_id,),
        "evidence_ids": (unit_id,),
        "blueprint_tags": ("hematology",),
        "created_at": "2026-08-25T12:00:00+00:00",
    }
    values.update(overrides)
    return LearningObjective(**values)  # type: ignore[arg-type]


def test_repository_round_trips_objective_and_immutable_evidence_links() -> None:
    database, knowledge, repository = _repositories()
    try:
        revision_id, unit_id = _seed_evidence(knowledge)
        objective = _objective(revision_id, unit_id)

        assert repository.create_objective(objective) == objective
        assert repository.get_objective(objective.objective_id) == objective
        assert repository.evidence_links(objective.objective_id) == (
            objective.evidence_links[0],
        )
        assert not hasattr(repository, "update_objective")
        assert not hasattr(repository, "delete_objective")
    finally:
        database.close()


def test_normalized_concept_key_is_unique_within_course() -> None:
    database, knowledge, repository = _repositories()
    try:
        revision_id, unit_id = _seed_evidence(knowledge)
        repository.create_objective(_objective(revision_id, unit_id))

        with pytest.raises(ValueError, match="concept key"):
            repository.create_objective(
                _objective(
                    revision_id,
                    unit_id,
                    objective_id="obj-hit-duplicate",
                    concept_key="Recognize HIT!",
                )
            )

        other_revision, other_unit = _seed_evidence(
            knowledge,
            suffix="foundations-hit",
            course_id="foundations",
            exam_id=None,
            lecture_id=None,
        )
        other_course = _objective(
            other_revision,
            other_unit,
            objective_id="obj-hit-other-course",
            course_id="foundations",
            exam_id=None,
            lecture_ids=(),
        )
        assert repository.create_objective(other_course) == other_course
    finally:
        database.close()


@pytest.mark.parametrize(
    ("authority", "state", "match"),
    [
        (AuthorityClass.GENERATED_ARTIFACT, SourceRevisionState.READY, "authority"),
        (AuthorityClass.COURSE_MATERIAL, SourceRevisionState.STAGED, "ready"),
    ],
)
def test_approved_objective_requires_allowed_ready_gate2a_evidence(
    authority: AuthorityClass,
    state: SourceRevisionState,
    match: str,
) -> None:
    database, knowledge, repository = _repositories()
    try:
        revision_id, unit_id = _seed_evidence(
            knowledge,
            authority=authority,
            state=state,
        )

        with pytest.raises(ValueError, match=match):
            repository.create_objective(_objective(revision_id, unit_id))

        assert repository.get_objective("obj-hit") is None
    finally:
        database.close()


def test_objective_evidence_must_match_declared_scope() -> None:
    database, knowledge, repository = _repositories()
    try:
        revision_id, unit_id = _seed_evidence(knowledge, course_id="cardio")

        with pytest.raises(ValueError, match="course scope"):
            repository.create_objective(_objective(revision_id, unit_id))
    finally:
        database.close()


def test_edges_are_idempotent_and_require_persisted_objectives() -> None:
    database, knowledge, repository = _repositories()
    try:
        first_revision, first_unit = _seed_evidence(knowledge, suffix="hit")
        second_revision, second_unit = _seed_evidence(knowledge, suffix="itp")
        repository.create_objective(_objective(first_revision, first_unit))
        repository.create_objective(
            _objective(
                second_revision,
                second_unit,
                objective_id="obj-itp",
                display_name="Recognize ITP",
                concept_key="recognize-itp",
            )
        )
        edge = ObjectiveEdge(
            source_objective_id="obj-hit",
            target_objective_id="obj-itp",
            edge_type=ObjectiveEdgeType.COMMONLY_CONFUSED_WITH,
            created_at="2026-08-25T12:00:00+00:00",
        )

        assert repository.add_edge(edge) == edge
        assert repository.add_edge(edge) == edge
        assert repository.edges_for_objective("obj-hit") == (edge,)
        with pytest.raises(KeyError, match="obj-missing"):
            repository.add_edge(
                ObjectiveEdge(
                    source_objective_id="obj-hit",
                    target_objective_id="obj-missing",
                    edge_type=ObjectiveEdgeType.PREREQUISITE,
                )
            )
    finally:
        database.close()
