from __future__ import annotations

from typing import Any, cast

import pytest

from oms_hub.knowledge.models import EvidenceLocator, EvidenceLocatorKind, EvidenceUnit
from oms_hub.knowledge.policy import (
    InsufficientEvidenceError,
    SourceScopeError,
    UnsupportedAuthorityError,
    allowed_authorities,
    assert_claim_evidence_allowed,
    filter_allowed_evidence,
    validate_scope,
)
from oms_hub.providers.contracts import (
    AuthorityClass,
    EvidenceRef,
    RetrievalScope,
    TruthMode,
)


def _scope(
    *,
    course_id: str = "heme",
    exam_id: str | None = "e2",
    lecture_ids: tuple[str, ...] = ("l13",),
    truth_mode: TruthMode = TruthMode.COURSE_ONLY,
    source_revision_ids: tuple[str, ...] = ("sr_1",),
) -> RetrievalScope:
    return RetrievalScope(course_id, exam_id, lecture_ids, truth_mode, source_revision_ids)


def _unit(**overrides: object) -> EvidenceUnit:
    values: dict[str, Any] = {
        "evidence_id": "ev_1",
        "source_revision_id": "sr_1",
        "authority_class": AuthorityClass.COURSE_MATERIAL,
        "course_id": "heme",
        "exam_id": "e2",
        "lecture_id": "l13",
        "locator": EvidenceLocator(EvidenceLocatorKind.SLIDE, "1"),
        "normalized_text": "text",
        "content_sha256": "a" * 64,
        "created_at": "2026-08-23T12:00:00+00:00",
    }
    values.update(overrides)
    return EvidenceUnit(**values)


def _ref(
    *,
    evidence_id: str = "ev_1",
    source_revision_id: str = "sr_1",
    authority_class: AuthorityClass = AuthorityClass.COURSE_MATERIAL,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id,
        source_revision_id,
        authority_class,
        "slide",
        "1",
        "text",
        "sha256:" + "a" * 64,
    )


@pytest.mark.parametrize(
    ("mode", "allowed"),
    [
        (TruthMode.COURSE_ONLY, {AuthorityClass.COURSE_MATERIAL}),
        (
            TruthMode.COURSE_AND_LITERATURE,
            {AuthorityClass.COURSE_MATERIAL, AuthorityClass.PUBLISHED_JOURNAL},
        ),
        (TruthMode.LITERATURE_ONLY, {AuthorityClass.PUBLISHED_JOURNAL}),
    ],
)
def test_truth_mode_matrix(
    mode: TruthMode, allowed: set[AuthorityClass]
) -> None:
    assert allowed_authorities(mode) == frozenset(allowed)


def test_validate_scope_accepts_default_course_scope() -> None:
    validate_scope(_scope())


@pytest.mark.parametrize(
    "scope",
    [
        _scope(course_id=""),
        _scope(course_id="   "),
        _scope(exam_id=""),
        _scope(lecture_ids=("",)),
        _scope(source_revision_ids=("",)),
        _scope(lecture_ids=("l13", "l13")),
        _scope(source_revision_ids=("sr_1", "sr_1")),
        _scope(truth_mode=cast(TruthMode, "not-a-mode")),
        RetrievalScope(cast(str, 123), "e2", ("l13",), TruthMode.COURSE_ONLY),
        RetrievalScope("heme", "e2", cast(tuple[str, ...], (123,)), TruthMode.COURSE_ONLY),
    ],
)
def test_validate_scope_rejects_malformed_blank_duplicate_or_invalid_values(
    scope: RetrievalScope,
) -> None:
    with pytest.raises(SourceScopeError):
        validate_scope(scope)


def test_validate_scope_is_pure_and_does_not_require_persistence() -> None:
    scope = _scope(source_revision_ids=("sr_missing",))

    validate_scope(scope)


@pytest.mark.parametrize(
    "mismatch",
    [
        {"course_id": "other"},
        {"exam_id": "e9"},
        {"lecture_id": "l99"},
        {"source_revision_id": "sr_9"},
    ],
)
def test_filter_rejects_explicit_course_exam_lecture_or_revision_mismatch(
    mismatch: dict[str, str],
) -> None:
    with pytest.raises(SourceScopeError):
        filter_allowed_evidence(_scope(), (_unit(**mismatch),))


def test_course_material_must_match_requested_course() -> None:
    with pytest.raises(SourceScopeError, match="course"):
        filter_allowed_evidence(_scope(course_id="cardio"), (_unit(),))


