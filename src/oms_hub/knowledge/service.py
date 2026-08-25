"""Read-only application services for source trust and citation previews."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

from oms_hub.artifacts import ArtifactRole
from oms_hub.document_processing.domain import DocumentLocator, SourceSnapshot
from oms_hub.document_processing.shadow import LegacyPptxProcessor
from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import UploadKind
from oms_hub.knowledge.backfill import scope_ids
from oms_hub.knowledge.ids import source_revision_id as make_revision_id
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevisionState,
)
from oms_hub.knowledge.normalization import CourseRevisionInput, normalize_course_revision
from oms_hub.knowledge.policy import filter_allowed_evidence, validate_scope
from oms_hub.providers.contracts import AuthorityClass, RetrievalScope

__all__ = [
    "CanonicalInputArtifact",
    "CitationPreview",
    "DependencyProvenanceUnavailable",
    "EvidenceView",
    "IndexAssetView",
    "IndexInputView",
    "InvalidKnowledgeScope",
    "KnowledgeIntegrityError",
    "KnowledgeNotFoundError",
    "KnowledgeService",
    "KnowledgeServiceError",
    "PreviewUnavailableError",
    "SourceRevisionView",
    "SourceScopeView",
    "StaleReport",
    "UnsupportedRevisionState",
]

_LEGACY_SOURCE = re.compile(r"legacy-study-revision:([1-9][0-9]*)\Z")
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*\Z")
_SLIDE_COORDINATE = re.compile(r"(?:slide\s+)?([1-9][0-9]*)(?::[1-9][0-9]*)?\Z")


class KnowledgeServiceError(RuntimeError):
    """Base class for typed, fail-closed service errors."""


class KnowledgeNotFoundError(KnowledgeServiceError):
    pass


class KnowledgeIntegrityError(KnowledgeServiceError):
    pass


class PreviewUnavailableError(KnowledgeServiceError):
    pass


class InvalidKnowledgeScope(KnowledgeServiceError):
    pass


class UnsupportedRevisionState(KnowledgeServiceError):
    pass


class DependencyProvenanceUnavailable(KnowledgeServiceError):
    pass


@dataclass(frozen=True, slots=True)
class CanonicalInputArtifact:
    artifact_id: str
    role: ArtifactRole
    path: Path
    sha256: str
    media_type: str


@dataclass(frozen=True, slots=True)
class IndexAssetView:
    asset_id: str
    path: Path | None
    media_type: str
    sha256: str
    locator: DocumentLocator


@dataclass(frozen=True, slots=True)
class IndexInputView:
    source_document_id: str
    source_revision_id: str
    source_family: str
    revision_state: SourceRevisionState
    authority_class: AuthorityClass
    course_id: str
    exam_id: str
    lecture_id: str
    pptx: CanonicalInputArtifact
    pdf: CanonicalInputArtifact
    evidence_units: tuple[EvidenceUnit, ...]
    assets: tuple[IndexAssetView, ...]


@dataclass(frozen=True, slots=True)
class CitationPreview:
    artifact_id: str
    page_number: int


@dataclass(frozen=True, slots=True)
class EvidenceView:
    evidence_id: str
    source_revision_id: str
    authority_class: AuthorityClass
    locator: EvidenceLocator
    excerpt: str
    preview: CitationPreview


@dataclass(frozen=True, slots=True)
class SourceRevisionView:
    source_revision_id: str
    revision_state: SourceRevisionState
    upload_eligible: bool


@dataclass(frozen=True, slots=True)
class SourceScopeView:
    scope: RetrievalScope
    revisions: tuple[SourceRevisionView, ...]

    @property
    def sources(self) -> tuple[SourceRevisionView, ...]:
        return self.revisions

    @property
    def source_revisions(self) -> tuple[SourceRevisionView, ...]:
        return self.revisions


@dataclass(frozen=True, slots=True)
class StaleReport:
    source_revision_id: str
    dependent_artifact_ids: tuple[str, ...] = ()


class KnowledgeService:
    """Compose existing read boundaries without adding repository APIs."""

    def __init__(
        self,
        knowledge_or_container: Any,
        artifact_service: Any | None = None,
        *,
        parser: Any | None = None,
    ) -> None:
        if artifact_service is None and not hasattr(knowledge_or_container, "get_revision"):
            container = knowledge_or_container
            self.knowledge = _first_attr(
                container, "knowledge", "knowledge_repository", "repository"
            )
            self.artifacts = _first_attr(
                container, "artifacts", "artifact_service", "artifact"
            )
            parser = parser or getattr(container, "parser", None)
        else:
            self.knowledge = knowledge_or_container
            self.artifacts = artifact_service
        if self.artifacts is None:
            raise TypeError("an ArtifactService is required")
        self.parser = parser or LegacyPptxProcessor()

    def resolve_index_input(self, source_revision_id: str) -> IndexInputView:
        revision = self.knowledge.get_revision(source_revision_id)
        if revision is None:
            raise KnowledgeNotFoundError("source revision was not found")
        state = SourceRevisionState(revision.state)
        if state not in {
            SourceRevisionState.READY,
            SourceRevisionState.STALE,
            SourceRevisionState.RETIRED,
        }:
            raise UnsupportedRevisionState("source revision state is not indexable")
        self._require_course_authority(source_revision_id)
        legacy_id = self._legacy_id(revision.source_document_id)
        if (
            make_revision_id(revision.source_document_id, revision.file_sha256)
            != source_revision_id
        ):
            raise KnowledgeIntegrityError("source revision identity is inconsistent")
        stored = tuple(self.knowledge.list_evidence(source_revision_id))
        course_id, exam_id, lecture_id = _evidence_scope(stored, source_revision_id)
        self._validate_legacy_metadata(
            legacy_id,
            revision,
            course_id,
            exam_id,
            lecture_id,
        )
        pptx = self._artifact(legacy_id, ArtifactRole.PPTX, source_revision_id)
        pdf = self._artifact(legacy_id, ArtifactRole.PDF, source_revision_id)
        snapshot = SourceSnapshot(
            id=revision.source_document_id,
            title=pptx.path.name,
            path=pptx.path,
            media_type=pptx.media_type,
            sha256=pptx.sha256,
        )
        try:
            parsed = self.parser.parse(snapshot, pptx.path.parent)
        except Exception as error:
            raise KnowledgeIntegrityError("canonical source cannot be reparsed") from error
        if (
            parsed.source_id != revision.source_document_id
            or parsed.source_sha256 != revision.file_sha256
            or parsed.parser_name != LegacyPptxProcessor.name
            or parsed.parser_version != LegacyPptxProcessor.version
        ):
            raise KnowledgeIntegrityError("canonical source identity changed")
        expected = normalize_course_revision(
            CourseRevisionInput(
                source_revision_id=source_revision_id,
                course_id=course_id,
                exam_id=exam_id,
                lecture_id=lecture_id,
                parsed_document=parsed,
            )
        )
        if _evidence_identities(stored) != _evidence_identities(expected):
            raise KnowledgeIntegrityError("stored evidence does not match canonical source")
        _validate_asset_references(stored, parsed.assets)
        assets = tuple(sorted(
            (self._asset(source_revision_id, asset) for asset in parsed.assets),
            key=lambda asset: asset.asset_id,
        ))
        return IndexInputView(
            source_document_id=revision.source_document_id,
            source_revision_id=source_revision_id,
            source_family="legacy_slides",
            revision_state=state,
            authority_class=AuthorityClass.COURSE_MATERIAL,
            course_id=course_id,
            exam_id=exam_id,
            lecture_id=lecture_id,
            pptx=pptx,
            pdf=pdf,
            evidence_units=tuple(sorted(stored, key=_evidence_sort_key)),
            assets=assets,
        )

    def get_revision_view(self, source_revision_id: str) -> SourceRevisionView:
        revision = self.knowledge.get_revision(source_revision_id)
        if revision is None:
            raise KnowledgeNotFoundError("source revision was not found")
        state = SourceRevisionState(revision.state)
        if state not in {
            SourceRevisionState.READY,
            SourceRevisionState.STALE,
            SourceRevisionState.RETIRED,
        }:
            raise UnsupportedRevisionState("source revision state is not reconcilable")
        return SourceRevisionView(source_revision_id, state, state is SourceRevisionState.READY)

    def get_scope_sources(self, scope: RetrievalScope) -> SourceScopeView:
        try:
            validate_scope(scope)
        except Exception as error:
            raise InvalidKnowledgeScope(str(error)) from error
        rows = getattr(self, "_scope_rows", None)
        if rows is None:
            rows = self._scope_query(scope)
        selected: dict[str, tuple[EvidenceUnit, SourceRevisionState]] = {}
        scoped_rows: list[tuple[EvidenceUnit, SourceRevisionState]] = []
        units: list[EvidenceUnit] = []
        for row in rows:
            if isinstance(row, tuple):
                unit, state = row
            else:
                unit = row
                revision = self.knowledge.get_revision(unit.source_revision_id)
                if revision is None:
                    continue
                state = SourceRevisionState(revision.state)
            if state not in {
                SourceRevisionState.READY,
                SourceRevisionState.STALE,
                SourceRevisionState.RETIRED,
            }:
                continue
            if unit.authority_class is not AuthorityClass.COURSE_MATERIAL:
                continue
            if unit.course_id != scope.course_id:
                continue
            if scope.exam_id is not None and unit.exam_id != scope.exam_id:
                continue
            if scope.lecture_ids and unit.lecture_id not in scope.lecture_ids:
                continue
            if (
                scope.source_revision_ids
                and unit.source_revision_id not in scope.source_revision_ids
            ):
                continue
            scoped_rows.append((unit, state))
            units.append(unit)
        try:
            evidence = filter_allowed_evidence(scope, units)
        except Exception as error:
            raise InvalidKnowledgeScope(str(error)) from error
        for unit in evidence:
            state = next(
                state
                for candidate, state in scoped_rows
                if candidate.evidence_id == unit.evidence_id
            )
            selected.setdefault(unit.source_revision_id, (unit, state))
        revisions = tuple(
            SourceRevisionView(revision_id, state, state is SourceRevisionState.READY)
            for revision_id, (_, state) in sorted(selected.items())
        )
        return SourceScopeView(scope, revisions)

    def resolve_evidence(self, evidence_id: str) -> EvidenceView:
        unit = self._evidence_by_id(evidence_id)
        if unit is None:
            raise KnowledgeNotFoundError("evidence was not found")
        slide_match = (
            _SLIDE_COORDINATE.fullmatch(unit.locator.value)
            if unit.locator.kind is EvidenceLocatorKind.SLIDE
            else None
        )
        if slide_match is None:
            raise PreviewUnavailableError("evidence does not have an unambiguous slide preview")
        revision = self.knowledge.get_revision(unit.source_revision_id)
        if revision is None:
            raise KnowledgeNotFoundError("source revision was not found")
        state = SourceRevisionState(revision.state)
        if state not in {
            SourceRevisionState.READY,
            SourceRevisionState.STALE,
            SourceRevisionState.RETIRED,
        }:
            raise UnsupportedRevisionState("source revision state is not reconcilable")
        self._require_course_authority(unit.source_revision_id)
        legacy_id = self._legacy_id(revision.source_document_id)
        course_id, exam_id, lecture_id = _evidence_scope(
            (unit,), unit.source_revision_id
        )
        self._validate_legacy_metadata(
            legacy_id,
            revision,
            course_id,
            exam_id,
            lecture_id,
        )
        page = int(slide_match.group(1))
        self._artifact(legacy_id, ArtifactRole.PDF, unit.source_revision_id)
        return EvidenceView(
            evidence_id=unit.evidence_id,
            source_revision_id=unit.source_revision_id,
            authority_class=unit.authority_class,
            locator=unit.locator,
            excerpt=unit.normalized_text,
            preview=CitationPreview(str(legacy_id), page),
        )

    def _require_course_authority(self, revision_id: str) -> None:
        authority = getattr(self.knowledge, "source_authority", None)
        database = getattr(self.knowledge, "database", None)
        engine = getattr(database, "engine", None)
        if engine is not None:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT s.authority_class
                        FROM source_revisions r
                        JOIN knowledge_sources s ON s.id = r.source_document_id
                        WHERE r.id = :revision_id
                        """
                    ),
                    {"revision_id": revision_id},
                ).first()
            authority = None if row is None else row[0]
        if authority is None:
            raise KnowledgeNotFoundError("source knowledge record was not found")
        try:
            resolved_authority = AuthorityClass(authority)
        except ValueError as error:
            raise KnowledgeIntegrityError("source authority is invalid") from error
        if resolved_authority is not AuthorityClass.COURSE_MATERIAL:
            raise KnowledgeIntegrityError("source authority is not course_material")

    def _validate_legacy_metadata(
        self,
        legacy_id: int,
        revision: Any,
        course_id: str,
        exam_id: str,
        lecture_id: str,
    ) -> None:
        repository = getattr(self.artifacts, "repository", None)
        catalog = getattr(self.artifacts, "catalog", None)
        if repository is None or catalog is None:
            raise KnowledgeIntegrityError("legacy read boundaries are unavailable")
        try:
            legacy = repository.get_study_revision(legacy_id)
        except KeyError as error:
            raise KnowledgeNotFoundError("legacy revision was not found") from error
        if legacy is None:
            raise KnowledgeNotFoundError("legacy revision was not found")
        try:
            kind = UploadKind(legacy.kind)
        except ValueError as error:
            raise KnowledgeIntegrityError("legacy revision kind is invalid") from error
        if legacy.id != legacy_id or kind is not UploadKind.SLIDES:
            raise KnowledgeIntegrityError("legacy revision is not a slide revision")
        if legacy.source_sha256 != revision.file_sha256:
            raise KnowledgeIntegrityError("legacy source hash does not match")
        if (
            not legacy.immutable_source_path.is_file()
            or sha256_file(legacy.immutable_source_path) != revision.file_sha256
        ):
            raise KnowledgeIntegrityError("legacy source file is invalid")
        lecture = catalog.get_lecture(legacy.lecture_id)
        if lecture is None:
            raise KnowledgeNotFoundError("legacy lecture was not found")
        expected_scope = scope_ids(lecture.subject, lecture.exam_number, lecture.lecture_number)
        if expected_scope != (course_id, exam_id, lecture_id):
            raise KnowledgeIntegrityError("legacy catalog scope does not match evidence")
        if not legacy.derived_sha256:
            raise KnowledgeIntegrityError("legacy PDF hash is missing")
        try:
            pdf = self.artifacts.resolve(legacy_id, ArtifactRole.PDF)
        except Exception as error:
            raise KnowledgeIntegrityError("legacy PDF artifact is unavailable") from error
        if not Path(pdf.path).is_file() or sha256_file(pdf.path) != legacy.derived_sha256:
            raise KnowledgeIntegrityError("legacy PDF hash does not match")

    def mark_dependents_stale(self, source_revision_id: str) -> StaleReport:
        del source_revision_id
        raise DependencyProvenanceUnavailable(
            "dependent provenance is not available until Task 5.2"
        )

    def _scope_query(
        self, scope: RetrievalScope
    ) -> tuple[tuple[EvidenceUnit, SourceRevisionState], ...]:
        database = getattr(self.knowledge, "database", None)
        engine = getattr(database, "engine", None)
        if engine is None:
            raise KnowledgeIntegrityError("knowledge database handle is unavailable")
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT e.id, e.source_revision_id, e.authority_class,
                           r.state,
                           e.course_id, e.exam_id, e.lecture_id,
                           e.locator_kind, e.locator_value, e.normalized_text,
                           e.image_asset_id, e.content_sha256, e.source_priority,
                           e.created_at, e.retired_at
                    FROM evidence_units e
                    JOIN source_revisions r ON r.id = e.source_revision_id
                    WHERE r.state IN (:ready, :stale, :retired)
                      AND e.course_id = :course_id
                    ORDER BY e.source_revision_id, e.locator_kind,
                             e.locator_value, e.id
                    """
                ),
                {
                    "ready": SourceRevisionState.READY.value,
                    "stale": SourceRevisionState.STALE.value,
                    "retired": SourceRevisionState.RETIRED.value,
                    "course_id": scope.course_id,
                },
            ).mappings().all()
        return tuple(
            (_unit_from_row(row), SourceRevisionState(row["state"])) for row in rows
        )

    def _evidence_by_id(self, evidence_id: str) -> EvidenceUnit | None:
        rows = getattr(self, "_scope_rows", None)
        if rows is not None:
            return next((unit for unit in rows if unit.evidence_id == evidence_id), None)
        direct_rows = getattr(self.knowledge, "evidence", None)
        if direct_rows is not None:
            return next((unit for unit in direct_rows if unit.evidence_id == evidence_id), None)
        revision = getattr(self.knowledge, "revision", None)
        if revision is not None:
            return next(
                (unit for unit in self.knowledge.list_evidence(revision.source_revision_id)
                 if unit.evidence_id == evidence_id),
                None,
            )
        database = getattr(self.knowledge, "database", None)
        engine = getattr(database, "engine", None)
        if engine is None:
            return None
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT id, source_revision_id, authority_class, course_id,
                           exam_id, lecture_id, locator_kind, locator_value,
                           normalized_text, image_asset_id, content_sha256,
                           source_priority, created_at, retired_at
                    FROM evidence_units WHERE id = :id
                    """
                ),
                {"id": evidence_id},
            ).mappings().first()
        return None if row is None else _unit_from_row(row)

    def _artifact(
        self,
        legacy_id: int,
        role: ArtifactRole,
        revision_id: str,
    ) -> CanonicalInputArtifact:
        try:
            resolved = self.artifacts.resolve(legacy_id, role)
            path = Path(resolved.path)
            expected = sha256_file(path)
        except Exception as error:
            raise KnowledgeIntegrityError("canonical artifact is unavailable") from error
        if not path.is_file() or expected != getattr(resolved, "sha256", expected):
            raise KnowledgeIntegrityError("canonical artifact checksum is invalid")
        return CanonicalInputArtifact(
            artifact_id=f"{revision_id}:{role.value}",
            role=role,
            path=path,
            sha256=expected,
            media_type=str(resolved.media_type),
        )

    @staticmethod
    def _legacy_id(source_document_id: str) -> int:
        match = _LEGACY_SOURCE.fullmatch(source_document_id)
        if match is None:
            raise KnowledgeIntegrityError("source document is not a legacy slide source")
        return int(match.group(1))

    @staticmethod
    def _asset(source_revision_id: str, asset: Any) -> IndexAssetView:
        if asset.path is not None:
            try:
                if sha256_file(asset.path) != asset.sha256:
                    raise KnowledgeIntegrityError("asset checksum is invalid")
            except KnowledgeIntegrityError:
                raise
            except Exception as error:
                raise KnowledgeIntegrityError("asset file is unavailable") from error
        return IndexAssetView(
            asset_id=f"{source_revision_id}:{asset.key}",
            path=asset.path,
            media_type=asset.media_type,
            sha256=asset.sha256,
            locator=asset.locator,
        )


