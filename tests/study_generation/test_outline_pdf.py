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


def test_outline_renderer_formats_notebook_markdown_without_literal_markers():
    payload = OutlinePdfRenderer().render(
        "Neuro Outline",
        """# Neurodegeneration

**Core concept:** protein aggregation

- Alzheimer disease
  - **Amyloid-beta** plaques
1. Identify the syndrome

***

Use `MRI` when indicated.
""",
    )

    reader = PdfReader(BytesIO(payload))
    extracted = "\n".join(page.extract_text() for page in reader.pages)

    assert len(reader.pages) == 1
    assert "Core concept:" in extracted
    assert "Amyloid-beta" in extracted
    assert "MRI" in extracted
    assert "**" not in extracted
    assert "***" not in extracted
    assert "`" not in extracted
