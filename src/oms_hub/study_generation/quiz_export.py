"""Export an already verified native quiz; authorization stays with the caller."""

from __future__ import annotations

import hashlib
import json
import sys
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pypdf import PdfReader
from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from oms_hub.files.atomic import verified_atomic_write
from oms_hub.files.trusted_paths import prepare_trusted_directory, trusted_managed_path
from oms_hub.study_generation.domain import NativeQuiz, QuizMatchingQuestion
from oms_hub.study_generation.native_quiz import parse_native_quiz, serialize_native_quiz
from oms_hub.study_generation.quiz_images import (
    MAX_QUIZ_IMAGE_BYTES,
    SanitizedQuizImage,
    sanitize_quiz_image,
)

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
_Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_PDF_REGULAR = "OMSQuizDejaVuSans"
_PDF_BOLD = "OMSQuizDejaVuSansBold"
_PDF_RENDERER_VERSION = 2


@lru_cache(maxsize=1)
def _register_pdf_fonts() -> dict[str, str]:
    root = Path(__file__).parent / "assets" / "quiz_fonts"
    hashes = {}
    for name, filename in ((_PDF_REGULAR, "DejaVuSans.ttf"), (_PDF_BOLD, "DejaVuSans-Bold.ttf")):
        payload = (root / filename).read_bytes()
        hashes[filename] = hashlib.sha256(payload).hexdigest()
        pdfmetrics.registerFont(TTFont(name, BytesIO(payload)))
    return hashes


class _SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: _Text
    segment_key: _Text
    locator: _Text


class _QuestionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    objective_ids: list[_Text] = Field(min_length=1, max_length=200)
    source_refs: list[_SourceRef] = Field(min_length=1, max_length=200)


class _Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    questions: dict[str, _QuestionEvidence]
    image_sha256: dict[str, _Sha256]


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def export_reviewed_quiz(
    quiz: NativeQuiz,
    image_paths: dict[str, Path],
    provenance: dict[str, object],
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    """Return JSON, ZIP, PDF bound to the same reviewed payload and source evidence.

    Callers must obtain the quiz and this exact question/source/image mapping from
    the authoritative review service, then recheck freshness before serving files.
    A native JSON file stays directly importable; its SHA is in all filenames.
    """
    raw = serialize_native_quiz(quiz).encode()
    if parse_native_quiz(raw.decode()) != quiz:
        raise ValueError("native quiz identifiers must survive serialization")
    evidence = _Provenance.model_validate(provenance)
    keys = {q.image_ref.key for q in quiz.questions if q.image_ref}
    if set(evidence.questions) != {q.id for q in quiz.questions}:
        raise ValueError("export requires evidence for every reviewed question")
    if keys != set(image_paths) or keys != set(evidence.image_sha256):
        raise ValueError("export image mappings must match referenced images exactly")
    images: dict[str, SanitizedQuizImage] = {}
    for key in sorted(keys):
        with image_paths[key].open("rb") as stream:
            payload = stream.read(MAX_QUIZ_IMAGE_BYTES + 1)
        sanitized = sanitize_quiz_image(payload)
        if sanitized.payload != payload or sanitized.sha256 != evidence.image_sha256[key]:
            raise ValueError("export image is not the verified sanitized PNG")
        images[key] = sanitized
    digest = hashlib.sha256(raw).hexdigest()
    manifest = {
        "format_version": 2,
        "pdf_renderer": {"version": _PDF_RENDERER_VERSION, "font_sha256": _register_pdf_fonts()},
        "accepted_payload_sha256": digest,
        "provenance": evidence.model_dump(mode="json"),
        "questions": [
            {"id": q.id, "image_key": q.image_ref.key if q.image_ref else None}
            for q in quiz.questions
        ],
        "images": {
            key: {"path": f"media/{key}.png", "sha256": value.sha256}
            for key, value in images.items()
        },
    }
    manifest_bytes = _json_bytes(manifest)
    # Windows may retain MAX_PATH even with a modern Python. Keep full hashes
    # and use its extended absolute spelling for validation, writes and reads.
    if sys.platform == "win32" and output_dir.is_absolute():
        spelling = str(output_dir)
        if not spelling.startswith("\\\\?\\"):
            output_dir = Path(
                "\\\\?\\UNC\\" + spelling[2:]
                if spelling.startswith("\\\\")
                else "\\\\?\\" + spelling
            )
    root = output_dir / hashlib.sha256(manifest_bytes).hexdigest()
    paths = tuple(root / f"quiz-{digest}.{suffix}" for suffix in ("json", "zip", "pdf"))
    pdf = _render_pdf(quiz, images, evidence, digest)
    # ponytail: bundle bytes stay in memory; stream ZIPs if real lecture size requires it.
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in [
            ("quiz.json", raw),
            ("manifest.json", manifest_bytes),
            *((f"media/{key}.png", value.payload) for key, value in images.items()),
        ]:
            info = ZipInfo(name)  # Fixed timestamp makes repeated exports byte-identical.
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, payload)
    # Finish rendering/validation before writing any deliverable. No stale-file fallback.
    if not prepare_trusted_directory(root) or not all(
        trusted_managed_path(path, root, require_regular_file=False) for path in paths
    ):
        raise ValueError("export destination must contain no filesystem indirection")
    for path, payload in zip(paths, (raw, buffer.getvalue(), pdf), strict=True):
        verified_atomic_write(payload, path)
    return paths[0], paths[1], paths[2]


