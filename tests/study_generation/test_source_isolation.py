import pytest

from oms_hub.study_generation.domain import (
    LectureSourceSet,
    RemoteSource,
    SourceIsolationError,
    SourceKind,
)


def source(remote_id, lecture_id, revision_id, kind, ready=True):
    return RemoteSource(
        remote_id,
        lecture_id,
        revision_id,
        str(revision_id).zfill(64),
        kind,
        ready,
    )


@pytest.mark.parametrize(
    ("pdf", "transcript"),
    [
        (
            source("pdf", 1, 10, SourceKind.LECTURE_PDF, False),
            source("txt", 1, 11, SourceKind.CLEANED_TRANSCRIPT),
        ),
        (
            source("pdf", 1, 10, SourceKind.LECTURE_PDF),
            source("txt", 2, 11, SourceKind.CLEANED_TRANSCRIPT),
        ),
        (
            source("pdf", 1, 10, SourceKind.CLEANED_TRANSCRIPT),
            source("txt", 1, 11, SourceKind.LECTURE_PDF),
        ),
        (
            source("same", 1, 10, SourceKind.LECTURE_PDF),
            source("same", 1, 11, SourceKind.CLEANED_TRANSCRIPT),
        ),
    ],
)
def test_invalid_source_set_is_rejected(pdf, transcript):
    with pytest.raises(SourceIsolationError):
        LectureSourceSet(lecture_id=1, pdf=pdf, transcript=transcript)
