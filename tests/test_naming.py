from oms_hub.domain import LectureKey
from oms_hub.naming import artifact_names, display_title, sanitize_filename


def test_canonical_lecture_names_use_colon_only_in_display_title():
    key = LectureKey(
        subject="Heme/Lymph",
        exam_number=1,
        lecture_number=4,
        topic="Anemia I",
    )

    assert display_title(key) == "Lecture 04: Anemia I"
    assert artifact_names(key).pptx == "Lecture 04 - Anemia I.pptx"
    assert artifact_names(key).pdf == "Lecture 04 - Anemia I.pdf"
    assert artifact_names(key).transcript == "Lecture 04 - Anemia I - Transcript.txt"
    assert artifact_names(key).summary == "Lecture 04 - NotebookLM Summary.pdf"


def test_windows_reserved_characters_are_replaced_and_whitespace_is_collapsed():
    key = LectureKey(
        subject="Neuro",
        exam_number=1,
        lecture_number=7,
        topic='Stroke: A/B? "Review"',
    )

    assert artifact_names(key).pdf == "Lecture 07 - Stroke - A-B - Review.pdf"


def test_windows_reserved_names_are_prefixed():
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("lpt1.txt") == "_lpt1.txt"
