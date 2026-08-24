import json
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from pydantic import TypeAdapter

from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    KnowledgeSource,
    SourceRevision,
    SourceRevisionState,
)
from oms_hub.providers.contracts import AuthorityClass


def _evidence_unit(**overrides: object) -> EvidenceUnit:
    values: dict[str, Any] = {
        "evidence_id": "ev_1",
        "source_revision_id": "sr_1",
        "authority_class": AuthorityClass.COURSE_MATERIAL,
        "course_id": "heme",
        "exam_id": "e2",
        "lecture_id": "l13",
        "locator": EvidenceLocator(kind=EvidenceLocatorKind.SLIDE, value="1"),
        "normalized_text": "text",
        "content_sha256": "a" * 64,
        "created_at": "2026-08-23T12:00:00+00:00",
    }
    values.update(overrides)
    return EvidenceUnit(**values)


def _round_trip(model: object, expected: dict[str, object]) -> None:
    adapter = TypeAdapter(type(model))
    encoded = adapter.dump_json(model)
    assert json.loads(encoded) == expected
    assert adapter.validate_json(encoded) == model


def test_source_revision_states_are_explicit() -> None:
    assert [state.value for state in SourceRevisionState] == [
        "staged",
        "normalizing",
        "ready",
        "stale",
        "failed",
        "retired",
    ]


def test_evidence_locator_kinds_are_explicit() -> None:
    assert [kind.value for kind in EvidenceLocatorKind] == [
        "page",
        "slide",
        "speaker_note",
        "transcript_segment",
        "section",
        "figure",
        "table",
        "article_page",
    ]


def test_models_are_frozen_and_slotted() -> None:
    models = (
        KnowledgeSource("source_1", AuthorityClass.COURSE_MATERIAL),
        SourceRevision("source_1", "sr_1", "a" * 64, SourceRevisionState.READY),
        EvidenceLocator(EvidenceLocatorKind.SLIDE, "1"),
        _evidence_unit(),
    )
    for model in models:
        assert hasattr(type(model), "__slots__")
        with pytest.raises(FrozenInstanceError):
            model.__class__.__setattr__(model, next(iter(model.__slots__)), "changed")


def test_course_evidence_requires_course_scope() -> None:
    with pytest.raises(ValueError, match="course_id"):
        EvidenceUnit(
            evidence_id="ev_1",
            source_revision_id="sr_1",
            authority_class=AuthorityClass.COURSE_MATERIAL,
            course_id=None,
            exam_id=None,
            lecture_id=None,
            locator=EvidenceLocator(kind=EvidenceLocatorKind.SLIDE, value="1"),
            normalized_text="text",
            content_sha256="a" * 64,
        )


def test_generated_artifact_cannot_be_marked_claim_authority() -> None:
    unit = EvidenceUnit(
        evidence_id="ev_1",
        source_revision_id="sr_1",
        authority_class=AuthorityClass.GENERATED_ARTIFACT,
        course_id="heme",
        exam_id="e2",
        lecture_id="l13",
        locator=EvidenceLocator(kind=EvidenceLocatorKind.SECTION, value="summary"),
        normalized_text="derived summary",
        content_sha256="a" * 64,
    )
    assert unit.supports_medical_claims is False


@pytest.mark.parametrize(
    ("authority_class", "supports"),
    [
        (AuthorityClass.COURSE_MATERIAL, True),
        (AuthorityClass.PUBLISHED_JOURNAL, True),
        (AuthorityClass.GENERATED_ARTIFACT, False),
        (AuthorityClass.QUESTION_STYLE_REFERENCE, False),
    ],
)
def test_medical_authority_is_derived_from_authority_class(
    authority_class: AuthorityClass, supports: bool
) -> None:
    assert _evidence_unit(authority_class=authority_class).supports_medical_claims is supports


def test_evidence_defaults_are_optional_and_scope_is_preserved() -> None:
    unit = _evidence_unit(
        image_asset_id=None,
        source_priority=0,
        created_at="2026-08-23T12:00:00+00:00",
        retired_at=None,
    )
    assert unit.image_asset_id is None
    assert unit.source_priority == 0
    assert unit.created_at == "2026-08-23T12:00:00+00:00"
    assert unit.retired_at is None
    assert (unit.course_id, unit.exam_id, unit.lecture_id) == ("heme", "e2", "l13")


def test_source_revision_exposes_read_only_consumer_compatibility_property() -> None:
    revision = SourceRevision("source_1", "sr_1", "a" * 64, SourceRevisionState.READY)
    assert revision.revision_id == revision.source_revision_id
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        revision.revision_id = "sr_2"  # type: ignore[misc]


def test_models_round_trip_through_json() -> None:
    source = KnowledgeSource("source_1", AuthorityClass.COURSE_MATERIAL)
    _round_trip(
        source,
        {
            "source_document_id": "source_1",
            "authority_class": "course_material",
        },
    )

    revision = SourceRevision("source_1", "sr_1", "a" * 64, SourceRevisionState.READY)
    _round_trip(
        revision,
        {
            "source_document_id": "source_1",
            "source_revision_id": "sr_1",
            "file_sha256": "a" * 64,
            "state": "ready",
        },
    )

    locator = EvidenceLocator(EvidenceLocatorKind.ARTICLE_PAGE, "12")
    _round_trip(locator, {"kind": "article_page", "value": "12"})

    unit = _evidence_unit(
        authority_class=AuthorityClass.PUBLISHED_JOURNAL,
        image_asset_id="img_1",
        source_priority=3,
        retired_at="2026-08-24T12:00:00+00:00",
    )
    _round_trip(
        unit,
        {
            "evidence_id": "ev_1",
            "source_revision_id": "sr_1",
            "authority_class": "published_journal",
            "course_id": "heme",
            "exam_id": "e2",
            "lecture_id": "l13",
            "locator": {"kind": "slide", "value": "1"},
            "normalized_text": "text",
            "image_asset_id": "img_1",
            "content_sha256": "a" * 64,
            "source_priority": 3,
            "created_at": "2026-08-23T12:00:00+00:00",
            "retired_at": "2026-08-24T12:00:00+00:00",
        },
    )
