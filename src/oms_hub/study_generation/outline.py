from html import escape
from io import BytesIO
from typing import Any

from pypdf import PdfReader
from reportlab.lib.enums import TA_CENTER  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.lib.styles import (  # type: ignore[import-untyped]
    ParagraphStyle,
    getSampleStyleSheet,
)
from reportlab.lib.units import inch  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from oms_hub.config import Settings
from oms_hub.domain import LectureKey
from oms_hub.files.atomic import verified_atomic_copy, verified_atomic_write
from oms_hub.routing import build_outline_destination, expanded_path
from oms_hub.study_generation.domain import (
    GenerationJob,
    NotebookAnswer,
    OutlineRecord,
)
from oms_hub.study_generation.repository import GenerationRepository


class OutlinePdfRenderer:
    def render(self, title: str, content: str) -> bytes:
        if not title.strip() or not content.strip():
            raise ValueError("outline title and content cannot be empty")
        buffer = BytesIO()
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "OutlineTitle",
            parent=styles["Title"],
            alignment=TA_CENTER,
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=20,
            spaceAfter=18,
        )
        body_style = ParagraphStyle(
            "OutlineBody",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=15,
            spaceAfter=7,
        )
        heading_styles = {
            1: ParagraphStyle(
                "OutlineH1",
                parent=styles["Heading1"],
                fontName="Helvetica-Bold",
                fontSize=14,
                leading=18,
                spaceBefore=10,
                spaceAfter=7,
            ),
            2: ParagraphStyle(
                "OutlineH2",
                parent=styles["Heading2"],
                fontName="Helvetica-Bold",
                fontSize=12,
                leading=16,
                spaceBefore=8,
                spaceAfter=5,
            ),
            3: ParagraphStyle(
                "OutlineH3",
                parent=styles["Heading3"],
                fontName="Helvetica-Bold",
                fontSize=10.5,
                leading=14,
                spaceBefore=6,
                spaceAfter=4,
            ),
        }
        story: list[object] = [Paragraph(escape(title.strip()), title_style)]
        bullets: list[ListItem] = []

        def flush_bullets() -> None:
            if bullets:
                story.append(
                    ListFlowable(
                        list(bullets),
                        bulletType="bullet",
                        leftIndent=20,
                        bulletFontName="Helvetica",
                    )
                )
                story.append(Spacer(1, 5))
                bullets.clear()

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                flush_bullets()
                continue
            if line in {"---", "***"}:
                flush_bullets()
                story.append(PageBreak())
                continue
            heading_level = len(line) - len(line.lstrip("#"))
            if heading_level and heading_level <= 3 and line[heading_level:].strip():
                flush_bullets()
                story.append(
                    Paragraph(
                        escape(line[heading_level:].strip()),
                        heading_styles[heading_level],
                    )
                )
            elif line.startswith(("- ", "* ")):
                bullets.append(
                    ListItem(
                        Paragraph(escape(line[2:].strip()), body_style),
                        leftIndent=8,
                    )
                )
            else:
                flush_bullets()
                story.append(Paragraph(escape(line), body_style))
        flush_bullets()

        document = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            topMargin=0.7 * inch,
            bottomMargin=0.7 * inch,
            title=title,
        )
        document.build(story, onLaterPages=_page_number)
        payload = buffer.getvalue()
        if not payload or not PdfReader(BytesIO(payload)).pages:
            raise RuntimeError("outline PDF could not be validated")
        return payload


class OutlineService:
    def __init__(
        self,
        settings: Settings,
        repository: GenerationRepository,
        renderer: OutlinePdfRenderer | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.renderer = renderer or OutlinePdfRenderer()

    def file(
        self,
        job: GenerationJob,
        lecture: LectureKey,
        answer: NotebookAnswer,
    ) -> OutlineRecord:
        title = (
            f"{lecture.subject} - Lecture {lecture.lecture_number:02d} - "
            f"{lecture.topic} - Lecture Outline"
        )
        payload = self.renderer.render(title, answer.text)
        immutable = (
            expanded_path(self.settings.data_dir)
            / "artifacts"
            / "generation"
            / job.id
            / "outline.pdf"
        )
        digest = verified_atomic_write(payload, immutable)
        destination = build_outline_destination(self.settings, lecture)
        verified_atomic_copy(immutable, destination)
        return self.repository.record_outline(
            job.lecture_id,
            job.id,
            destination,
            digest,
        )


def _page_number(canvas: Any, document: Any) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(7.75 * inch, 0.4 * inch, f"Page {document.page}")
    canvas.restoreState()
