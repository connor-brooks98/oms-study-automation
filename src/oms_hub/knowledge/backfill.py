"""Deterministic, read-only legacy slide revision backfill."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.document_processing.domain import ParsedDocument, SourceSnapshot
from oms_hub.document_processing.shadow import LegacyPptxProcessor
from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import StudyRevision, UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.knowledge.ids import source_revision_id as make_revision_id
from oms_hub.knowledge.models import EvidenceUnit, SourceRevision, SourceRevisionState
from oms_hub.knowledge.normalization import CourseRevisionInput, normalize_course_revision
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.providers.contracts import AuthorityClass
from oms_hub.repositories import CatalogRepository

__all__ = [
    "BackfillReport",
    "SlideRevisionBackfill",
    "backfill_all_ready_course_revisions",
    "backfill_slide_revision",
    "scope_ids",
]

_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*\Z")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_MAX_SIGNED_INT64 = 2**63 - 1


@dataclass(frozen=True, slots=True)
class BackfillReport:
    examined: int
    created: int
    already_present: int
    failed: int
    failure_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _BatchResult:
    report: BackfillReport
    examined_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    revision: StudyRevision
    source_document_id: str
    source_revision_id: str
    course_id: str
    exam_id: str
    lecture_id: str
    parsed_document: ParsedDocument
    evidence: tuple[EvidenceUnit, ...]


def _canonical_subject(subject: str) -> str:
    value = " ".join(unicodedata.normalize("NFKC", subject).casefold().split())
    if not value:
        raise ValueError("lecture subject must not be blank")
    return value


def scope_ids(subject: str, exam_number: int, lecture_number: int) -> tuple[str, str, str]:
    """Return bounded StoreKey-safe course, exam, and lecture identifiers."""
    canonical = _canonical_subject(subject)
    for label, number in (("exam", exam_number), ("lecture", lecture_number)):
        if not isinstance(number, int) or not 1 <= number <= _MAX_SIGNED_INT64:
            raise ValueError(f"{label} number must be a positive signed-64-bit integer")
    slug = re.sub(r"[^a-z0-9]+", "-", canonical).strip("-")
    course = f"{(slug[:74].rstrip('-') or 'course')}-{sha256(canonical.encode()).hexdigest()[:24]}"
    exam_seed = f"{canonical}\0exam\0{exam_number}"
    lecture_seed = f"{canonical}\0exam\0{exam_number}\0lecture\0{lecture_number}"
    exam = f"exam-{exam_number}-{sha256(exam_seed.encode()).hexdigest()[:24]}"
    lecture = f"lecture-{lecture_number}-{sha256(lecture_seed.encode()).hexdigest()[:24]}"
    values = (course, exam, lecture)
    if any(not _SAFE_ID.fullmatch(value) for value in values):
        raise ValueError("derived scope identifier is not StoreKey-safe")
    return values


class SlideRevisionBackfill:
    def __init__(
        self,
        ingestion: Any,
        catalog: Any,
        knowledge: KnowledgeRepository,
        *,
        parser: Any | None = None,
    ) -> None:
        self.ingestion = ingestion
        self.catalog = catalog
        self.knowledge = knowledge
        self.parser = parser or LegacyPptxProcessor()

    def backfill_slide_revision(self, slide_revision_id: str) -> SourceRevision:
        candidate = self._prepare(slide_revision_id)
        return self._activate_candidate(candidate)

    def _activate_candidate(self, candidate: _Candidate) -> SourceRevision:
        existing = self.knowledge.get_revision(candidate.source_revision_id)
        already_present = (
            existing is not None
            and existing.state is SourceRevisionState.READY
            and self.knowledge.has_exact_evidence(
                candidate.source_revision_id,
                candidate.evidence,
            )
        )
        activated = self.knowledge.activate_revision(
            candidate.source_revision_id,
            source_document_id=candidate.source_document_id,
            file_sha256=candidate.revision.source_sha256,
            source_family="legacy_slides",
            authority_class=AuthorityClass.COURSE_MATERIAL,
            course_id=candidate.course_id,
            exam_id=candidate.exam_id,
            lecture_id=candidate.lecture_id,
            units=candidate.evidence,
        )
        self._last_already_present = already_present
        return activated

    def backfill_all_ready_course_revisions(
        self,
        limit: int,
        *,
        dry_run: bool = False,
    ) -> BackfillReport:
        return self._backfill_all_ready_course_revisions(limit, dry_run=dry_run).report

    def _backfill_all_ready_course_revisions(
        self,
        limit: int,
        *,
        dry_run: bool,
    ) -> _BatchResult:
        if limit < 0:
            raise ValueError("limit must not be negative")
        candidates = self._eligible_revisions()
        examined_ids = tuple(str(revision.id) for revision in candidates)
        prepared: list[tuple[StudyRevision, _Candidate]] = []
        failures: list[str] = []
        for revision in candidates:
            try:
                prepared.append((revision, self._prepare(str(revision.id))))
            except Exception:
                failures.append(str(revision.id))
        selected = prepared[:limit]
        created = already_present = 0
        for revision, candidate in selected:
            revision_id = str(revision.id)
            try:
                if dry_run:
                    created += 1
                    continue
                self._activate_candidate(candidate)
                if getattr(self, "_last_already_present", False):
                    already_present += 1
                else:
                    created += 1
            except Exception:
                failures.append(revision_id)
        return _BatchResult(
            report=BackfillReport(
                examined=len(candidates),
                created=created,
                already_present=already_present,
                failed=len(failures),
                failure_ids=tuple(sorted(set(failures), key=int)),
            ),
            examined_ids=examined_ids,
        )

    def _eligible_revisions(self) -> list[StudyRevision]:
        revisions: list[StudyRevision] = []
        for lecture in self.catalog.list_lectures():
            current = self.ingestion.list_current_revisions(lecture.id)
            if isinstance(current, dict):
                revision = current.get(UploadKind.SLIDES)
                if revision is not None:
                    revisions.append(revision)
            else:
                revisions.extend(
                    revision for revision in current if revision.kind is UploadKind.SLIDES
                )
        return sorted(revisions, key=lambda value: value.id)

    def _prepare(self, slide_revision_id: str) -> _Candidate:
        if not _POSITIVE_DECIMAL.fullmatch(slide_revision_id):
            raise ValueError("slide revision ID must be canonical positive base-10")
        numeric_id = int(slide_revision_id)
        revision = self.ingestion.get_study_revision(numeric_id)
        if revision is None:
            raise KeyError(numeric_id)
        if revision.id != numeric_id:
            raise ValueError("legacy revision ID does not match the requested ID")
        if (
            revision.kind is not UploadKind.SLIDES
            or revision.state != "current"
            or not revision.current
        ):
            raise ValueError("legacy revision must be a current slide revision")
        source = revision.immutable_source_path
        if not source.is_file() or sha256_file(source) != revision.source_sha256:
            raise ValueError("immutable slide source is missing or checksum-mismatched")
        derived = revision.canonical_derived_path
        if not revision.derived_sha256 or derived is None or not derived.is_file():
            raise ValueError("canonical derived PDF metadata is missing")
        if sha256_file(derived) != revision.derived_sha256:
            raise ValueError("canonical derived PDF checksum is mismatched")
        lecture = self.catalog.get_lecture(revision.lecture_id)
        if lecture is None:
            raise KeyError(revision.lecture_id)
        course_id, exam_id, lecture_id = scope_ids(
            lecture.subject,
            lecture.exam_number,
            lecture.lecture_number,
        )
        source_document_id = f"legacy-study-revision:{revision.id}"
        source_revision = make_revision_id(source_document_id, revision.source_sha256)
        snapshot = SourceSnapshot(
            id=source_document_id,
            title=source.name,
            path=source,
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            sha256=revision.source_sha256,
        )
        parsed = self.parser.parse(snapshot, source.parent)
        if parsed.source_sha256 != revision.source_sha256:
            raise ValueError("parser changed the immutable source checksum")
        evidence = normalize_course_revision(
            CourseRevisionInput(
                source_revision_id=source_revision,
                course_id=course_id,
                exam_id=exam_id,
                lecture_id=lecture_id,
                parsed_document=parsed,
            )
        )
        if not evidence:
            raise ValueError("empty evidence cannot become ready")
        return _Candidate(
            revision=revision,
            source_document_id=source_document_id,
            source_revision_id=source_revision,
            course_id=course_id,
            exam_id=exam_id,
            lecture_id=lecture_id,
            parsed_document=parsed,
            evidence=evidence,
        )

def backfill_slide_revision(slide_revision_id: str, **dependencies: Any) -> SourceRevision:
    service = SlideRevisionBackfill(
        dependencies.pop("ingestion"),
        dependencies.pop("catalog"),
        dependencies.pop("knowledge"),
        parser=dependencies.pop("parser", None),
    )
    if dependencies:
        raise TypeError(f"unexpected dependencies: {', '.join(dependencies)}")
    candidate = service._prepare(slide_revision_id)
    return service._activate_candidate(candidate)


def backfill_all_ready_course_revisions(limit: int, **dependencies: Any) -> BackfillReport:
    service = SlideRevisionBackfill(
        dependencies.pop("ingestion"),
        dependencies.pop("catalog"),
        dependencies.pop("knowledge"),
        parser=dependencies.pop("parser", None),
    )
    dry_run = bool(dependencies.pop("dry_run", False))
    if dependencies:
        raise TypeError(f"unexpected dependencies: {', '.join(dependencies)}")
    return service.backfill_all_ready_course_revisions(limit, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    if not args.dry_run:
        raise SystemExit("Task 1.6 activation requires explicit application wiring")
    settings = Settings()
    database = Database(settings.database_url)
    try:
        knowledge = KnowledgeRepository(database)
        from sqlalchemy import inspect

        if not inspect(database.engine).has_table("source_revisions"):
            report = BackfillReport(0, 0, 0, 0, ())
            examined_ids: tuple[str, ...] = ()
            warnings = ["knowledge_schema_uninitialized"]
        else:
            batch = SlideRevisionBackfill(
                IngestionRepository(database),
                CatalogRepository(database),
                knowledge,
            )._backfill_all_ready_course_revisions(args.limit, dry_run=True)
            report = batch.report
            examined_ids = batch.examined_ids
            warnings = []
        print(
            json.dumps(
                {
                    "examined_revision_ids": list(examined_ids),
                    "examined": report.examined,
                    "created": report.created,
                    "already_present": report.already_present,
                    "failed": report.failed,
                    "failure_ids": list(report.failure_ids),
                    "warnings": ["dry_run", *warnings],
                },
                sort_keys=True,
            )
        )
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    main()
