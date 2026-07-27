from io import BytesIO

from pypdf import PdfReader

from oms_hub.study_generation.outline import OutlinePdfRenderer


def test_outline_renderer_creates_valid_single_pdf():
    payload = OutlinePdfRenderer().render(
        "Neuro - Lecture 01 - Seizures - Lecture Outline",
        "# Objectives\n- Localize seizure onset\n\n## Pearl\nTreat status quickly.",
    )

    reader = PdfReader(BytesIO(payload))

    assert len(reader.pages) >= 1
    assert reader.pages[0].extract_text().startswith("Neuro")


def test_outline_renderer_rejects_empty_content():
    try:
        OutlinePdfRenderer().render("Neuro Outline", " \n ")
    except ValueError as error:
        assert "empty" in str(error)
    else:
        raise AssertionError("expected empty outline to be rejected")