@pytest.mark.parametrize(
    "authority_class",
    [
        AuthorityClass.PUBLISHED_JOURNAL,
        AuthorityClass.GENERATED_ARTIFACT,
        AuthorityClass.QUESTION_STYLE_REFERENCE,
    ],
)
def test_non_course_evidence_without_optional_scope_metadata_can_be_filtered(
    authority_class: AuthorityClass,
) -> None:
    unit = _unit(
        evidence_id=f"ev_{authority_class.value}",
        authority_class=authority_class,
        course_id=None,
        exam_id=None,
        lecture_id=None,
    )

    assert filter_allowed_evidence(_scope(), (unit,)) == ()


@pytest.mark.parametrize(
    "field_name",
    ["course_id", "exam_id", "lecture_id"],
)
def test_non_course_populated_scope_metadata_must_match(field_name: str) -> None:
    values: dict[str, object] = {
        "authority_class": AuthorityClass.PUBLISHED_JOURNAL,
        "course_id": None,
        "exam_id": None,
        "lecture_id": None,
    }
    values[field_name] = {
        "course_id": "other",
        "exam_id": "e9",
        "lecture_id": "l99",
    }[field_name]

    with pytest.raises(SourceScopeError):
        filter_allowed_evidence(_scope(), (_unit(**values),))


def test_filter_returns_only_allowed_authorities_in_input_order() -> None:
    course = _unit(evidence_id="ev_course")
    literature = _unit(
        evidence_id="ev_literature",
        authority_class=AuthorityClass.PUBLISHED_JOURNAL,
        course_id=None,
        exam_id=None,
        lecture_id=None,
    )
    generated = _unit(
        evidence_id="ev_generated",
        authority_class=AuthorityClass.GENERATED_ARTIFACT,
    )

    result = filter_allowed_evidence(
        _scope(truth_mode=TruthMode.COURSE_AND_LITERATURE),
        (literature, generated, course),
    )

    assert result == (literature, course)


@pytest.mark.parametrize(
    "mode",
    [TruthMode.COURSE_ONLY, TruthMode.LITERATURE_ONLY],
)
def test_filter_returns_empty_tuple_when_authority_filter_excludes_all(
    mode: TruthMode,
) -> None:
    unit = _unit(
        authority_class=(
            AuthorityClass.PUBLISHED_JOURNAL
            if mode is TruthMode.COURSE_ONLY
            else AuthorityClass.COURSE_MATERIAL
        ),
        course_id=None if mode is TruthMode.COURSE_ONLY else "heme",
        exam_id=None if mode is TruthMode.COURSE_ONLY else "e2",
        lecture_id=None if mode is TruthMode.COURSE_ONLY else "l13",
    )

    assert filter_allowed_evidence(_scope(truth_mode=mode), (unit,)) == ()


@pytest.mark.parametrize(
    ("mode", "authority_class"),
    [
        (TruthMode.COURSE_ONLY, AuthorityClass.PUBLISHED_JOURNAL),
        (TruthMode.LITERATURE_ONLY, AuthorityClass.COURSE_MATERIAL),
        (TruthMode.COURSE_AND_LITERATURE, AuthorityClass.GENERATED_ARTIFACT),
        (TruthMode.COURSE_AND_LITERATURE, AuthorityClass.QUESTION_STYLE_REFERENCE),
    ],
)
def test_claim_rejects_disallowed_or_non_medical_authority(
    mode: TruthMode, authority_class: AuthorityClass
) -> None:
    with pytest.raises(UnsupportedAuthorityError):
        assert_claim_evidence_allowed(
            _scope(truth_mode=mode), (_ref(authority_class=authority_class),)
        )


def test_claim_rejects_empty_evidence() -> None:
    with pytest.raises(InsufficientEvidenceError):
        assert_claim_evidence_allowed(_scope(), ())


def test_claim_accepts_non_empty_allowed_evidence() -> None:
    assert_claim_evidence_allowed(_scope(), (_ref(),))
    assert_claim_evidence_allowed(
        _scope(truth_mode=TruthMode.LITERATURE_ONLY),
        (_ref(authority_class=AuthorityClass.PUBLISHED_JOURNAL),),
    )


def test_claim_rejects_revision_outside_explicit_allowlist() -> None:
    with pytest.raises(SourceScopeError, match="revision"):
        assert_claim_evidence_allowed(_scope(), (_ref(source_revision_id="sr_9"),))


def test_claim_allows_any_revision_without_explicit_allowlist() -> None:
    assert_claim_evidence_allowed(
        _scope(source_revision_ids=()), (_ref(source_revision_id="sr_9"),)
    )
