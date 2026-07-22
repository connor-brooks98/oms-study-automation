from oms_hub.canvas.domain import SourceKind
from oms_hub.canvas.routing import build_paths
from oms_hub.config import Settings
from oms_hub.domain import LectureKey


def test_lecture_paths_follow_local_and_goodnotes_conventions(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        study_root=tmp_path / "OMS II",
        icloud_staging_root=tmp_path / "iCloud",
        revision_root=tmp_path / "revisions",
    )
    paths = build_paths(
        settings,
        LectureKey("Neuro", 1, 1, "General CNS Pathology"),
        SourceKind.LECTURE,
        "source.pptx",
        42,
    )
    assert paths.local_source == (
        tmp_path / "OMS II/Neuro/Exam 1/Lectures/Lecture 01 - General CNS Pathology.pptx"
    )
    assert paths.local_pdf == (
        tmp_path / "OMS II/Neuro/Exam 1/Lectures/Lecture 01 - General CNS Pathology.pdf"
    )
    assert paths.icloud_pdf == (
        tmp_path
        / "iCloud/OMS II Goodnotes Inbox/Neuro/Exam 1/Lecture 01 - General CNS Pathology.pdf"
    )
    assert paths.revision_original == tmp_path / "revisions/42/source.pptx"


def test_different_pq_names_have_stable_distinct_destinations(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        study_root=tmp_path / "study",
        icloud_staging_root=tmp_path / "cloud",
        revision_root=tmp_path / "revisions",
    )
    lecture = LectureKey("Cardio", 2, 7, "Heart Failure")
    first = build_paths(settings, lecture, SourceKind.PRACTICE_QUESTIONS, "Case A.docx", 1)
    second = build_paths(settings, lecture, SourceKind.PRACTICE_QUESTIONS, "Case B.docx", 2)
    assert first.local_pdf != second.local_pdf
    assert first.local_pdf.parent.name == "Practice Questions"
