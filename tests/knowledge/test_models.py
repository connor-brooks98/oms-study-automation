import json
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest
from pydantic import TypeAdapter, ValidationError

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


def test_direct_construction_coerces_enum_strings() -> None:
    source = KnowledgeSource("source_1", cast(AuthorityClass, "course_material"))
    revision = SourceRevision("source_1", "sr_1", "a" * 64, cast(SourceRevisionState, "ready"))
    locator = EvidenceLocator(cast(EvidenceLocatorKind, "slide"), "1")
    unit = _evidence_unit(
        authority_class=cast(AuthorityClass, "course_material"),
        locator=EvidenceLocator(cast(EvidenceLocatorKind, "slide"), "1"),
    )

    assert source.authority_class is AuthorityClass.COURSE_MATERIAL
    assert revision.state is SourceRevisionState.READY
    assert locator.kind is EvidenceLocatorKind.SLIDE
    assert unit.authority_class is AuthorityClass.COURSE_MATERIAL
    assert unit.locator.kind is EvidenceLocatorKind.SLIDE


def test_direct_construction_rejects_unknown_enum_strings() -> None:
    with pytest.raises(ValueError):
        KnowledgeSource("source_1", cast(AuthorityClass, "unknown"))
    with pytest.raises(ValueError):
        SourceRevision("source_1", "sr_1", "a" * 64, cast(SourceRevisionState, "unknown"))
    with pytest.raises(ValueError):
        EvidenceLocator(cast(EvidenceLocatorKind, "unknown"), "1")
    with pytest.raises(ValueError):
        EvidenceUnit(
            evidence_id="ev_1",
            source_revision_id="sr_1",
            authority_class=cast(AuthorityClass, "unknown"),
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, "1"),
            normalized_text="text",
            content_sha256="a" * 64,
        )
    with pytest.raises(ValueError):
        EvidenceUnit(
            evidence_id="ev_1",
            source_revision_id="sr_1",
            authority_class=AuthorityClass.GENERATED_ARTIFACT,
            course_id="heme",
            exam_id="e2",
            lecture_id="l13",
            locator=EvidenceLocator(cast(EvidenceLocatorKind, "unknown"), "1"),
            normalized_text="text",
            content_sha256="a" * 64,
        )


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


@pytest.mark.parametrize("course_id", [None, "", "   ", 123])
def test_course_material_rejects_blank_or_non_string_course_scope(course_id: object) -> None:
    with pytest.raises(ValueError, match="course_id"):
        _evidence_unit(course_id=course_id)


@pytest.mark.parametrize("field_name", ["created_at", "retired_at"])
@pytest.mark.parametrize(
    "timestamp",
    [
        "",
        "not-a-timestamp",
        "2026-08-23T12:00:00",
        "2026-08-23T12:00:00+01:00",
        123,
    ],
)
def test_direct_construction_rejects_invalid_utc_timestamps(
    field_name: str, timestamp: object
) -> None:
    with pytest.raises(ValueError, match=field_name):
        _evidence_unit(**{field_name: timestamp})


@pytest.mark.parametrize("timestamp", ["2026-08-23T12:00:00Z", "2026-08-23T12:00:00+00:00"])
def test_direct_construction_preserves_utc_timestamp_forms(timestamp: str) -> None:
    unit = _evidence_unit(created_at=timestamp, retired_at=timestamp)
    assert unit.created_at == timestamp
    assert unit.retired_at == timestamp


def _evidence_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "evidence_id": "ev_1",
        "source_revision_id": "sr_1",
        "authority_class": "course_material",
        "course_id": "heme",
        "exam_id": "e2",
        "lecture_id": "l13",
        "locator": {"kind": "slide", "value": "1"},
        "normalized_text": "text",
        "content_sha256": "a" * 64,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("course_id", "   "),
        ("course_id", 123),
        ("created_at", "2026-08-23T12:00:00"),
        ("created_at", "2026-08-23T12:00:00+01:00"),
        ("retired_at", "not-a-timestamp"),
        ("retired_at", "2026-08-23T12:00:00-05:00"),
    ],
)
def test_type_adapter_rejects_invalid_scope_and_timestamps(
    field_name: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(EvidenceUnit).validate_python(_evidence_payload(**{field_name: value}))


@pytest.mark.parametrize("timestamp", ["2026-08-23T12:00:00Z", "2026-08-23T12:00:00+00:00"])
def test_type_adapter_accepts_utc_timestamp_forms(timestamp: str) -> None:
    unit = TypeAdapter(EvidenceUnit).validate_python(
        _evidence_payload(created_at=timestamp, retired_at=timestamp)
    )
    assert unit.created_at == timestamp
    assert unit.retired_at == timestamp


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-23T12:00:00+00",
        "2026-08-23T12:00:00+0000",
        "2026-08-23T12:00:00-00:00",
    ],
)
def test_direct_construction_rejects_noncanonical_utc_suffixes(timestamp: str) -> None:
    with pytest.raises(ValueError, match="created_at"):
        _evidence_unit(created_at=timestamp)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-23T12:00:00+00",
        "2026-08-23T12:00:00+0000",
        "2026-08-23T12:00:00-00:00",
    ],
)
def test_type_adapter_rejects_noncanonical_utc_suffixes(timestamp: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(EvidenceUnit).validate_python(_evidence_payload(created_at=timestamp))


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
