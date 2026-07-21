import pytest

from oms_hub.outlook_parser import parse_lecture_title


def test_parses_real_outlook_title():
    parsed = parse_lecture_title(
        "4K. Heme/Lymph: Anemia I | Jun Wang, MD, PhD"
    )

    assert parsed.lecture_number == 4
    assert parsed.campus == "K"
    assert parsed.subject == "Heme/Lymph"
    assert parsed.topic == "Anemia I"
    assert parsed.lecturer == "Jun Wang, MD, PhD"


def test_parses_multicharacter_campus_code():
    parsed = parse_lecture_title(
        "12OP. Cardio: Heart Failure II | Jane Doe, DO"
    )

    assert parsed.campus == "OP"


def test_rejects_nonlecture_title():
    with pytest.raises(ValueError, match="unrecognized lecture title"):
        parse_lecture_title("Class Meeting")
