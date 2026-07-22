from dataclasses import replace

from oms_hub.domain import LectureStepName
from tests.canvas.test_classifier import attachment
from tests.canvas.test_pipeline import add_revision, prepared, stored_step


def proposed_pair(database, tmp_path):
    settings, catalog, lecture_id, repository, pipeline = prepared(database, tmp_path)
    first = add_revision(settings, repository, lecture_id, attachment("Anemia.pptx"))
    first_result = pipeline.process_revision(first.id)
    second_value = replace(
        attachment("Anemia.pptx"),
        modified_at="2026-07-22T12:00:00Z",
    )
    second = add_revision(settings, repository, lecture_id, second_value)
    pipeline.process_revision(second.id)
    return catalog, lecture_id, repository, pipeline, first_result, second


def test_approve_replacement_promotes_and_updates_goodnotes_detail(database, tmp_path) -> None:
    catalog, lecture_id, repository, pipeline, first_result, second = proposed_pair(
        database, tmp_path
    )
    result = pipeline.approve_replacement(second.id)
    assert result.paths.local_source is not None
    assert result.paths.local_source.read_bytes() == b"PK-source"
    assert repository.get_revision(second.id).state == "current"
    assert stored_step(
        catalog, lecture_id, LectureStepName.GOODNOTES_DELIVERED
    ).detail == "Updated PDF staged; Goodnotes re-import may be required"
    assert first_result.paths.local_pdf.exists()


def test_keep_current_suppresses_exact_proposal(database, tmp_path) -> None:
    _, _, repository, pipeline, _, second = proposed_pair(database, tmp_path)
    pipeline.keep_current(second.id)
    assert repository.get_revision(second.id).state == "kept"


def test_retry_rejects_missing_staged_source(database, tmp_path) -> None:
    _, _, repository, pipeline, _, second = proposed_pair(database, tmp_path)
    path = repository.get_revision(second.id).stored_path
    assert path is not None
    from pathlib import Path

    Path(path).unlink()
    try:
        pipeline.retry_revision(second.id)
    except ValueError as error:
        assert "checksum" in str(error) or "missing" in str(error)
    else:
        raise AssertionError("retry accepted a missing immutable source")
