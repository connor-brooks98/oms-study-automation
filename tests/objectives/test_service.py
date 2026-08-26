from __future__ import annotations

from oms_hub.objectives.extraction import ProposedObjective
from oms_hub.objectives.service import (
    ObjectiveProposalDisposition,
    ObjectiveService,
)

from oms_hub.db import Database
from oms_hub.knowledge.ids import evidence_id, sha256_text, source_revision_id
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevisionState,
)
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.objectives.models import ObjectiveStatus
from oms_hub.objectives.repository import ObjectiveRepository
from oms_hub.providers.contracts import AuthorityClass

NOW = "2026-08-26T14:00:00+00:00"
LATER = "2026-08-26T15:00:00+00:00"


def _repositories() -> tuple[
    Database,
    KnowledgeRepository,
    ObjectiveRepository,
    ObjectiveService,
]:
    database = Database("sqlite://")
    knowledge = KnowledgeRepository(database)
    knowledge.initialize()
    objectives = ObjectiveRepository(database, knowledge)
    objectives.initialize()
    service = ObjectiveService(objectives, clock=lambda: NOW)
    service.initialize()
    return database, knowledge, objectives, service


def _seed(
    knowledge: KnowledgeRepository,
    suffix: str,
    *,
    course_id: str = "heme",
) -> tuple[str, str]:
    source_id = f"source-{suffix}"
    file_hash = sha256_text(f"file-{suffix}")
    revision_id = source_revision_id(source_id, file_hash)
    text = f"Supported fact {suffix}."
    unit_id = evidence_id(revision_id, "slide:1", sha256_text(text))
    knowledge.create_source(source_id, AuthorityClass.COURSE_MATERIAL)
    knowledge.create_revision(source_id, file_hash, SourceRevisionState.READY)
    knowledge.put_evidence_units(
        revision_id,
        (
            EvidenceUnit(
                evidence_id=unit_id,
                source_revision_id=revision_id,
                authority_class=AuthorityClass.COURSE_MATERIAL,
                course_id=course_id,
                exam_id="exam-2",
                lecture_id="lecture-13",
                locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, "1"),
                normalized_text=text,
                content_sha256=sha256_text(text),
                created_at="2026-08-26T12:00:00+00:00",
            ),
        ),
    )
    return revision_id, unit_id


def _proposal(
    revision_id: str,
    unit_id: str,
    **overrides: object,
) -> ProposedObjective:
    values: dict[str, object] = {
        "observable_verb": "differentiate",
        "concept": "heparin-induced thrombocytopenia",
        "description": "Differentiate HIT from other thrombocytopenias.",
        "course_id": "heme",
        "exam_id": "exam-2",
        "lecture_ids": ("lecture-13",),
        "source_revision_ids": (revision_id,),
        "evidence_ids": (unit_id,),
    }
    values.update(overrides)
    return ProposedObjective(**values)  # type: ignore[arg-type]


def test_captured_proposals_are_durable_but_not_authoritative() -> None:
    database, knowledge, objectives, service = _repositories()
    try:
        revision_id, unit_id = _seed(knowledge, "hit")
        proposal = _proposal(revision_id, unit_id)

        record = service.capture((proposal,))[0]
        restarted = ObjectiveService(objectives, clock=lambda: LATER)
        restarted.initialize()

        assert record.disposition is ObjectiveProposalDisposition.PENDING
        assert restarted.list_proposals() == (record,)
        assert objectives.get_objective(proposal.proposal_id) is None
    finally:
        database.close()


def test_human_approval_revalidates_evidence_and_is_idempotent() -> None:
    database, knowledge, objectives, service = _repositories()
    try:
        revision_id, unit_id = _seed(knowledge, "hit")
        proposal = _proposal(revision_id, unit_id)
        service.capture((proposal,))

        approved = service.approve(proposal.proposal_id)
        retry = service.approve(proposal.proposal_id)
        objective = objectives.get_objective(proposal.proposal_id)

        assert approved == retry
        assert approved.disposition is ObjectiveProposalDisposition.APPROVED
        assert approved.approved_objective_id == proposal.proposal_id
        assert objective is not None and objective.status is ObjectiveStatus.APPROVED
        assert objective.evidence_ids == (unit_id,)
    finally:
        database.close()


def test_approval_fails_closed_when_source_is_no_longer_ready() -> None:
    database, knowledge, objectives, service = _repositories()
    try:
        revision_id, unit_id = _seed(knowledge, "hit")
        proposal = _proposal(revision_id, unit_id)
        service.capture((proposal,))
        knowledge.retire_revision(revision_id)

        try:
            service.approve(proposal.proposal_id)
        except ValueError as error:
            assert "ready" in str(error)
        else:
            raise AssertionError("stale proposal was approved")

        assert service.get_proposal(proposal.proposal_id).disposition is (
            ObjectiveProposalDisposition.PENDING
        )
        assert objectives.get_objective(proposal.proposal_id) is None
    finally:
        database.close()


def test_merge_is_audited_and_combines_only_pending_same_scope_proposals() -> None:
    database, knowledge, objectives, service = _repositories()
    try:
        first_revision, first_unit = _seed(knowledge, "hit")
        second_revision, second_unit = _seed(knowledge, "hit-syndrome")
        target = _proposal(first_revision, first_unit)
        source = _proposal(
            second_revision,
            second_unit,
            concept="heparin-induced thrombocytopenia syndrome",
        )
        service.capture((target, source))

        merged = service.merge(source.proposal_id, target.proposal_id)
        source_record = service.get_proposal(source.proposal_id)

        assert merged.proposal.evidence_ids == (first_unit, second_unit)
        assert merged.proposal.source_revision_ids == (first_revision, second_revision)
        assert source_record.disposition is ObjectiveProposalDisposition.MERGED
        assert source_record.merged_into_id == target.proposal_id
        assert objectives.get_objective(target.proposal_id) is None
    finally:
        database.close()


def test_retire_handles_pending_and_approved_records_without_rewriting_evidence() -> None:
    database, knowledge, objectives, service = _repositories()
    try:
        first_revision, first_unit = _seed(knowledge, "pending")
        pending = _proposal(first_revision, first_unit, concept="pending objective")
        second_revision, second_unit = _seed(knowledge, "approved")
        approved = _proposal(second_revision, second_unit, concept="approved objective")
        service.capture((pending, approved))

        pending_record = service.retire(pending.proposal_id)
        service.approve(approved.proposal_id)
        approved_record = service.retire(approved.proposal_id)
        retired_objective = objectives.get_objective(approved.proposal_id)

        assert pending_record.disposition is ObjectiveProposalDisposition.RETIRED
        assert objectives.get_objective(pending.proposal_id) is None
        assert approved_record.disposition is ObjectiveProposalDisposition.RETIRED
        assert retired_objective is not None
        assert retired_objective.status is ObjectiveStatus.RETIRED
        assert retired_objective.evidence_ids == (second_unit,)
    finally:
        database.close()
