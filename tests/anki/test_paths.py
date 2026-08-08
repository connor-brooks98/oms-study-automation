import pytest

from oms_hub.anki.paths import (
    LectureIdentity,
    canonical_component,
    target_deck,
    target_tag,
)


def test_confirmed_paths_are_derived_from_one_identity() -> None:
    identity = LectureIdentity(
        course="Heme Lymph",
        exam_number=1,
        lecture_number=4,
        topic="Anemia I",
    )

    assert target_deck(identity) == (
        "OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I"
    )
    assert target_tag(identity) == (
        "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"
    )


def test_component_normalization_collapses_unicode_whitespace_and_punctuation() -> None:
    assert canonical_component("  Hémé / Lymph — II  ", separator="_") == (
        "Heme_Lymph_II"
    )
    assert canonical_component(" Heme / Lymph ", separator="") == "HemeLymph"


def test_component_normalization_caps_output_at_eighty_characters() -> None:
    value = canonical_component("a" * 100, separator="_")

    assert value == "a" * 80


@pytest.mark.parametrize("value", ["", "   ", "///", "💉"])
def test_component_normalization_rejects_empty_results(value: str) -> None:
    with pytest.raises(ValueError, match="component"):
        canonical_component(value, separator="_")


@pytest.mark.parametrize("field", ["exam_number", "lecture_number"])
def test_lecture_identity_rejects_non_positive_numbers(field: str) -> None:
    values = {
        "course": "Heme Lymph",
        "exam_number": 1,
        "lecture_number": 4,
        "topic": "Anemia I",
    }
    values[field] = 0

    with pytest.raises(ValueError, match=field):
        LectureIdentity(**values)
