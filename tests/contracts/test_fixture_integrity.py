"""Integrity checks for the deterministic grounded-learning test fixtures."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from oms_hub.providers import AuthorityClass, EvidenceRef, RetrievalScope, TruthMode
from tests.builders.knowledge import (
    build_evidence_ref,
    build_retrieval_scope,
    build_source_revision,
)
from tests.builders.questions import build_board_question_draft

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "grounded_learning"
COURSE_SOURCE = FIXTURE_ROOT / "course" / "lecture-13-normalized.md"
COURSE_PAGE_MAP = FIXTURE_ROOT / "course" / "lecture-13-pages.json"
LITERATURE_SOURCE = FIXTURE_ROOT / "literature" / "article-1-normalized.md"
README = FIXTURE_ROOT / "README.md"
BUILDER_PATHS = (
    Path(__file__).resolve().parents[1] / "builders" / "knowledge.py",
    Path(__file__).resolve().parents[1] / "builders" / "questions.py",
)

COURSE_MARKERS = frozenset(
    {
        "course_l13_intrinsic_pathway",
        "course_l13_extrinsic_pathway",
        "course_l13_pt",
        "course_l13_ptt",
        "course_l13_hemophilia_a",
        "course_l13_hit_timing",
        "course_l13_hit_mechanism",
        "course_l13_dic_pattern",
    }
)
LITERATURE_MARKERS = frozenset(
    {
        "literature_a1_hit_rapid_onset",
        "literature_a1_hit_antibody_persistence",
    }
)
ALL_MARKERS = COURSE_MARKERS | LITERATURE_MARKERS
MARKER_PATTERN = re.compile(r"\[EVIDENCE:([a-z0-9_]+)\]")
MARKER_START_PATTERN = re.compile(r"^\[EVIDENCE:([a-z0-9_]+)\] ")
REQUIRED_FILES = (COURSE_SOURCE, COURSE_PAGE_MAP, LITERATURE_SOURCE, README)


def _source_markers(path: Path) -> list[str]:
    return MARKER_PATTERN.findall(path.read_text(encoding="utf-8"))


def _source_lines(path: Path) -> list[str]:
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            assert not MARKER_PATTERN.search(line), "headings cannot hide evidence markers"
            continue
        if stripped == "This heading is metadata, not an evidence paragraph.":
            continue
        lines.append(line)
    return lines


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"expected mapping, got {type(value).__name__}")
    return value


def test_required_fixture_files_exist() -> None:
    assert all(path.is_file() for path in REQUIRED_FILES)


def test_synthetic_evidence_markers_are_unique() -> None:
    markers = _source_markers(COURSE_SOURCE) + _source_markers(LITERATURE_SOURCE)
    assert markers
    assert len(markers) == len(set(markers))
    assert set(markers) == ALL_MARKERS
    assert all(re.fullmatch(r"[a-z0-9_]+", marker) for marker in markers)


@pytest.mark.parametrize("path", (COURSE_SOURCE, LITERATURE_SOURCE))
def test_every_source_line_has_one_leading_marker(path: Path) -> None:
    for line in _source_lines(path):
        assert MARKER_START_PATTERN.match(line)
        assert len(MARKER_PATTERN.findall(line)) == 1


def test_course_page_map_is_complete_and_stable() -> None:
    page_map = _mapping(json.loads(COURSE_PAGE_MAP.read_text(encoding="utf-8")))
    assert set(page_map) == {
        "schema_version",
        "fixture_id",
        "source_document_id",
        "source_revision_id",
        "authority_class",
        "scope",
        "pages",
    }
    assert page_map["schema_version"] == 1
    assert page_map["fixture_id"] == "synthetic_course_lecture_13_v1"
    assert page_map["source_document_id"] == "source_fixture_course_l13"
    assert page_map["source_revision_id"] == "sr_fixture_course_l13_v1"
    assert page_map["authority_class"] == "course_material"

    scope = _mapping(page_map["scope"])
    assert scope == {"course_id": "heme", "exam_id": "e2", "lecture_id": "l13"}

    pages = page_map["pages"]
    assert isinstance(pages, list)
    page_numbers: list[int] = []
    slide_numbers: list[int] = []
    mapped_markers: list[str] = []
    for page in pages:
        page_record = _mapping(page)
        assert set(page_record) == {"page_number", "slide_number", "evidence_markers"}
        page_number = page_record["page_number"]
        slide_number = page_record["slide_number"]
        markers = page_record["evidence_markers"]
        assert isinstance(page_number, int) and page_number > 0
        assert isinstance(slide_number, int) and slide_number > 0
        assert isinstance(markers, list)
        page_numbers.append(page_number)
        slide_numbers.append(slide_number)
        for marker in markers:
            assert isinstance(marker, str)
            assert re.fullmatch(r"[a-z0-9_]+", marker)
            mapped_markers.append(marker)

    assert page_numbers == sorted(page_numbers)
    assert len(page_numbers) == len(set(page_numbers))
    assert len(slide_numbers) == len(set(slide_numbers))
    assert len(mapped_markers) == len(set(mapped_markers))
    assert set(mapped_markers) == COURSE_MARKERS
    assert not set(mapped_markers) & LITERATURE_MARKERS
    assert set(mapped_markers) == set(_source_markers(COURSE_SOURCE))


def test_course_fixture_omits_hit_treatment_recommendations() -> None:
    course = COURSE_SOURCE.read_text(encoding="utf-8").lower()
    for forbidden in (
        "argatroban",
        "bivalirudin",
        "fondaparinux",
        "direct thrombin inhibitor",
        "discontinue heparin",
        "stop heparin",
    ):
        assert forbidden not in course


def test_course_and_literature_have_intentional_timing_discrepancy() -> None:
    course = COURSE_SOURCE.read_text(encoding="utf-8").lower()
    literature = LITERATURE_SOURCE.read_text(encoding="utf-8").lower()
    assert "5 to 10 days" in course
    assert "within 24 hours" in literature
    assert "recent prior exposure" in literature


def test_fixtures_are_synthetic_and_private_source_free() -> None:
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file()
    )
    for forbidden in (
        "/users/",
        "connor",
        "conbro13",
        "lmu",
        "dcom",
        "notebooklm-storage",
        "collection.anki2",
        "api_key",
        "authorization: bearer",
        "@gmail.com",
    ):
        assert forbidden not in fixture_text

    literature = LITERATURE_SOURCE.read_text(encoding="utf-8").lower()
    for forbidden in (
        "http://",
        "https://",
        "doi:",
        "pmid:",
        "pubmed",
        "journal of",
    ):
        assert forbidden not in literature
    readme = README.read_text(encoding="utf-8").lower()
    for required in (
        "original synthetic test prose",
        "no private course material",
        "must never be presented as a real lecture or publication",
        "markers are fixture anchors, not production evidence ids",
        "hit treatment is intentionally absent",
        "rapid-onset hit exception",
        "separately labeled",
        "tests must fail if evidence markers become duplicated or orphaned",
        "fixture changes require integrity-test and handoff review",
        "production code must not import tests/builders",
        "no real doi, pmid, author, institution, journal, or source url",
    ):
        assert required in readme


def test_fixture_encoding_line_endings_and_json_format_are_deterministic() -> None:
    for path in REQUIRED_FILES:
        raw = path.read_bytes()
        raw.decode("utf-8")
        assert b"\r" not in raw
        assert not re.search(rb"[A-Za-z]:[\\/]", raw)
        assert not re.search(rb"(?im)^\s*[\"']?(?:date|created_at|updated_at)[\"']?\s*[:=]", raw)

    json_bytes = COURSE_PAGE_MAP.read_bytes()
    assert json_bytes.endswith(b"\n")
    assert not json_bytes.endswith(b"\n\n")
    assert json.loads(json_bytes.decode("utf-8"))


def test_build_source_revision_is_deterministic_and_independent() -> None:
    first = build_source_revision()
    second = build_source_revision()
    assert first == {
        "source_document_id": "source_fixture_course_l13",
        "source_revision_id": "sr_fixture_course_l13_v1",
        "file_sha256": "0" * 64,
        "state": "ready",
    }
    assert first == second
    assert first is not second
    assert len(first["file_sha256"]) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", first["file_sha256"])
    first["state"] = "changed"
    assert second["state"] == "ready"
    assert set(second) == {"source_document_id", "source_revision_id", "file_sha256", "state"}

    overridden = build_source_revision(
        source_document_id="doc",
        source_revision_id="revision",
        file_sha256="checksum",
        state="draft",
    )
    assert overridden == {
        "source_document_id": "doc",
        "source_revision_id": "revision",
        "file_sha256": "checksum",
        "state": "draft",
    }


def test_build_evidence_ref_defaults_and_checksum_overrides() -> None:
    default = build_evidence_ref()
    excerpt = "Hemophilia A is factor VIII deficiency with prolonged PTT and normal PT."
    assert isinstance(default, EvidenceRef)
    assert default == EvidenceRef(
        "ev_fixture_course_l13_hemophilia_a",
        "sr_fixture_course_l13_v1",
        AuthorityClass.COURSE_MATERIAL,
        "slide",
        "5",
        excerpt,
        "sha256:" + hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    )
    assert build_evidence_ref() == default
    assert build_evidence_ref(checksum="").checksum == ""
    assert build_evidence_ref(checksum="provided").checksum == "provided"


def test_build_retrieval_scope_copies_and_preserves_iterables() -> None:
    lecture_ids = ["l13", "l2", "l13"]
    source_revision_ids = ["sr_b", "sr_a", "sr_b"]
    scope = build_retrieval_scope(
        lecture_ids=lecture_ids,
        source_revision_ids=source_revision_ids,
    )
    assert isinstance(scope, RetrievalScope)
    assert scope == RetrievalScope(
        "heme",
        "e2",
        ("l13", "l2", "l13"),
        TruthMode.COURSE_ONLY,
        ("sr_b", "sr_a", "sr_b"),
    )
    assert scope.course_id == "heme"
    assert scope.exam_id == "e2"
    assert scope.lecture_ids == ("l13", "l2", "l13")
    assert scope.truth_mode is TruthMode.COURSE_ONLY
    assert scope.source_revision_ids == ("sr_b", "sr_a", "sr_b")
    lecture_ids.append("later")
    source_revision_ids.clear()
    assert scope.lecture_ids == ("l13", "l2", "l13")
    assert scope.source_revision_ids == ("sr_b", "sr_a", "sr_b")
    assert build_retrieval_scope() == RetrievalScope(
        "heme", "e2", ("l13",), TruthMode.COURSE_ONLY, ()
    )


def test_build_board_question_draft_defaults_are_deeply_independent() -> None:
    first = build_board_question_draft()
    second = build_board_question_draft()
    assert first == second
    assert first is not second
    assert set(first) == {
        "stem",
        "lead_in",
        "options",
        "correct_option_id",
        "objective_ids",
        "difficulty",
        "blueprint_tags",
        "claims",
    }
    assert len(first["options"]) == 4
    assert first["correct_option_id"] == "B"
    assert first["objective_ids"] == ["obj_fixture_hemophilia_a"]
    assert first["difficulty"] == 3
    assert first["blueprint_tags"] == ["clinical_presentation:bleeding_disorder"]
    assert first["stem"] == (
        "A child has recurrent hemarthroses. Laboratory testing shows a normal PT and a "
        "prolonged PTT."
    )
    assert first["lead_in"] == "Which coagulation-factor deficiency best explains these findings?"
    assert all(
        option["text"] and option["rationale"] and option["evidence_ids"]
        for option in first["options"]
    )
    assert {claim["role"] for claim in first["claims"]} == {
        "stem",
        "correct_support",
        "teaching_point",
    }
    assert len({claim["claim_id"] for claim in first["claims"]}) == len(first["claims"])
    assert all(claim["text"] and claim["evidence_ids"] for claim in first["claims"])

    assert first["options"] is not second["options"]
    assert first["claims"] is not second["claims"]
    for index, option in enumerate(first["options"]):
        assert option is not second["options"][index]
        assert all(
            option["evidence_ids"] is not sibling["evidence_ids"]
            for sibling_index, sibling in enumerate(first["options"])
            if sibling_index != index
        )
        assert option["evidence_ids"] is not second["options"][index]["evidence_ids"]
    for index, claim in enumerate(first["claims"]):
        assert claim is not second["claims"][index]
        assert all(
            claim["evidence_ids"] is not sibling["evidence_ids"]
            for sibling_index, sibling in enumerate(first["claims"])
            if sibling_index != index
        )
        assert claim["evidence_ids"] is not second["claims"][index]["evidence_ids"]

    first["blueprint_tags"].append("mutated-blueprint")
    first["objective_ids"].append("mutated")
    first["options"][0]["text"] = "mutated-option"
    first["claims"][0]["text"] = "mutated-claim"
    assert "mutated-blueprint" not in second["blueprint_tags"]
    assert "mutated" not in second["objective_ids"]
    assert second["options"][0]["text"] != "mutated-option"
    assert first["options"][1]["text"] != "mutated-option"
    assert second["claims"][0]["text"] != "mutated-claim"
    assert first["claims"][1]["text"] != "mutated-claim"

    for index, option in enumerate(first["options"]):
        marker = f"mutated-option-evidence-{index}"
        option["evidence_ids"].append(marker)
        assert marker not in second["options"][index]["evidence_ids"]
        assert all(
            marker not in sibling["evidence_ids"]
            for sibling_index, sibling in enumerate(first["options"])
            if sibling_index != index
        )
    for index, claim in enumerate(first["claims"]):
        marker = f"mutated-claim-evidence-{index}"
        claim["evidence_ids"].append(marker)
        assert marker not in second["claims"][index]["evidence_ids"]
        assert all(
            marker not in sibling["evidence_ids"]
            for sibling_index, sibling in enumerate(first["claims"])
            if sibling_index != index
        )


@pytest.mark.parametrize("option_count", (0, 3, 5))
def test_build_board_question_draft_supports_deterministic_option_counts(option_count: int) -> None:
    payload = build_board_question_draft(option_count=option_count)
    assert len(payload["options"]) == option_count


def test_build_board_question_draft_preserves_invalid_controls() -> None:
    invalid = build_board_question_draft(correct_option_id="Z")
    assert invalid["correct_option_id"] == "Z"
    duplicate = build_board_question_draft(option_count=2, duplicate_option_id=True)
    assert duplicate["options"][0]["option_id"] == duplicate["options"][-1]["option_id"]
    with pytest.raises(ValueError, match="negative"):
        build_board_question_draft(option_count=-1)
    with pytest.raises(ValueError, match="6.*5|5.*6"):
        build_board_question_draft(option_count=6)
    with pytest.raises(ValueError, match="duplicate"):
        build_board_question_draft(option_count=1, duplicate_option_id=True)
    with pytest.raises(ValueError, match="duplicate"):
        build_board_question_draft(option_count=0, duplicate_option_id=True)


def test_build_board_question_draft_defensively_copies_iterables() -> None:
    objective_ids = ["objective"]
    blueprint_tags = ["tag"]
    evidence_ids = ["evidence"]
    payload = build_board_question_draft(
        objective_ids=objective_ids,
        blueprint_tags=blueprint_tags,
        evidence_ids=evidence_ids,
    )
    objective_ids.append("later")
    blueprint_tags.append("later")
    evidence_ids.append("later")
    assert payload["objective_ids"] == ["objective"]
    assert payload["blueprint_tags"] == ["tag"]
    assert all(option["evidence_ids"] == ["evidence"] for option in payload["options"])
    assert all(claim["evidence_ids"] == ["evidence"] for claim in payload["claims"])


def test_builders_have_no_forbidden_nondeterminism() -> None:
    forbidden_names = {
        "uuid",
        "random",
        "secrets",
        "datetime",
        "time",
        "os",
        "socket",
    }
    for path in BUILDER_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in forbidden_names for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden_names
            if isinstance(node, ast.Name):
                assert node.id not in forbidden_names
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"now", "utcnow", "time", "gethostname"}


def test_production_does_not_import_test_builders_or_fixtures() -> None:
    production_root = Path(__file__).resolve().parents[2] / "src" / "oms_hub"
    for path in production_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            assert all(not module.startswith("tests.builders") for module in modules)
            assert all("grounded_learning" not in module for module in modules)
