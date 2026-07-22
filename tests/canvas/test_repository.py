from dataclasses import replace

import pytest

from oms_hub.canvas.classifier import classify_attachment
from oms_hub.canvas.domain import CatalogMatch, CourseMappingInput, ReviewState
from oms_hub.canvas.repository import CanvasRepository
from tests.canvas.test_classifier import attachment


def test_course_mappings_require_approved_unique_subjects(database) -> None:
    repository = CanvasRepository(database)
    repository.replace_course_mappings(
        [CourseMappingInput("720", "Clinical Neuroscience", "NEURO", "Neuro")]
    )
    assert repository.list_course_mappings()[0].subject == "Neuro"
    with pytest.raises(ValueError, match="approved"):
        repository.replace_course_mappings(
            [CourseMappingInput("bad", "Bad", "BAD", "Unknown")]
        )


def test_metadata_replay_is_idempotent_and_revision_change_is_preserved(database) -> None:
    repository = CanvasRepository(database)
    repository.replace_course_mappings(
        [CourseMappingInput("751", "Hematology & Lymph", "HEME", "Heme/Lymph")]
    )
    value = attachment("Anemia.pptx")
    classification = classify_attachment(value)
    match = CatalogMatch(7, "Heme/Lymph", 1, 0.99, "exact")
    first = repository.ingest_metadata(value, classification, match)
    replay = repository.ingest_metadata(value, classification, match)
    changed = repository.ingest_metadata(
        replace(value, modified_at="2026-07-22T12:00:00Z"), classification, match
    )
    assert first.revision_id == replay.revision_id
    assert changed.revision_id != first.revision_id
    assert repository.count_revisions(first.source_item_id) == 2


def test_review_list_only_returns_review_items(database) -> None:
    repository = CanvasRepository(database)
    repository.replace_course_mappings(
        [CourseMappingInput("751", "Hematology & Lymph", "HEME", "Heme/Lymph")]
    )
    value = attachment("practice questions.csv")
    repository.ingest_metadata(
        value,
        classify_attachment(value),
        CatalogMatch(None, "Heme/Lymph", None, 0.0, "unmatched"),
    )
    items = repository.list_review_items()
    assert len(items) == 1
    assert items[0].review_state == ReviewState.NEEDS_REVIEW.value