def _first_attr(value: Any, *names: str) -> Any:
    for name in names:
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _evidence_scope(units: tuple[EvidenceUnit, ...], revision_id: str) -> tuple[str, str, str]:
    if not units:
        raise KnowledgeIntegrityError("source revision has no evidence")
    scopes = {(unit.course_id, unit.exam_id, unit.lecture_id) for unit in units}
    if len(scopes) != 1:
        raise KnowledgeIntegrityError("evidence scope is inconsistent")
    course_id, exam_id, lecture_id = next(iter(scopes))
    if any(
        unit.source_revision_id != revision_id
        or unit.authority_class is not AuthorityClass.COURSE_MATERIAL
        or not isinstance(course_id, str)
        or not isinstance(exam_id, str)
        or not isinstance(lecture_id, str)
        for unit in units
    ):
        raise KnowledgeIntegrityError("evidence authority or scope is inconsistent")
    assert isinstance(course_id, str)
    assert isinstance(exam_id, str)
    assert isinstance(lecture_id, str)
    return course_id, exam_id, lecture_id


def _evidence_sort_key(unit: EvidenceUnit) -> tuple[str, str, str]:
    return unit.locator.kind.value, unit.locator.value, unit.evidence_id


def _evidence_identity(unit: EvidenceUnit) -> tuple[object, ...]:
    return (
        unit.evidence_id,
        unit.source_revision_id,
        unit.authority_class,
        unit.course_id,
        unit.exam_id,
        unit.lecture_id,
        unit.locator.kind,
        unit.locator.value,
        unit.normalized_text,
        unit.content_sha256,
        unit.image_asset_id,
        unit.source_priority,
    )


