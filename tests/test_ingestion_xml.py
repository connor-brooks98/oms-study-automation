from zipfile import ZipFile

from oms_hub.ingestion.service import IngestionService


def test_pptx_evidence_rejects_xml_entities(tmp_path):
    archive_path = tmp_path / "hostile.pptx"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            """<!DOCTYPE root [
            <!ENTITY payload "expanded text">
            ]><root>&payload;</root>""",
        )

    service = object.__new__(IngestionService)
    with ZipFile(archive_path) as archive:
        assert service._xml_text(archive, "ppt/slides/slide1.xml") == ""
