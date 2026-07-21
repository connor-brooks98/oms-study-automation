from oms_hub.matching import CatalogLecture, LectureMatcher
from oms_hub.outlook_parser import parse_lecture_title


def test_exact_number_subject_and_title_is_high_confidence():
    matcher = LectureMatcher(
        [
            CatalogLecture(
                41,
                "Heme/Lymph",
                1,
                4,
                "Anemia I",
                "Jun Wang, MD, PhD",
            )
        ]
    )

    result = matcher.match(
        parse_lecture_title(
            "4K. Heme/Lymph: Anemia I | Jun Wang, MD, PhD"
        )
    )

    assert result.lecture_id == 41
    assert result.confidence >= 0.90
    assert result.needs_review is False
    assert "subject exact" in result.evidence


def test_conflicting_same_number_candidates_require_review():
    matcher = LectureMatcher(
        [
            CatalogLecture(1, "MSK", 1, 4, "Imaging", "A. Zeller, DO"),
            CatalogLecture(
                2,
                "MSK",
                2,
                4,
                "Pediatric Trauma",
                "R. McGill, DO",
            ),
        ]
    )

    result = matcher.match(
        parse_lecture_title("4K. MSK: Unknown Topic | Guest Lecturer")
    )

    assert result.lecture_id is None
    assert result.needs_review is True
    assert "competing candidates" in result.evidence


def test_missing_subject_and_number_candidate_requires_review():
    matcher = LectureMatcher([])

    result = matcher.match(
        parse_lecture_title("4K. MSK: Imaging | A. Zeller, DO")
    )

    assert result.lecture_id is None
    assert result.confidence == 0.0
    assert result.needs_review is True
    assert result.evidence == ("no subject/number candidate",)
