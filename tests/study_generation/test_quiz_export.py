import hashlib
import json
from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.quiz_export import export_reviewed_quiz
from oms_hub.study_generation.quiz_images import sanitize_quiz_image

MEDICAL_TEXT = "Na⁺ K⁺ Ca²⁺ H₂O → α-synuclein β ≥ 2 – reassess"


def test_medical_superscripts_subscripts_survive_all_pdf_text_styles(tmp_path):
    quiz, images, provenance = export_fixture(tmp_path)
    first = quiz.questions[0]
    quiz = replace(
        quiz,
        title=MEDICAL_TEXT,
        questions=(
            replace(
                first,
                stem=MEDICAL_TEXT,
                rationale=MEDICAL_TEXT,
                choices=(replace(first.choices[0], text=MEDICAL_TEXT), first.choices[1]),
                learning_objective=MEDICAL_TEXT,
                image_ref=replace(first.image_ref, description=MEDICAL_TEXT),
            ),
            quiz.questions[1],
        ),
    )
    provenance["questions"]["q1"]["source_refs"][0]["locator"] = MEDICAL_TEXT
    raw, _, pdf = export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    assert parse_native_quiz(raw.read_text()) == quiz
    pages = PdfReader(pdf).pages
    text = "\n".join(page.extract_text() for page in pages)
    assert text.count(MEDICAL_TEXT) >= 7
    assert "■" not in text and "�" not in text
    embedded = {
        str(font.get_object()["/BaseFont"]): font.get_object()
        for page in pages
        for font in page["/Resources"]["/Font"].values()
        if "DejaVuSans" in str(font.get_object().get("/BaseFont", ""))
    }
    assert len(embedded) == 2
    assert all(font["/FontDescriptor"]["/FontFile2"].get_data() for font in embedded.values())


@pytest.mark.parametrize("field", ["title", "stem", "rationale", "source"])
def test_unsupported_pdf_character_is_rejected_before_writing(tmp_path, field):
    quiz, images, provenance = export_fixture(tmp_path)
    unsupported = "Unsupported character: \u0378"
    if field == "source":
        provenance["questions"]["q1"]["source_refs"][0]["locator"] = unsupported
    elif field == "title":
        quiz = replace(quiz, title=unsupported)
    else:
        quiz = replace(
            quiz, questions=(replace(quiz.questions[0], **{field: unsupported}), quiz.questions[1])
        )
    with pytest.raises(ValueError, match="U\\+0378"):
        export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    assert not (tmp_path / "exports").exists()


def test_renderer_identity_changes_root_and_preserves_previous_exports(tmp_path, monkeypatch):
    from oms_hub.study_generation import quiz_export

    quiz, images, provenance = export_fixture(tmp_path)
    original = export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    original_bytes = [path.read_bytes() for path in original]
    with ZipFile(original[1]) as archive:
        identity = json.loads(archive.read("manifest.json"))["pdf_renderer"]
    fonts = Path(quiz_export.__file__).parent / "assets" / "quiz_fonts"
    assert identity["font_sha256"] == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in fonts.glob("*.ttf")
    }
    monkeypatch.setattr(quiz_export, "_PDF_RENDERER_VERSION", identity["version"] + 1)
    updated = export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    assert [p.name for p in original] == [p.name for p in updated]
    assert original[0].parent != updated[0].parent
    assert [path.read_bytes() for path in original] == original_bytes