def _evidence_identities(units: tuple[EvidenceUnit, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(sorted((_evidence_identity(unit) for unit in units), key=str))


def _validate_asset_references(units: tuple[EvidenceUnit, ...], assets: Any) -> None:
    asset_keys = tuple(asset.key for asset in assets)
    if len(asset_keys) != len(set(asset_keys)):
        raise KnowledgeIntegrityError("parsed asset identities are ambiguous")
    known = set(asset_keys)
    for unit in units:
        if unit.image_asset_id is not None and unit.image_asset_id not in known:
            raise KnowledgeIntegrityError("evidence references a missing asset")
        if unit.locator.kind is EvidenceLocatorKind.FIGURE and unit.image_asset_id is None:
            raise KnowledgeIntegrityError("figure evidence is missing asset provenance")


def _unit_from_row(row: Any) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=row["id"],
        source_revision_id=row["source_revision_id"],
        authority_class=AuthorityClass(row["authority_class"]),
        course_id=row["course_id"],
        exam_id=row["exam_id"],
        lecture_id=row["lecture_id"],
        locator=EvidenceLocator(EvidenceLocatorKind(row["locator_kind"]), row["locator_value"]),
        normalized_text=row["normalized_text"],
        content_sha256=row["content_sha256"],
        image_asset_id=row["image_asset_id"],
        source_priority=row["source_priority"],
        created_at=row["created_at"],
        retired_at=row["retired_at"],
    )
