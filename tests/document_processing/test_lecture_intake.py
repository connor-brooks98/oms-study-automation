import hashlib
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from oms_hub.document_processing.lecture_intake import (
    SUPPORTED_LECTURE_SUFFIXES,
    parse_lecture_source,
    render_reading_pdf,
)
from oms_hub.ingestion.domain import UploadKind, UploadManifestSlot
from oms_hub.ingestion.staging import StagingService, UploadRejected


def source_file(root: Path, suffix: str) -> Path:
    path = root / f"lecture{suffix}"
    if suffix == ".docx":
        with ZipFile(path, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="png" ContentType="image/png"/>'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
            )
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/></Relationships>',
            )
            archive.writestr(
                "word/document.xml",
                "<w:document "
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
                "<w:body><w:p><w:r><w:t>The blue ring represents probe A.</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>The orange disk represents probe B.</w:t></w:r></w:p>"
                '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="952500" cy="381000"/>'
                '<wp:docPr id="1" name="Blue diagram"/><a:graphic><a:graphicData>'
                '<a:blip r:embed="image1"/></a:graphicData></a:graphic></wp:inline>'
                "</w:drawing></w:r></w:p></w:body></w:document>",
            )
            archive.writestr(
                "word/_rels/document.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="image1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/image" '
                'Target="media/image1.png"/></Relationships>',
            )
            image = BytesIO()
            Image.new("RGB", (100, 40), "blue").save(image, format="PNG")
            archive.writestr("word/media/image1.png", image.getvalue())
    elif suffix == ".pdf":
        canvas = Canvas(str(path))
        canvas.drawString(40, 750, "The blue ring represents probe A.")
        canvas.save()
    elif suffix == ".rtf":
        path.write_bytes(
            rb"{\rtf1\ansi The blue ring represents probe A.\par "
            rb"The orange disk represents probe B.}"
        )
    elif suffix == ".pptx":
        from tests.document_processing.pptx_factory import SlideFixture, build_pptx

        build_pptx(
            path, slides=(SlideFixture("Probe A", "The blue ring represents probe A.", image=True),)
        )
    else:
        path.write_text("The blue ring represents probe A.\n\nThe orange disk represents probe B.")
    return path


@pytest.mark.parametrize("kind", list(UploadKind))
@pytest.mark.parametrize("suffix", SUPPORTED_LECTURE_SUFFIXES)
def test_real_formats_stage_and_extract_with_original_identity(tmp_path, kind, suffix):
    path = source_file(tmp_path, suffix)
    staging = StagingService(tmp_path / "staging", 2_000_000, 4_000_000)
    batch = staging.begin_batch(kind)
    staged = staging.stage_file(batch, path.name, BytesIO(path.read_bytes()))
    assert staged.path.read_bytes() == path.read_bytes()
    assert staged.original_filename == path.name
    parsed = parse_lecture_source(path, tmp_path / "assets")
    assert "blue ring" in " ".join(s.text for s in parsed.segments)
    assert parsed.source_sha256 == staged.sha256
    assert all(segment.locator.label for segment in parsed.segments)


@pytest.mark.parametrize(
    "suffix,payload",
    [
        (".pdf", b"%PDF-1.7 broken"),
        (".docx", b"a renamed text file"),
        (".pptx", b"a renamed text file"),
        (".rtf", rb"{\rtf1 unclosed"),
        (".txt", b"binary\x00payload"),
        (".md", b"\xff\xfe invalid UTF-8"),
        (".doc", b"legacy document"),
    ],
)
def test_malformed_and_unsupported_formats_do_not_stage(tmp_path, suffix, payload):
    staging = StagingService(tmp_path / "staging", 10000, 20000)
    with pytest.raises(UploadRejected):
        staging.stage_file(
            staging.begin_batch(UploadKind.SLIDES), "lecture" + suffix, BytesIO(payload)
        )
    assert not list(staging.root.rglob("*.ready"))


def test_reading_pdf_is_labeled_and_keeps_source_text(tmp_path):
    from pypdf import PdfReader

    path = source_file(tmp_path, ".docx")
    document = parse_lecture_source(path, tmp_path / "assets")
    output = tmp_path / "reading.pdf"
    render_reading_pdf(document, output)
    text = "\n".join(page.extract_text() for page in PdfReader(output).pages)
    assert "Reflowed reading copy" in text
    assert "blue ring" in text
    assert "block 1" in text
    assert document.assets[0].path.is_file()
    assert any(segment.asset_keys for segment in document.segments)
    assert sum(len(page.images) for page in PdfReader(output).pages) == 1


@pytest.mark.parametrize("chunks", [False, True])
def test_word_manifest_validation_preserves_bytes_and_cancellation(tmp_path, chunks):
    source = source_file(tmp_path, ".docx")
    payload = source.read_bytes()
    staging = StagingService(tmp_path / "stage", 2_000_000, 4_000_000)
    slot = UploadManifestSlot(
        str(uuid4()), source.name, len(payload), hashlib.sha256(payload).hexdigest()
    )
    manifest = staging.begin_manifest(UploadKind.TRANSCRIPTS, [slot])
    if chunks:
        session = staging.begin_manifest_chunks(manifest.id, slot.id)
        midpoint = len(payload) // 2
        staging.append_chunk(session.id, 0, BytesIO(payload[:midpoint]))
        staging.append_chunk(session.id, midpoint, BytesIO(payload[midpoint:]))
        staging.finalize_chunks(session.id)
    else:
        staging.stage_manifest_file(manifest.id, slot.id, BytesIO(payload))
    staged = staging.manifest_uploads(manifest.id)
    assert staged[0].path.read_bytes() == payload
    staging.discard_manifest(manifest.id)
    assert not staged[0].path.exists()
    assert not list(staging.root.rglob("*.part"))


def test_wrong_office_type_and_encrypted_pdf_rejected(tmp_path):
    from pypdf import PdfWriter

    staging = StagingService(tmp_path / "stage", 2_000_000, 4_000_000)
    source = source_file(tmp_path, ".docx")
    with pytest.raises(UploadRejected):
        staging.stage_file(
            staging.begin_batch(UploadKind.SLIDES), "renamed.pptx", BytesIO(source.read_bytes())
        )
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt("password-required")
    encrypted = BytesIO()
    writer.write(encrypted)
    with pytest.raises(UploadRejected, match="PDF"):
        staging.stage_file(
            staging.begin_batch(UploadKind.SLIDES), "locked.pdf", BytesIO(encrypted.getvalue())
        )
