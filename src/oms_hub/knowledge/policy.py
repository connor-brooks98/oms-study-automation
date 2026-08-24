"""Pure truth-mode and retrieval-scope policy checks."""

from __future__ import annotations

from collections.abc import Iterable

from oms_hub.knowledge.models import EvidenceUnit
from oms_hub.providers.contracts import AuthorityClass, EvidenceRef, RetrievalScope, TruthMode

__all__ = [
    "InsufficientEvidenceError",
    "SourceScopeError",
    "UnsupportedAuthorityError",
    "allowed_authorities",
    "assert_claim_evidence_allowed",
    "filter_allowed_evidence",
    "validate_scope",
]


class SourceScopeError(ValueError):
    """Raised when a scope or evidence record crosses its requested boundary."""


class UnsupportedAuthorityError(ValueError):
    """Raised when evidence cannot support claims under the requested mode."""


class InsufficientEvidenceError(RuntimeError):
    """Raised when a claim has no evidence references."""


def allowed_authorities(mode: TruthMode) -> frozenset[AuthorityClass]:
    """Return the medical authority classes enabled by a truth mode."""
    resolved_mode = TruthMode(mode)
    if resolved_mode is TruthMode.COURSE_ONLY:
        return frozenset({AuthorityClass.COURSE_MATERIAL})
    if resolved_mode is TruthMode.COURSE_AND_LITERATURE:
        return frozenset(
            {AuthorityClass.COURSE_MATERIAL, AuthorityClass.PUBLISHED_JOURNAL}
        )
    return frozenset({AuthorityClass.PUBLISHED_JOURNAL})


def validate_scope(scope: RetrievalScope) -> None:
    """Validate scope shape without checking persistence or source existence."""
    if not isinstance(scope, RetrievalScope):
        raise SourceScopeError("scope must be a RetrievalScope")

    _validate_identifier(scope.course_id, "course_id")
    if scope.exam_id is not None:
        _validate_identifier(scope.exam_id, "exam_id")
    _validate_identifier_sequence(scope.lecture_ids, "lecture_ids")
    _validate_identifier_sequence(scope.source_revision_ids, "source_revision_ids")
    if len(scope.lecture_ids) != len(set(scope.lecture_ids)):
        raise SourceScopeError("lecture_ids must not contain duplicates")
    if len(scope.source_revision_ids) != len(set(scope.source_revision_ids)):
        raise SourceScopeError("source_revision_ids must not contain duplicates")
    try:
        TruthMode(scope.truth_mode)
    except ValueError as error:
        raise SourceScopeError("truth_mode is invalid") from error


def filter_allowed_evidence(
    scope: RetrievalScope, evidence: Iterable[EvidenceUnit]
) -> tuple[EvidenceUnit, ...]:
    """Keep in-scope evidence whose authority is enabled by ``scope``."""
    validate_scope(scope)
    allowed = allowed_authorities(scope.truth_mode)
    result: list[EvidenceUnit] = []
    for unit in evidence:
        _validate_evidence_scope(scope, unit)
        if unit.authority_class in allowed:
            result.append(unit)
    return tuple(result)


def assert_claim_evidence_allowed(
    scope: RetrievalScope, refs: Iterable[EvidenceRef]
) -> None:
    """Assert that claim references are present, allowed, and revision-scoped."""
    validate_scope(scope)
    references = tuple(refs)
    if not references:
        raise InsufficientEvidenceError("claim requires at least one evidence reference")

    allowed = allowed_authorities(scope.truth_mode)
    for ref in references:
        if ref.authority_class not in allowed:
            raise UnsupportedAuthorityError(
                f"authority {ref.authority_class!r} cannot support this claim"
            )
    if scope.source_revision_ids:
        for ref in references:
            if ref.source_revision_id not in scope.source_revision_ids:
                raise SourceScopeError(
                    f"evidence reference {ref.evidence_id!r} has an out-of-scope revision"
                )


def _validate_identifier(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SourceScopeError(f"{field_name} must be a non-blank identifier")


def _validate_identifier_sequence(values: object, field_name: str) -> None:
    if not isinstance(values, tuple):
        raise SourceScopeError(f"{field_name} must be a tuple of identifiers")
    for value in values:
        _validate_identifier(value, field_name)


def _validate_evidence_scope(scope: RetrievalScope, unit: EvidenceUnit) -> None:
    if unit.authority_class is AuthorityClass.COURSE_MATERIAL:
        if unit.course_id != scope.course_id:
            raise SourceScopeError("course material does not match course scope")
    elif unit.course_id is not None and unit.course_id != scope.course_id:
        raise SourceScopeError("evidence course_id does not match course scope")

    if (
        scope.exam_id is not None
        and unit.exam_id is not None
        and unit.exam_id != scope.exam_id
    ):
        raise SourceScopeError("evidence exam_id does not match exam scope")
    if (
        scope.lecture_ids
        and unit.lecture_id is not None
        and unit.lecture_id not in scope.lecture_ids
    ):
        raise SourceScopeError("evidence lecture_id does not match lecture scope")
    if (
        scope.source_revision_ids
        and unit.source_revision_id not in scope.source_revision_ids
    ):
        raise SourceScopeError("evidence source_revision_id is outside the scope")