def export_fixture(tmp_path, *, long=False):
    images = {}
    for key, size in (("portrait", (300, 900)), ("wide", (1400, 300))):
        path = tmp_path / f"{key}.png"
        image = Image.new("RGB", size, "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((4, 4, size[0] - 5, size[1] - 5), outline="#94a3b8", width=4)
        draw.ellipse((40, 40, 220, 220), outline="#2563eb", width=16)
        center = (size[0] - 130, size[1] - 130)
        draw.ellipse(
            (center[0] - 45, center[1] - 45, center[0] + 45, center[1] + 45), fill="#f97316"
        )
        image.save(path)
        path.write_bytes(sanitize_quiz_image(path.read_bytes()).payload)
        images[key] = path
    quiz = parse_native_quiz(
        json.dumps(
            {
                "title": "Reviewed structures < & >",
                "questions": [
                    {
                        "stem": f"Case {index}: β ≥ 2 → reassess – select the matching probe. "
                        + (
                            "The sample contains a large ring and a smaller disk. " * 45
                            if long
                            else ""
                        ),
                        "choices": ["Probe A", "Probe B"],
                        "correct_index": 0,
                        "rationale": (
                            f"Explanation {index}: Probe A binds the ring; Probe B binds the disk. "
                        )
                        + (
                            "The original diagram supports the stated relationship. " * 55
                            if long
                            else ""
                        ),
                        "learning_objective": "lo-1: Match shape and probe.",
                        "image_ref": {
                            "key": key,
                            "source_title": "Synthetic slides",
                            "locator": f"Slide {index}",
                            "description": f"{key} source figure",
                        },
                    }
                    for index, key in enumerate(images, 1)
                ],
            }
        )
    )
    provenance = {
        "questions": {
            q.id: {
                "objective_ids": ["lo-1"],
                "source_refs": [
                    {
                        "source_id": "source-1",
                        "segment_key": f"slide-{index}",
                        "locator": f"Slide {index}",
                    }
                ],
            }
            for index, q in enumerate(quiz.questions, 1)
        },
        "image_sha256": {
            key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in images.items()
        },
    }
    return quiz, images, provenance


def test_export_reopens_same_reviewed_payload_images_provenance_and_explanations(tmp_path):
    quiz, images, provenance = export_fixture(tmp_path)
    paths = export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    raw, bundle, pdf = paths
    assert parse_native_quiz(raw.read_text()) == quiz
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    assert all(digest in path.name for path in paths)
    with ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("quiz.json") == raw.read_bytes()
        assert manifest["accepted_payload_sha256"] == digest
        assert manifest["provenance"] == provenance
        for key, path in images.items():
            assert archive.read(manifest["images"][key]["path"]) == path.read_bytes()
            assert manifest["images"][key]["sha256"] == provenance["image_sha256"][key]
        assert manifest["questions"][0]["image_key"] == "portrait"
    reader = PdfReader(pdf)
    pages = [page.extract_text() for page in reader.pages]
    text = "\n".join(pages)
    assert digest in text
    for question in quiz.questions:
        assert question.stem in text
        assert question.rationale in text
    assert "Explanation" not in pages[0]
    assert "lo-1" in text and "source-1" in text and "Slide 1" in text
    assert "Reviewed structures < & >" in text
    assert "β ≥ 2 → reassess –" in text
    assert export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports") == paths
    changed = replace(
        quiz, questions=(replace(quiz.questions[0], stem="Edited stem"), quiz.questions[1])
    )
    assert export_reviewed_quiz(changed, images, provenance, tmp_path / "exports") != paths


@pytest.mark.parametrize(
    "failure",
    [
        "absent",
        "changed",
        "bad_png",
        "unknown_image",
        "missing_question",
        "empty_source",
        "unsafe_key",
    ],
)
def test_export_rejects_incomplete_or_mismatched_evidence_before_writing(tmp_path, failure):
    quiz, images, provenance = export_fixture(tmp_path)
    if failure == "absent":
        images["portrait"].unlink()
    elif failure == "changed":
        images["portrait"].write_bytes(b"changed")
    elif failure == "bad_png":
        images["portrait"].write_bytes(b"not an image")
        provenance["image_sha256"]["portrait"] = hashlib.sha256(b"not an image").hexdigest()
    elif failure == "unknown_image":
        images["unreferenced"] = images["portrait"]
    elif failure == "missing_question":
        provenance["questions"].pop("q1")
    elif failure == "empty_source":
        provenance["questions"]["q1"]["source_refs"] = []
    else:
        quiz = replace(
            quiz,
            questions=(
                replace(
                    quiz.questions[0],
                    image_ref=replace(quiz.questions[0].image_ref, key="../../escape"),
                ),
                quiz.questions[1],
            ),
        )
    with pytest.raises((ValueError, OSError)):
        export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    assert not (tmp_path / "exports").exists()


def test_long_pdf_has_multiple_answer_pages_and_original_portrait_and_wide_images(tmp_path):
    quiz, images, provenance = export_fixture(tmp_path, long=True)
    _, _, pdf = export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    pages = PdfReader(pdf).pages
    text = "\n".join(page.extract_text() for page in pages)
    assert len(pages) >= 5
    assert text.count("Explanation") == 2
    assert sum(len(page.images) for page in pages) == 2
    assert "Answers and explanations" in text
    assert "Sources and objectives" in text


def test_provenance_change_gets_new_bundle_and_corrupted_existing_export_is_rejected(tmp_path):
    quiz, images, provenance = export_fixture(tmp_path)
    original = export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    provenance["questions"]["q1"]["source_refs"][0]["locator"] = "Slide 1 figure A"
    updated = export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    assert original[0].name == updated[0].name
    assert original[0].parent != updated[0].parent
    updated[2].write_bytes(b"incomplete PDF")
    with pytest.raises(OSError, match="immutable destination"):
        export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")


def test_matching_question_exports_answers_only_in_answer_section(tmp_path):
    quiz = parse_native_quiz(
        json.dumps(
            {
                "title": "Matching",
                "questions": [
                    {
                        "kind": "matching",
                        "stem": "Match each shape to a probe.",
                        "prompts": [
                            {"label": "1", "text": "Ring", "correct_index": 0},
                            {"label": "2", "text": "Disk", "correct_index": 1},
                        ],
                        "choices": ["Probe A", "Probe B"],
                        "rationale": "Each probe binds its own shape.",
                    }
                ],
            }
        )
    )
    provenance = {
        "questions": {
            "q1": {
                "objective_ids": ["lo-1"],
                "source_refs": [
                    {
                        "source_id": "source-1",
                        "segment_key": "slide-1",
                        "locator": "Slide 1",
                    }
                ],
            }
        },
        "image_sha256": {},
    }
    raw, _, pdf = export_reviewed_quiz(quiz, {}, provenance, tmp_path / "exports")
    assert parse_native_quiz(raw.read_text()) == quiz
    pages = [p.extract_text() for p in PdfReader(pdf).pages]
    assert "1: A. Probe A" not in pages[0]
    assert "1: A. Probe A" in pages[1] and "2: B. Probe B" in pages[1]


@pytest.mark.parametrize("target", ["nested_root", "output_file"])
def test_export_rejects_indirection_and_preserves_existing_bytes(tmp_path, target):
    quiz, images, provenance = export_fixture(tmp_path)
    paths = export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    outside = tmp_path / "existing"
    if target == "nested_root":
        paths[0].parent.rename(outside)
        paths[0].parent.symlink_to(outside, target_is_directory=True)
        original = {path.name: path.read_bytes() for path in outside.iterdir()}
    else:
        outside.write_bytes(b"preserved outside bytes")
        paths[2].unlink()
        paths[2].symlink_to(outside)
        original = outside.read_bytes()
    with pytest.raises(ValueError, match="indirection"):
        export_reviewed_quiz(quiz, images, provenance, tmp_path / "exports")
    if target == "nested_root":
        assert {path.name: path.read_bytes() for path in outside.iterdir()} == original
    else:
        assert outside.read_bytes() == original