def _render_pdf(
    quiz: NativeQuiz,
    images: dict[str, SanitizedQuizImage],
    evidence: _Provenance,
    digest: str,
) -> bytes:
    _register_pdf_fonts()
    styles = getSampleStyleSheet()
    for name in ("Title", "Heading1", "Heading2"):
        styles[name].fontName = _PDF_BOLD
    body = ParagraphStyle(
        "QuizBody",
        parent=styles["BodyText"],
        fontName=_PDF_REGULAR,
        fontSize=10.5,
        leading=15,
        spaceAfter=8,
    )
    small = ParagraphStyle(
        "QuizSource", parent=body, fontSize=9, leading=13, textColor=colors.HexColor("#475569")
    )
    heading = ParagraphStyle(
        "QuizHeading",
        parent=styles["Heading2"],
        textColor=colors.HexColor("#1e3a5f"),
        keepWithNext=True,
    )

    def paragraph(text: str, style: Any = body) -> Any:
        glyphs = pdfmetrics.getFont(style.fontName).face.charToGlyph
        unsupported = sorted({ord(c) for c in text if c not in "\r\n" and not glyphs.get(ord(c))})
        if unsupported:
            codes = ", ".join(f"U+{code:04X}" for code in unsupported)
            raise ValueError(f"quiz PDF font does not support characters: {codes}")
        return Paragraph(escape(text).replace("\n", "<br/>"), style)

    story = [
        paragraph(quiz.title, styles["Title"]),
        paragraph(f"{len(quiz.questions)} reviewed questions", small),
        paragraph("Questions", styles["Heading1"]),
    ]
    for index, question in enumerate(quiz.questions, 1):
        story.extend([paragraph(f"Question {index}", heading), paragraph(question.stem)])
        if question.image_ref:
            item = images[question.image_ref.key]
            scale = min(504 / item.width, 360 / item.height, 1)
            story.extend(
                [
                    Image(
                        BytesIO(item.payload), width=item.width * scale, height=item.height * scale
                    ),
                    paragraph(f"Question {index}: {question.image_ref.description}", small),
                ]
            )
        if isinstance(question, QuizMatchingQuestion):
            story.extend(paragraph(f"{p.label}. {p.text}") for p in question.prompts)
        story.extend(
            paragraph(f"{chr(65 + i)}. {choice.text}") for i, choice in enumerate(question.choices)
        )
        if index < len(quiz.questions):
            story.append(Spacer(1, 12))
    story.extend([PageBreak(), paragraph("Answers and explanations", styles["Heading1"])])
    for index, question in enumerate(quiz.questions, 1):
        story.append(paragraph(f"Question {index}", heading))
        choices = {
            choice.id: f"{chr(65 + i)}. {choice.text}" for i, choice in enumerate(question.choices)
        }
        if isinstance(question, QuizMatchingQuestion):
            story.extend(
                paragraph(f"{p.label}: {choices[p.correct_choice_id]}") for p in question.prompts
            )
        else:
            story.append(paragraph(f"Correct answer: {choices[question.correct_choice_id]}"))
        story.append(paragraph(question.rationale))
    story.extend([PageBreak(), paragraph("Sources and objectives", styles["Heading1"])])
    for index, question in enumerate(quiz.questions, 1):
        record = evidence.questions[question.id]
        story.append(paragraph(f"Question {index} ({question.id})", heading))
        story.append(paragraph("Objectives: " + ", ".join(record.objective_ids), small))
        if question.learning_objective:
            story.append(paragraph(question.learning_objective, small))
        story.extend(
            paragraph(f"{ref.source_id}: {ref.segment_key} ({ref.locator})", small)
            for ref in record.source_refs
        )
        if question.image_ref:
            ref = question.image_ref
            story.append(paragraph(f"Image {ref.key}: {ref.source_title}, {ref.locator}", small))
    story.extend(
        [Spacer(1, 12), paragraph("Accepted payload SHA-256", heading), paragraph(digest, small)]
    )

    def footer(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont(_PDF_REGULAR, 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(54, 32, "Reviewed lecture quiz")
        canvas.drawRightString(558, 32, f"Page {document.page}")
        canvas.restoreState()

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=50,
        bottomMargin=50,
        title=quiz.title,
        invariant=1,
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    payload = buffer.getvalue()
    if not PdfReader(BytesIO(payload)).pages:
        raise ValueError("quiz PDF is empty")
    return payload
