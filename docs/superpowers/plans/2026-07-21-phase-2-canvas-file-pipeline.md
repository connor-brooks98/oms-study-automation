# Phase 2 Canvas File Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a localhost-only Canvas companion extension and Hub pipeline that discovers lecture PowerPoints and professor practice questions, files validated PDFs into the OMS II and iCloud staging trees, and holds lecture revisions for approval.

**Architecture:** A Manifest V3 extension uses the existing Chrome Canvas session, posts attachment metadata to the Hub, and downloads only Hub-approved files. Focused Python services classify and match sources against the Phase 1 catalog, preserve immutable revisions, serialize Microsoft Office conversion, and atomically promote verified outputs. New-source automation is opt-in after discovery-only review; revisions and uncertainty remain human-approved in the dashboard.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Pydantic 2, SQLite, pytest, `pypdf>=6.14,<7`, `msoffcrypto-tool>=6,<7`, `pywin32==312` on Windows, Chrome Manifest V3, Chrome alarms/downloads/storage APIs, and Node's built-in test runner for extension logic.

## Global Constraints

- Canvas base URL and extension host access are exactly `https://lmunet.instructure.com/*` and `http://127.0.0.1:8765/*`.
- The extension uses the active Canvas session; never read, export, or forward cookies and never store a Canvas password or API token.
- Scan only explicitly mapped courses and only module/page/direct-file content; never scan grades, quizzes, submissions, discussions, announcements, assignments, external LTI content, or the whole Files area.
- The extension alarm owns the 30-minute discovery cadence; the Hub must not create a competing Canvas poller.
- The first run is discovery-only. Automatic downloads require explicit dashboard enablement after preview.
- Automatically process lecture `.ppt`/`.pptx` and positive-evidence PQ `.pdf`/`.doc`/`.docx`/`.ppt`/`.pptx`; ignore professor lecture PDFs and route uncertain, macro-enabled, encrypted, or unsupported files to review.
- Resolve `%USERPROFILE%\Documents\OMS II` at runtime and place immutable revisions under `C:\ProgramData\OMSStudyHub\artifacts\revisions` by default.
- Only validated PDFs may reach final local or iCloud paths. Promotion is atomic and checksum-verified; failure must leave the prior final intact.
- A changed lecture is fully staged but never promoted until approval. Keep every source revision; never delete revisions automatically.
- Run one Office conversion at a time in the signed-in Windows session and close only the Office instance started by the Hub.
- Use `pypdf>=6.14,<7` and `pywin32==312; sys_platform == "win32"`.
- Use `msoffcrypto-tool>=6,<7` only to detect encrypted Office files; never attempt password guessing or decryption.
- Do not mark `practice_questions_uploaded` when a PQ is merely filed locally; that existing checklist step remains reserved for the later NotebookLM upload.
- Store only concise matching/classification evidence, never full Canvas page bodies.
- Preserve upstream MIT attribution in `extension/canvas-hub/LICENSE` and `extension/canvas-hub/NOTICE.md`.
- Every task must keep `pytest`, Ruff, strict mypy, and the existing Phase 1 acceptance suite green.
- Course subjects are exactly `Neuro`, `MSK`, `OPP`, `EPC`, `Heme/Lymph`, `Cardio`, `Renal`, and `Resp`; the setup UI displays Clinical Neuroscience, Musculoskeletal, Osteopathic Principles & Practice III, Essentials Patient Care III, Hematology & Lymph, Cardiovascular, Renal, and Respiratory as their expected Canvas-course hints.

---

## File Map

- `src/oms_hub/canvas/domain.py`: Canvas enums and immutable API/service value objects.
- `src/oms_hub/canvas/repository.py`: all Phase 2 persistence and idempotent state transitions.
- `src/oms_hub/canvas/classifier.py`: lecture/PQ/ignore/review decision rules.
- `src/oms_hub/canvas/matcher.py`: standard-course and EPC catalog matching.
- `src/oms_hub/canvas/routing.py`: canonical local, iCloud, and revision paths.
- `src/oms_hub/canvas/pairing.py`: one-time codes, bearer verification, and credential-store integration.
- `src/oms_hub/canvas/api.py`: localhost extension API and command/disposition contract.
- `src/oms_hub/canvas/ingestion.py`: inbox containment, stabilization, signature validation, and immutable source promotion.
- `src/oms_hub/canvas/pipeline.py`: processing jobs, conversion, validation, promotion, checklist updates, and recovery.
- `src/oms_hub/files/atomic.py`: verified atomic copy/replace primitives.
- `src/oms_hub/files/pdf.py`: PDF integrity checks.
- `src/oms_hub/files/office.py`: serial Word/PowerPoint COM adapter boundary.
- `src/oms_hub/web/canvas_routes.py`: setup, pairing, preview, review, and approval web routes.
- `extension/canvas-hub/`: private unpacked MV3 extension with no build step.

### Task 1: Phase 2 Configuration, Domain Types, and Schema

**Files:**
- Create: `src/oms_hub/canvas/__init__.py`
- Create: `src/oms_hub/canvas/domain.py`
- Modify: `src/oms_hub/config.py`
- Modify: `src/oms_hub/models.py`
- Modify: `pyproject.toml`
- Test: `tests/canvas/test_domain_and_schema.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces enums `ConnectionState`, `SourceKind`, `ReviewState`, `RevisionState`, `ArtifactRole`, `ValidationState`, `JobAction`, and `JobState`.
- Produces dataclasses `CanvasAttachment`, `Classification`, `CatalogMatch`, `DownloadDisposition`, and `CanonicalPaths` with the exact fields exercised below.
- Adds settings `canvas_base_url`, `canvas_inbox`, `revision_root`, `study_root`, `icloud_staging_root`, `canvas_auto_process`, `canvas_scan_minutes`, `office_timeout_seconds`, and `max_ingest_bytes`.
- Adds only new tables so `Database.create_schema()` upgrades an existing Phase 1 SQLite database without destructive migration.

- [ ] **Step 1: Write failing domain, configuration, and schema tests**

```python
# tests/canvas/test_domain_and_schema.py
from sqlalchemy import inspect

from oms_hub.canvas.domain import ArtifactRole, ConnectionState, SourceKind
from oms_hub.db import Database


def test_canvas_domain_values_are_stable() -> None:
    assert ConnectionState.LOGIN_REQUIRED.value == "canvas_login_required"
    assert SourceKind.LECTURE.value == "lecture"
    assert ArtifactRole.LOCAL_PDF.value == "local_pdf"


def test_create_schema_adds_phase_2_tables_without_removing_lectures(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.create_schema()
    tables = set(inspect(database.engine).get_table_names())
    assert "lectures" in tables
    assert {
        "canvas_connections", "canvas_course_mappings", "canvas_source_items",
        "source_revisions", "artifacts", "processing_jobs",
    } <= tables
```

```python
# append to tests/test_config.py
from pathlib import Path

from oms_hub.config import Settings


def test_canvas_defaults_are_local_and_discovery_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.canvas_base_url == "https://lmunet.instructure.com"
    assert settings.canvas_scan_minutes == 30
    assert settings.canvas_auto_process is False
    assert settings.study_root == Path(r"%USERPROFILE%\Documents\OMS II")
    assert settings.max_ingest_bytes == 250 * 1024 * 1024
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/canvas/test_domain_and_schema.py tests/test_config.py -q`

Expected: collection fails because `oms_hub.canvas.domain` and Canvas settings do not exist.

- [ ] **Step 3: Add exact domain values and settings**

```python
# src/oms_hub/canvas/domain.py
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ConnectionState(StrEnum):
    UNPAIRED = "unpaired"
    CONNECTED = "connected"
    LOGIN_REQUIRED = "canvas_login_required"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class SourceKind(StrEnum):
    LECTURE = "lecture"
    PRACTICE_QUESTIONS = "practice_questions"
    IGNORE = "ignore"
    REVIEW = "review"


class ReviewState(StrEnum):
    NONE = "none"
    NEEDS_REVIEW = "needs_review"
    RESOLVED = "resolved"


class RevisionState(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADED = "downloaded"
    PROPOSED = "proposed"
    CURRENT = "current"
    KEPT = "kept"
    FAILED = "failed"


class ArtifactRole(StrEnum):
    ORIGINAL = "original"
    STAGED_PDF = "staged_pdf"
    LOCAL_PPTX = "local_pptx"
    LOCAL_PDF = "local_pdf"
    ICLOUD_PDF = "icloud_pdf"


class ValidationState(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"


class JobAction(StrEnum):
    INGEST = "ingest"
    CONVERT = "convert"
    PROMOTE = "promote"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class CanvasAttachment:
    course_id: str
    course_name: str
    course_code: str
    module_id: str
    module_title: str
    item_id: str
    item_title: str
    item_type: str
    page_url: str
    page_title: str
    file_id: str
    filename: str
    content_type: str
    size: int
    modified_at: str
    download_url: str
    evidence_text: str = ""


@dataclass(frozen=True, slots=True)
class Classification:
    kind: SourceKind
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class CatalogMatch:
    lecture_id: int | None
    subject: str | None
    exam_number: int | None
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class DownloadDisposition:
    source_item_id: int
    action: str
    reason: str
    relative_filename: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalPaths:
    revision_original: Path
    revision_pdf: Path
    local_source: Path | None
    local_pdf: Path
    icloud_pdf: Path
```

Add these fields to `Settings` in `src/oms_hub/config.py`:

```python
    canvas_base_url: str = "https://lmunet.instructure.com"
    canvas_inbox: Path = Path(r"%USERPROFILE%\Downloads\OMSStudyHub\CanvasInbox")
    revision_root: Path = Path(r"C:\ProgramData\OMSStudyHub\artifacts\revisions")
    study_root: Path = Path(r"%USERPROFILE%\Documents\OMS II")
    icloud_staging_root: Path | None = None
    canvas_auto_process: bool = False
    canvas_scan_minutes: int = Field(default=30, ge=30, le=30)
    office_timeout_seconds: int = Field(default=180, ge=30, le=600)
    max_ingest_bytes: int = Field(default=250 * 1024 * 1024, ge=1)
```

Add `pypdf>=6.14,<7` and `msoffcrypto-tool>=6,<7` to project dependencies, plus `pywin32==312; sys_platform == "win32"` as a Windows-only dependency.

- [ ] **Step 4: Add the six Phase 2 SQLAlchemy models**

Add models with these uniqueness and foreign-key rules:

```python
class CanvasConnectionModel(Base):
    __tablename__ = "canvas_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    base_url: Mapped[str] = mapped_column(String(300), unique=True)
    state: Mapped[str] = mapped_column(String(40), default="unpaired")
    extension_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    credential_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paired_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_heartbeat: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_successful_scan: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_requested_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    study_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    icloud_staging_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovery_confirmed: Mapped[bool] = mapped_column(default=False)
    auto_process: Mapped[bool] = mapped_column(default=False)
    last_scan_item_count: Mapped[int] = mapped_column(default=0)
    last_scan_new_count: Mapped[int] = mapped_column(default=0)


class CanvasCourseMappingModel(Base):
    __tablename__ = "canvas_course_mappings"
    __table_args__ = (UniqueConstraint("course_id"), UniqueConstraint("subject"))
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[str] = mapped_column(String(100))
    course_name: Mapped[str] = mapped_column(String(300))
    course_code: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(default=True)


class CanvasSourceItemModel(Base):
    __tablename__ = "canvas_source_items"
    __table_args__ = (UniqueConstraint("course_id", "file_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[str] = mapped_column(String(100))
    file_id: Mapped[str] = mapped_column(String(100))
    filename: Mapped[str] = mapped_column(String(500))
    source_url: Mapped[str] = mapped_column(Text)
    context_json: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(40))
    lecture_id: Mapped[int | None] = mapped_column(ForeignKey("lectures.id"), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(100), nullable=True)
    exam_number: Mapped[int | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    review_state: Mapped[str] = mapped_column(String(30), default="none")
    discovered_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class SourceRevisionModel(Base):
    __tablename__ = "source_revisions"
    __table_args__ = (UniqueConstraint("source_item_id", "remote_signature"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("canvas_source_items.id"))
    remote_signature: Mapped[str] = mapped_column(String(64))
    modified_at: Mapped[str] = mapped_column(String(60))
    remote_size: Mapped[int]
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    stored_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="discovered")
    discovered_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ArtifactModel(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("revision_id", "role", "path"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("source_revisions.id"))
    role: Mapped[str] = mapped_column(String(40))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    validation_state: Mapped[str] = mapped_column(String(30), default="pending")
    promoted_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    current: Mapped[bool] = mapped_column(default=False)


class ProcessingJobModel(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (UniqueConstraint("revision_id", "action"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("source_revisions.id"))
    action: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)
```

- [ ] **Step 5: Run checks and commit**

Run: `python -m pytest tests/canvas/test_domain_and_schema.py tests/test_config.py -q`

Expected: all focused tests pass.

Run: `python -m ruff check src tests && python -m mypy src/oms_hub`

Expected: both commands exit 0.

```bash
git add pyproject.toml src/oms_hub/config.py src/oms_hub/models.py src/oms_hub/canvas tests/canvas tests/test_config.py
git commit -m "feat: add Canvas pipeline domain and schema"
```

### Task 2: Course Mapping, Classification, and Catalog Matching

**Files:**
- Create: `src/oms_hub/canvas/classifier.py`
- Create: `src/oms_hub/canvas/matcher.py`
- Create: `src/oms_hub/canvas/repository.py`
- Test: `tests/canvas/test_classifier.py`
- Test: `tests/canvas/test_matcher.py`
- Test: `tests/canvas/test_repository.py`

**Interfaces:**
- Consumes: `CanvasAttachment`, `Classification`, `CatalogMatch`, `SourceKind`, Phase 1 `LectureModel`, and `Database`.
- Produces: `classify_attachment(value: CanvasAttachment) -> Classification`.
- Produces: `match_attachment(value: CanvasAttachment, subject: str, lectures: Sequence[LectureModel]) -> CatalogMatch`.
- Produces `CanvasRepository` methods `replace_course_mappings`, `list_course_mappings`, `ingest_metadata`, `get_disposition_context`, and `list_review_items`.

- [ ] **Step 1: Write failing classification tests for required positive and negative rules**

```python
# tests/canvas/test_classifier.py
from dataclasses import replace

from oms_hub.canvas.classifier import classify_attachment
from oms_hub.canvas.domain import CanvasAttachment, SourceKind


def attachment(filename: str, **changes: object) -> CanvasAttachment:
    base = CanvasAttachment(
        "751", "Hematology & Lymph", "LEC-DOSYS-751", "10", "Exam 1 Lectures",
        "20", "Lecture 4: Anemia I", "Page", "/courses/751/pages/anemia-i", "Lecture 4: Anemia I",
        "30", filename, "application/octet-stream", 1234, "2026-07-21T12:00:00Z",
        "https://lmunet.instructure.com/files/30/download", "",
    )
    return replace(base, **changes)


def test_lecture_powerpoint_wins_on_lecture_page() -> None:
    result = classify_attachment(attachment("2026 Student Anemia.pptx"))
    assert result.kind is SourceKind.LECTURE
    assert result.confidence >= 0.90


def test_duplicate_lecture_pdf_is_ignored() -> None:
    result = classify_attachment(attachment("2026 Student Anemia.pdf"))
    assert result.kind is SourceKind.IGNORE
    assert "lecture PDF" in result.reason


def test_positive_pq_docx_is_collected() -> None:
    result = classify_attachment(attachment("Practice questions for anemia.docx"))
    assert result.kind is SourceKind.PRACTICE_QUESTIONS


def test_negative_reading_overrides_weak_question_word() -> None:
    result = classify_attachment(attachment("Reading assignment questions.pdf"))
    assert result.kind is SourceKind.IGNORE


def test_macro_enabled_office_file_requires_review() -> None:
    assert classify_attachment(attachment("Anemia lecture.pptm")).kind is SourceKind.REVIEW
```

- [ ] **Step 2: Run classifier tests and verify failure**

Run: `python -m pytest tests/canvas/test_classifier.py -q`

Expected: import fails because `classifier.py` does not exist.

- [ ] **Step 3: Implement deterministic classification**

```python
# src/oms_hub/canvas/classifier.py
from pathlib import Path

from oms_hub.canvas.domain import CanvasAttachment, Classification, SourceKind

PQ_TERMS = ("practice question", "practice qs", "question set", "review question", "case questions")
NEGATIVE_TERMS = (
    "reading", "objective", "rubric", "article", "lab instruction", "expectation",
    "assignment", "lockdown browser",
)
AUTO_PQ_SUFFIXES = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}


def classify_attachment(value: CanvasAttachment) -> Classification:
    suffix = Path(value.filename).suffix.casefold()
    context = " ".join((value.module_title, value.item_title, value.page_title, value.filename, value.evidence_text)).casefold()
    if suffix in {".pptm", ".docm", ".xlsm"}:
        return Classification(SourceKind.REVIEW, 1.0, "macro-enabled Office file")
    has_pq = any(term in context for term in PQ_TERMS)
    has_negative = any(term in context for term in NEGATIVE_TERMS)
    if has_negative and not any(term in value.filename.casefold() for term in PQ_TERMS):
        return Classification(SourceKind.IGNORE, 0.95, "negative content category")
    if has_pq:
        if suffix in AUTO_PQ_SUFFIXES:
            return Classification(SourceKind.PRACTICE_QUESTIONS, 0.95, "positive practice-question evidence")
        return Classification(SourceKind.REVIEW, 0.85, "practice questions use an unsupported file type")
    lecture_context = "lecture" in f"{value.item_title} {value.page_title}".casefold()
    if lecture_context and suffix in {".ppt", ".pptx"}:
        return Classification(SourceKind.LECTURE, 0.95, "PowerPoint on a lecture page")
    if lecture_context and suffix == ".pdf":
        return Classification(SourceKind.IGNORE, 0.99, "duplicate professor lecture PDF")
    return Classification(SourceKind.IGNORE, 0.70, "not a lecture PowerPoint or professor practice questions")
```

- [ ] **Step 4: Write failing standard-course and EPC matching tests**

```python
# tests/canvas/test_matcher.py
from dataclasses import replace
from types import SimpleNamespace

from oms_hub.canvas.domain import CanvasAttachment
from oms_hub.canvas.matcher import match_attachment


def attachment(filename: str, **changes: object) -> CanvasAttachment:
    base = CanvasAttachment(
        "751", "Hematology & Lymph", "LEC-DOSYS-751", "10", "Exam 1 Lectures", "20",
        "Lecture 4: Anemia I", "Page", "/courses/751/pages/anemia-i", "Lecture 4: Anemia I", "30",
        filename, "application/octet-stream", 1234, "2026-07-21T12:00:00Z",
        "https://lmunet.instructure.com/files/30/download", "",
    )
    return replace(base, **changes)


def lecture(id: int, subject: str, exam: int, number: int, topic: str) -> SimpleNamespace:
    return SimpleNamespace(id=id, subject=subject, exam_number=exam, lecture_number=number, topic=topic)


def test_standard_match_requires_subject_exam_and_lecture_number() -> None:
    value = attachment("anemia.pptx")
    catalog = [lecture(7, "Heme/Lymph", 1, 4, "Anemia I")]
    result = match_attachment(value, "Heme/Lymph", catalog)
    assert (result.lecture_id, result.exam_number) == (7, 1)
    assert result.confidence >= 0.95


def test_epc_unique_topic_match_derives_exam_and_number() -> None:
    value = attachment(
        "Giving the Assessment and Plan.pptx", module_title="Giving the Assessment and Plan",
        item_title="Giving the Assessment and Plan Lecture", page_title="Giving the Assessment and Plan Lecture",
    )
    catalog = [
        lecture(8, "EPC", 1, 3, "The Difficult Patient"),
        lecture(9, "EPC", 1, 4, "Giving the Assessment and Plan"),
    ]
    result = match_attachment(value, "EPC", catalog)
    assert (result.lecture_id, result.exam_number) == (9, 1)
    assert result.confidence >= 0.88


def test_epc_competing_topic_matches_require_review() -> None:
    value = attachment("Communication.pptx", module_title="Communication", item_title="Communication Lecture")
    catalog = [lecture(10, "EPC", 1, 1, "Communication I"), lecture(11, "EPC", 2, 7, "Communication II")]
    assert match_attachment(value, "EPC", catalog).lecture_id is None
```

- [ ] **Step 5: Implement normalization, numbered matching, and EPC score margin**

```python
# src/oms_hub/canvas/matcher.py
import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Protocol

from oms_hub.canvas.domain import CanvasAttachment, CatalogMatch


class LectureRecord(Protocol):
    id: int
    subject: str
    exam_number: int
    lecture_number: int
    topic: str


def _number(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _topic_score(value: CanvasAttachment, topic: str) -> float:
    normalized_topic = _normalized(topic)
    contexts = (
        _normalized(value.module_title),
        _normalized(value.item_title.removesuffix(" Lecture")),
        _normalized(value.page_title.removesuffix(" Lecture")),
    )
    return max(SequenceMatcher(None, context, normalized_topic).ratio() for context in contexts if context)


def match_attachment(value: CanvasAttachment, subject: str, lectures: Sequence[LectureRecord]) -> CatalogMatch:
    candidates = [item for item in lectures if item.subject == subject]
    exam = _number(r"exam\s*(\d+)", value.module_title)
    number = _number(r"lecture\s*(\d+)", f"{value.item_title} {value.page_title}")
    if subject != "EPC" and exam is not None and number is not None:
        exact = [item for item in candidates if item.exam_number == exam and item.lecture_number == number]
        if len(exact) == 1:
            return CatalogMatch(exact[0].id, subject, exam, 0.99, "course, exam, and lecture number agree")
        return CatalogMatch(None, subject, exam, 0.0, "catalog number match is missing or conflicting")
    ranked = sorted(
        ((_topic_score(value, item.topic), item) for item in candidates),
        key=lambda pair: pair[0], reverse=True,
    )
    if not ranked or ranked[0][0] < 0.62:
        return CatalogMatch(None, subject, None, ranked[0][0] if ranked else 0.0, "topic match is too weak")
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.12:
        return CatalogMatch(None, subject, None, ranked[0][0], "topic match is not unique")
    best = ranked[0][1]
    return CatalogMatch(best.id, subject, best.exam_number, 0.90, "unique strong topic match")
```

- [ ] **Step 6: Add repository tests and idempotent persistence**

Test that replacing mappings enforces the approved subjects, metadata replay returns the existing source/revision, a changed remote signature creates one new revision, and `list_review_items()` returns only `needs_review`. Implement `CanvasRepository` with transaction-scoped SQLAlchemy queries and these signatures:

The public method signatures are `replace_course_mappings(values: list[CourseMappingInput]) -> None`, `list_course_mappings() -> list[CanvasCourseMappingModel]`, `ingest_metadata(value: CanvasAttachment, classification: Classification, match: CatalogMatch) -> MetadataResult`, `get_disposition_context(source_item_id: int) -> DispositionContext`, and `list_review_items() -> list[CanvasSourceItemModel]`. Define `CourseMappingInput`, `MetadataResult`, and `DispositionContext` as frozen slot dataclasses in `canvas/domain.py`; their fields are respectively `(course_id, course_name, course_code, subject, enabled)`, `(source_item_id, revision_id, created, review_state)`, and `(source_item_id, revision_id, kind, lecture_id, subject, exam_number, confidence, has_current_artifact)`.

Use `remote_signature = sha256(f"{course_id}\0{file_id}\0{modified_at}\0{size}".encode()).hexdigest()`. Store `context_json` with IDs/titles/type and store `evidence_json` with only `classification.reason` and `match.reason`. Never store `evidence_text` or full page HTML.

- [ ] **Step 7: Run checks and commit**

Run: `python -m pytest tests/canvas/test_classifier.py tests/canvas/test_matcher.py tests/canvas/test_repository.py -q`

Expected: all tests pass, including replay and changed-revision cases.

```bash
git add src/oms_hub/canvas tests/canvas
git commit -m "feat: classify and match Canvas sources"
```

### Task 3: Canonical Routing and Verified File Primitives

**Files:**
- Create: `src/oms_hub/files/__init__.py`
- Create: `src/oms_hub/files/atomic.py`
- Create: `src/oms_hub/files/pdf.py`
- Create: `src/oms_hub/canvas/routing.py`
- Modify: `src/oms_hub/naming.py`
- Test: `tests/files/test_atomic.py`
- Test: `tests/files/test_pdf.py`
- Test: `tests/canvas/test_routing.py`
- Test: `tests/test_naming.py`

**Interfaces:**
- Produces `sha256_file(path: Path) -> str`, `verified_atomic_copy(source: Path, destination: Path) -> str`, and `validate_pdf(path: Path) -> PdfValidation`.
- Produces `build_paths(settings: Settings, lecture: LectureKey, kind: SourceKind, original_filename: str, revision_id: int) -> CanonicalPaths`.

- [ ] **Step 1: Write failing routing and file-integrity tests**

```python
# tests/canvas/test_routing.py
from pathlib import Path
from oms_hub.canvas.domain import SourceKind
from oms_hub.canvas.routing import build_paths
from oms_hub.config import Settings
from oms_hub.domain import LectureKey


def test_lecture_paths_follow_local_and_goodnotes_conventions(tmp_path) -> None:
    settings = Settings(
        _env_file=None, study_root=tmp_path / "OMS II", icloud_staging_root=tmp_path / "iCloud",
        revision_root=tmp_path / "revisions",
    )
    paths = build_paths(settings, LectureKey("Neuro", 1, 1, "General CNS Pathology"), SourceKind.LECTURE, "source.pptx", 42)
    assert paths.local_source == tmp_path / "OMS II/Neuro/Exam 1/Lectures/Lecture 01 - General CNS Pathology.pptx"
    assert paths.local_pdf == tmp_path / "OMS II/Neuro/Exam 1/Lectures/Lecture 01 - General CNS Pathology.pdf"
    assert paths.icloud_pdf == tmp_path / "iCloud/OMS II Goodnotes Inbox/Neuro/Exam 1/Lecture 01 - General CNS Pathology.pdf"
    assert paths.revision_original == tmp_path / "revisions/42/source.pptx"
```

```python
# tests/files/test_atomic.py
from oms_hub.files.atomic import sha256_file, verified_atomic_copy


def test_verified_copy_creates_parent_and_preserves_checksum(tmp_path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"complete")
    destination = tmp_path / "nested/final.bin"
    digest = verified_atomic_copy(source, destination)
    assert destination.read_bytes() == b"complete"
    assert digest == sha256_file(destination)
    assert not list(destination.parent.glob("*.partial-*"))
```

```python
# tests/files/test_pdf.py
from pypdf import PdfWriter
from oms_hub.files.pdf import validate_pdf


def test_pdf_requires_at_least_one_page(tmp_path) -> None:
    path = tmp_path / "one.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)
    assert validate_pdf(path).page_count == 1
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/canvas/test_routing.py tests/files -q`

Expected: imports fail because the routing and files modules do not exist.

- [ ] **Step 3: Implement verified copying and PDF validation**

```python
# src/oms_hub/files/atomic.py
import hashlib
import os
import shutil
import uuid
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_atomic_copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial-{uuid.uuid4().hex}")
    try:
        shutil.copy2(source, temporary)
        source_digest = sha256_file(source)
        if sha256_file(temporary) != source_digest:
            raise OSError("copied file checksum mismatch")
        os.replace(temporary, destination)
        if sha256_file(destination) != source_digest:
            raise OSError("promoted file checksum mismatch")
        return source_digest
    finally:
        temporary.unlink(missing_ok=True)
```

```python
# src/oms_hub/files/pdf.py
from dataclasses import dataclass
from pathlib import Path
from pypdf import PdfReader


@dataclass(frozen=True, slots=True)
class PdfValidation:
    page_count: int
    size: int


def validate_pdf(path: Path) -> PdfValidation:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("PDF is missing or empty")
    with path.open("rb") as stream:
        reader = PdfReader(stream, strict=True)
        page_count = len(reader.pages)
    if page_count < 1:
        raise ValueError("PDF contains no pages")
    return PdfValidation(page_count=page_count, size=path.stat().st_size)
```

- [ ] **Step 4: Implement canonical paths using existing naming sanitization**

Promote the existing private `_safe_topic()` helper to public `sanitize_filename(value: str) -> str` in `src/oms_hub/naming.py`, retain its current behavior for Phase 1 callers, and add Windows reserved-device handling by prefixing `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, and `LPT1`–`LPT9` with `_`. Expand the supplied Settings paths with `os.path.expandvars`, reject a missing iCloud root before building its path, preserve the original suffix for lecture sources, use a stable sanitized original stem suffix for multiple PQ sets, and return paths exactly as asserted in Step 1. The pipeline in Task 9 must supply an effective Settings copy whose study/iCloud roots use confirmed database values when present. The PQ base format is `Lecture 01 - Topic - {descriptive stem}.pdf`, with `Practice Questions` substituted when the descriptive stem contains only generic PQ terms.

- [ ] **Step 5: Add traversal and failure-preservation cases, run checks, and commit**

Add tests proving sanitized topics cannot escape the root, Windows reserved names are prefixed, a failed copy leaves an existing destination unchanged, invalid/truncated PDFs fail, and two differently named PQs receive different stable destinations.

Run: `python -m pytest tests/canvas/test_routing.py tests/files -q`

Expected: all tests pass.

```bash
git add src/oms_hub/files src/oms_hub/canvas/routing.py src/oms_hub/naming.py tests/files tests/canvas/test_routing.py tests/test_naming.py
git commit -m "feat: add verified Canvas artifact routing"
```

### Task 4: Pairing Security and Local Extension API

**Files:**
- Create: `src/oms_hub/canvas/pairing.py`
- Create: `src/oms_hub/canvas/api.py`
- Modify: `src/oms_hub/security/secret_store.py`
- Modify: `src/oms_hub/app.py`
- Test: `tests/canvas/test_pairing.py`
- Test: `tests/canvas/test_api.py`

**Interfaces:**
- Produces `PairingService.create_code() -> PairingCode`, `PairingService.exchange(code: str, extension_id: str) -> str`, `PairingService.verify(bearer: str) -> None`, and `PairingService.revoke() -> None`.
- Produces extension endpoints `POST /api/canvas/pair`, `POST /api/canvas/heartbeat`, `GET /api/canvas/config`, `POST /api/canvas/discover`, and `POST /api/canvas/download-complete`.
- `GET /api/canvas/config` returns only mapped course IDs/names, scan interval, discovery-only/auto flag, inbox relative path, and an optional one-shot `scan_requested` flag.

- [ ] **Step 1: Write failing pairing and API boundary tests**

```python
# tests/canvas/test_pairing.py
from oms_hub.canvas.pairing import PairingService


def test_pairing_code_is_one_time_and_database_stores_only_fingerprint(memory_secret_store, canvas_repo) -> None:
    service = PairingService(canvas_repo, memory_secret_store)
    code = service.create_code()
    bearer = service.exchange(code.value, "extension-test")
    service.verify(bearer)
    assert canvas_repo.connection().credential_fingerprint != bearer
    assert memory_secret_store.get("canvas_extension_bearer") == bearer
    try:
        service.exchange(code.value, "second-extension")
    except ValueError as error:
        assert "expired or already used" in str(error)
    else:
        raise AssertionError("pairing code was reusable")
```

```python
# tests/canvas/test_api.py
def test_canvas_api_rejects_missing_bearer(client) -> None:
    response = client.post("/api/canvas/heartbeat", json={"state": "connected"})
    assert response.status_code == 401


def test_discover_rejects_non_lmu_urls(client, paired_headers, attachment_payload) -> None:
    attachment_payload["download_url"] = "https://evil.example/file"
    response = client.post("/api/canvas/discover", headers=paired_headers, json={"items": [attachment_payload]})
    assert response.status_code == 422


def test_discover_rejects_oversized_batches(client, paired_headers, attachment_payload) -> None:
    response = client.post("/api/canvas/discover", headers=paired_headers, json={"items": [attachment_payload] * 501})
    assert response.status_code == 422
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/canvas/test_pairing.py tests/canvas/test_api.py -q`

Expected: imports or routes fail because pairing/API components do not exist.

- [ ] **Step 3: Implement one-time pairing and constant-time verification**

Generate a six-digit code with `secrets.randbelow`, keep only its SHA-256 and a five-minute monotonic expiry in process memory, and generate the bearer with `secrets.token_urlsafe(32)`. Store the bearer under the existing keyring service `OMSStudyHub` and key `canvas_extension_bearer` through the secret-store protocol. Add `delete(key: str) -> None` to `SecretStore` and implement it with `keyring.delete_password`, ignoring only `keyring.errors.PasswordDeleteError`; update Phase 1 fake stores to supply `delete`. Store only `sha256(bearer.encode()).hexdigest()` in `CanvasConnectionModel`, and use `hmac.compare_digest` for verification. Revocation deletes the credential and clears the fingerprint/extension identity.

- [ ] **Step 4: Implement strict Pydantic request schemas and authenticated routes**

Use `Field(max_length=1024)` for URLs/paths, `Field(max_length=500)` for filenames/titles, `HttpUrl`, `extra="forbid"`, a maximum of 500 metadata items per request, and exact `https://lmunet.instructure.com` host validation. Return `413` for bodies beyond 2 MiB through a route-level `Content-Length` check. The discovery response is:

```json
{
  "dispositions": [
    {"source_item_id": 12, "action": "download", "reason": "new high-confidence lecture", "relative_filename": "12/30/source.pptx"}
  ]
}
```

Allowed actions are `download`, `skip`, and `review`. Discovery-only changes every otherwise-download disposition to `review` with reason `Discovery-only mode is enabled` and does not create a download job. The authoritative switch is `CanvasConnectionModel.auto_process`; `Settings.canvas_auto_process` only seeds a new connection and may force automation off through the environment, never on past an incomplete setup gate.

- [ ] **Step 5: Include the Canvas router in the app and run security cases**

Update `create_app()` to instantiate `CanvasRepository`/`PairingService` on `app.state`, include the Canvas API router, and retain the `127.0.0.1` default. Add tests for wrong bearer, revoked bearer, expired code, HTML content type, invalid action schema, disabled course, and unmapped course.

Run: `python -m pytest tests/canvas/test_pairing.py tests/canvas/test_api.py -q`

Expected: all tests pass.

```bash
git add src/oms_hub/canvas src/oms_hub/security/secret_store.py src/oms_hub/app.py tests/canvas
git commit -m "feat: secure the Canvas companion API"
```

### Task 5: Manifest V3 Extension Discovery Core

**Files:**
- Create: `extension/canvas-hub/manifest.json`
- Create: `extension/canvas-hub/LICENSE`
- Create: `extension/canvas-hub/NOTICE.md`
- Create: `extension/canvas-hub/package.json`
- Create: `extension/canvas-hub/lib/canvas-api.js`
- Create: `extension/canvas-hub/lib/discovery.js`
- Create: `extension/canvas-hub/tests/canvas-api.test.js`
- Create: `extension/canvas-hub/tests/discovery.test.js`

**Interfaces:**
- Produces `canvasFetchJson(path, fetchImpl)`, `listAll(path, fetchImpl)`, `isAuthenticationResponse(response, bodyText)`, `discoverCourse(course, fetchImpl)`, and `extractAttachments(pageHtml, context)`.
- Emits metadata using the exact `CanvasAttachment` snake_case JSON fields accepted by Task 4.

- [ ] **Step 1: Add the restricted manifest and license files**

```json
{
  "manifest_version": 3,
  "name": "OMS Study Hub Canvas Companion",
  "version": "0.1.0",
  "description": "Discovers mapped LMU lecture files for the local OMS Study Hub.",
  "permissions": ["alarms", "downloads", "storage"],
  "host_permissions": [
    "https://lmunet.instructure.com/*",
    "http://127.0.0.1:8765/*"
  ],
  "background": {"service_worker": "background.js", "type": "module"},
  "action": {"default_popup": "popup.html"}
}
```

Copy the complete MIT license text from `jasp-nerd/canvas-course-downloader` into `LICENSE`. In `NOTICE.md`, name the upstream project, URL, reviewed baseline `v2.10.0`, MIT license, and state that session pagination/download patterns informed this extension.

- [ ] **Step 2: Write failing pagination, authentication, and discovery tests**

```javascript
// extension/canvas-hub/tests/canvas-api.test.js
import test from "node:test";
import assert from "node:assert/strict";
import { isAuthenticationResponse, listAll } from "../lib/canvas-api.js";

test("detects Canvas login HTML", () => {
  const response = { status: 200, headers: new Headers({"content-type": "text/html"}), url: "https://lmunet.instructure.com/login" };
  assert.equal(isAuthenticationResponse(response, "<form id='login_form'>"), true);
});

test("follows Canvas Link pagination", async () => {
  const pages = [
    new Response(JSON.stringify([{id: 1}]), {headers: {"content-type": "application/json", "link": "</api/v1/x?page=2>; rel=\"next\""}}),
    new Response(JSON.stringify([{id: 2}]), {headers: {"content-type": "application/json"}}),
  ];
  assert.deepEqual(await listAll("/api/v1/x", async () => pages.shift()), [{id: 1}, {id: 2}]);
});
```

Add discovery fixtures proving only `Page` and direct `File` module items are opened, page attachment links become metadata, assignments/quizzes/external URLs are not opened, and course/module/item/page/file identifiers are preserved.

- [ ] **Step 3: Run extension tests and verify failure**

Run: `cd extension/canvas-hub && node --test`

Expected: tests fail because the JavaScript modules do not exist.

- [ ] **Step 4: Implement authenticated REST pagination and bounded discovery**

`canvasFetchJson` must call `fetch` with `{credentials: "include", redirect: "follow"}`, reject 401/403, reject login URL/HTML, require `application/json`, and throw typed `CanvasLoginRequiredError` or `CanvasProtocolError`. `listAll` follows only a `rel="next"` URL on the same `lmunet.instructure.com` origin and stops after 100 pages.

`discoverCourse` requests:

```text
/api/v1/courses/{course_id}/modules?include[]=items&per_page=100
/api/v1/courses/{course_id}/pages/{page_url}
/api/v1/files/{file_id}
```

It accepts only module item types `Page` and `File`, extracts same-origin `/files/<id>/download` anchors from page HTML with `DOMParser`, fetches file metadata, truncates plain-text evidence to 500 characters, deduplicates by course/file ID, and never follows arbitrary page links.

- [ ] **Step 5: Run tests and inspect manifest permissions**

Run: `cd extension/canvas-hub && node --test`

Expected: all tests pass.

Run: `python -c "import json; p=json.load(open('extension/canvas-hub/manifest.json')); assert p['host_permissions']==['https://lmunet.instructure.com/*','http://127.0.0.1:8765/*']; assert 'cookies' not in p['permissions']"`

Expected: exits 0.

```bash
git add extension/canvas-hub
git commit -m "feat: discover Canvas module attachments"
```

### Task 6: Extension Pairing, Scheduling, and Downloads

**Files:**
- Create: `extension/canvas-hub/lib/hub-client.js`
- Create: `extension/canvas-hub/lib/scanner.js`
- Create: `extension/canvas-hub/lib/downloads.js`
- Create: `extension/canvas-hub/background.js`
- Create: `extension/canvas-hub/popup.html`
- Create: `extension/canvas-hub/popup.js`
- Create: `extension/canvas-hub/tests/hub-client.test.js`
- Create: `extension/canvas-hub/tests/scanner.test.js`
- Create: `extension/canvas-hub/tests/downloads.test.js`

**Interfaces:**
- Consumes the Task 4 API and Task 5 discovery functions.
- Produces `pair(code)`, `heartbeat(state)`, `getConfig()`, `postDiscover(items)`, `postDownloadComplete(payload)`, `runScan(dependencies)`, and `downloadDisposition(disposition, metadata)`.

- [ ] **Step 1: Write failing client, scanner, and download tests**

Tests must prove the bearer is saved only to `chrome.storage.local`, every paired Hub call sends an `Authorization` header containing the stored bearer, alarm name is `canvas_scan` with `periodInMinutes: 30`, startup creates the alarm if absent, scan-now uses the same single-flight lock, only `download` dispositions call `chrome.downloads.download`, filenames remain beneath `OMSStudyHub/CanvasInbox`, download completion reports an absolute resolved path, and 401/403/login errors set `canvas_login_required` without immediate retry.

- [ ] **Step 2: Run tests and verify failure**

Run: `cd extension/canvas-hub && node --test`

Expected: new tests fail because client/scanner/download modules do not exist.

- [ ] **Step 3: Implement the localhost Hub client and single-flight scanner**

All Hub requests target the constant `http://127.0.0.1:8765`. Pairing is the only unauthenticated request. Use JSON content type, a 15-second `AbortSignal.timeout`, schema checks on disposition actions, and redact the bearer from error strings. `runScan` must:

1. return `already_running` when its module-level promise exists;
2. heartbeat `scanning`;
3. fetch mapped config;
4. discover only enabled course IDs;
5. post batches of at most 100 metadata objects;
6. invoke downloads only for `download` actions;
7. heartbeat `connected` with item counts; and
8. heartbeat `canvas_login_required` on `CanvasLoginRequiredError` without backoff retries.

- [ ] **Step 4: Implement managed downloads and service-worker lifecycle**

Use `chrome.downloads.download({url, filename, conflictAction: "uniquify", saveAs: false})`. Keep a `downloadId -> {source_item_id, relative_filename}` map in `chrome.storage.session`; on `chrome.downloads.onChanged` completion call `chrome.downloads.search({id})`, require one complete record with a local filename, and post it to `/api/canvas/download-complete`. Create the 30-minute alarm in both `runtime.onInstalled` and `runtime.onStartup`. The popup provides Pair, Scan now, status, last scan, and Login required messaging but never renders or logs the bearer.

- [ ] **Step 5: Run tests, load-check the manifest, and commit**

Run: `cd extension/canvas-hub && node --test`

Expected: all extension tests pass.

```bash
git add extension/canvas-hub
git commit -m "feat: pair and schedule the Canvas companion"
```

### Task 7: Download Ingestion and Immutable Revision Storage

**Files:**
- Create: `src/oms_hub/canvas/ingestion.py`
- Create: `src/oms_hub/files/office_security.py`
- Modify: `src/oms_hub/canvas/api.py`
- Modify: `src/oms_hub/canvas/repository.py`
- Test: `tests/canvas/test_ingestion.py`
- Test: `tests/canvas/test_download_api.py`
- Test: `tests/files/test_office_security.py`

**Interfaces:**
- Produces `IngestionService.complete_download(source_item_id: int, download_id: int, path: Path) -> IngestedRevision`.
- Produces exception `OfficeSecurityError` and `office_file_is_encrypted(path: Path) -> bool`.
- Consumes `verified_atomic_copy`, Settings inbox/revision roots, and repository revision state methods.
- Enqueues exactly one `JobAction.CONVERT` job only for a validated, checksum-finalized revision that is allowed to process.

- [ ] **Step 1: Write failing containment, stabilization, and replay tests**

```python
# tests/canvas/test_ingestion.py
def test_rejects_download_outside_managed_inbox(ingestion, tmp_path) -> None:
    outside = tmp_path / "outside.pptx"
    outside.write_bytes(b"pptx")
    with pytest.raises(ValueError, match="managed Canvas inbox"):
        ingestion.complete_download(1, 99, outside)


def test_ingest_promotes_one_immutable_original_and_replay_is_idempotent(ingestion, inbox_file) -> None:
    first = ingestion.complete_download(1, 99, inbox_file)
    second = ingestion.complete_download(1, 99, inbox_file)
    assert first.revision_id == second.revision_id
    assert first.sha256 == second.sha256
    assert first.stored_path.read_bytes() == inbox_file.read_bytes()
    assert ingestion.repository.count_jobs(first.revision_id, "convert") == 1
```

In `tests/files/test_office_security.py`, patch `msoffcrypto.OfficeFile` with fakes whose `is_encrypted()` returns true and false, plus a fake that raises a parse exception. Assert true/false are returned unchanged and the parse exception becomes `OfficeSecurityError`, which ingestion maps to review without calling the converter.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/canvas/test_ingestion.py tests/canvas/test_download_api.py tests/files/test_office_security.py -q`

Expected: import or route failures.

- [ ] **Step 3: Implement safe ingestion**

Resolve the reported path with `Path.resolve(strict=True)` and require `path.is_relative_to(expanded_canvas_inbox.resolve())`. Reject files larger than `max_ingest_bytes`, wait for two equal `(size, mtime_ns)` samples 500 ms apart with a five-second bound, require the expected suffix and ZIP magic for `.pptx/.docx`, OLE magic for `.ppt/.doc`, and `%PDF-` for `.pdf`. Implement `office_file_is_encrypted` by opening the file in binary mode, constructing `msoffcrypto.OfficeFile(stream)`, and returning `is_encrypted()`; parsing errors require review. Call this preflight for every Office source and enter review before conversion when it returns true. Calculate SHA-256, copy to `{revision_root}/{revision_id}/{sanitized original filename}`, verify the copy, update the immutable revision, and enqueue the unique conversion job in one repository transaction.

- [ ] **Step 4: Connect download completion to ingestion and bound errors**

The route verifies the bearer before reading the path, accepts only integer IDs and a path of at most 1,024 characters, and maps containment/type/size failures to `422` while recording a concise review error without exposing full exception traces. A repeated completion returns the original result with HTTP 200.

- [ ] **Step 5: Add signature-mismatch and revised-source cases, run checks, and commit**

Add tests proving an observed size different from metadata enters review, a different SHA-256 for the same matched lecture becomes `PROPOSED`, and the first high-confidence source becomes eligible for automatic processing only when `canvas_auto_process=True`.

Run: `python -m pytest tests/canvas/test_ingestion.py tests/canvas/test_download_api.py tests/files/test_office_security.py -q`

Expected: all tests pass.

```bash
git add src/oms_hub/canvas tests/canvas
git commit -m "feat: ingest immutable Canvas revisions"
```

### Task 8: Microsoft Office Conversion Boundary

**Files:**
- Create: `src/oms_hub/files/office.py`
- Test: `tests/files/test_office.py`
- Test: `tests/files/test_office_windows.py`

**Interfaces:**
- Produces protocol `OfficeConverter.convert(source: Path, destination: Path) -> None`.
- Produces `SerialOfficeConverter(timeout_seconds: int)` and exception types `OfficeUnavailableError`, `OfficeTimeoutError`, and `OfficeConversionError`.
- `tests/files/test_office_windows.py` is marked `skipif(sys.platform != "win32")` and is run manually on the NUC with synthetic documents.

- [ ] **Step 1: Write failing platform, serialization, and owned-cleanup tests**

Use injected fake `powerpoint_factory`, `word_factory`, and `executor` dependencies. Tests must prove `.ppt/.pptx` selects PowerPoint, `.doc/.docx` selects Word, `.pdf` is rejected by the Office adapter, two calls cannot overlap, a timeout raises `OfficeTimeoutError`, `Quit()` is called only on the injected instance, and a pre-existing Office process is never enumerated or terminated.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/files/test_office.py -q`

Expected: import fails because `office.py` does not exist.

- [ ] **Step 3: Implement the adapter behind injected factories**

Use a process-wide `threading.Lock`, a one-worker `ThreadPoolExecutor`, and `future.result(timeout=timeout_seconds)`. On Windows, lazily import `pythoncom` and `win32com.client`; call `pythoncom.CoInitialize()`/`CoUninitialize()` in the worker. Use `DispatchEx("PowerPoint.Application")`, `Presentations.Open(str(source), WithWindow=False)`, `SaveAs(str(destination), 32)`, and use `DispatchEx("Word.Application")`, `Documents.Open(str(source), ReadOnly=True)`, `ExportAsFixedFormat(str(destination), 17)`. Set application visibility false and alerts off, close the opened document/presentation in `finally`, then call `Quit()` only on that `DispatchEx` instance. On non-Windows raise `OfficeUnavailableError` before importing pywin32.

- [ ] **Step 4: Add the NUC-only smoke fixture and run portable checks**

The Windows test creates one-page `.pptx` and `.docx` fixtures through their respective `DispatchEx` applications, converts them, validates both PDFs with `validate_pdf`, and closes only its own applications. Mark it `@pytest.mark.windows_office` and register the marker in `pyproject.toml`.

Run: `python -m pytest tests/files/test_office.py -q`

Expected: portable unit tests pass; Windows smoke test remains deselected.

```bash
git add src/oms_hub/files/office.py tests/files pyproject.toml
git commit -m "feat: add isolated Office PDF conversion"
```

### Task 9: Processing Orchestrator, Promotion, and Checklist Updates

**Files:**
- Create: `src/oms_hub/canvas/pipeline.py`
- Modify: `src/oms_hub/canvas/repository.py`
- Modify: `src/oms_hub/scheduler.py`
- Modify: `src/oms_hub/app.py`
- Test: `tests/canvas/test_pipeline.py`
- Test: `tests/canvas/test_recovery.py`

**Interfaces:**
- Produces `CanvasPipeline.run_next() -> bool`, `CanvasPipeline.process_revision(revision_id: int) -> PipelineResult`, and `CanvasPipeline.recover_abandoned_jobs() -> RecoveryReport`.
- Consumes `OfficeConverter`, `validate_pdf`, `verified_atomic_copy`, `build_paths`, repository job claims, and `CatalogRepository.set_step_status`.

- [ ] **Step 1: Write failing first-source, PQ, and revision tests with a fake converter**

```python
class FakeConverter:
    def convert(self, source: Path, destination: Path) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as stream:
            writer.write(stream)


def stored_step(catalog, lecture_id: int, name: str):
    lecture = catalog.get_lecture(lecture_id)
    assert lecture is not None
    return next(item for item in lecture.steps if item.name == name)


def test_new_lecture_promotes_all_outputs_and_updates_steps(pipeline, catalog, lecture_id, lecture_revision) -> None:
    result = pipeline.process_revision(lecture_revision.id)
    assert result.state == "current"
    assert result.paths.local_source.exists()
    assert result.paths.local_pdf.exists()
    assert result.paths.icloud_pdf.exists()
    assert stored_step(catalog, lecture_id, "canvas_pptx_found").status == "complete"
    assert stored_step(catalog, lecture_id, "pptx_downloaded").status == "complete"
    assert stored_step(catalog, lecture_id, "pdf_filed").status == "complete"
    assert stored_step(catalog, lecture_id, "goodnotes_delivered").detail.startswith("Staged for import:")


def test_changed_lecture_stays_proposed_and_does_not_replace_current(pipeline, current_final_pdf, changed_revision) -> None:
    old_pdf = current_final_pdf.read_bytes()
    result = pipeline.process_revision(changed_revision.id)
    assert result.state == "proposed"
    assert current_final_pdf.read_bytes() == old_pdf


def test_pq_does_not_complete_notebooklm_upload_step(pipeline, catalog, pq_lecture_id, pq_revision) -> None:
    pipeline.process_revision(pq_revision.id)
    assert stored_step(catalog, pq_lecture_id, "practice_questions_uploaded").status == "waiting"
```

- [ ] **Step 2: Run pipeline tests and verify failure**

Run: `python -m pytest tests/canvas/test_pipeline.py -q`

Expected: import fails because `pipeline.py` does not exist.

- [ ] **Step 3: Implement serial job claiming, conversion, validation, and promotion**

`run_next` atomically claims the oldest queued job by changing it to `RUNNING` and incrementing attempts. Before calling `build_paths`, `process_revision` creates `effective_settings = settings.model_copy(update={"study_root": Path(connection.study_root), "icloud_staging_root": Path(connection.icloud_staging_root)})` when confirmed database roots exist. It then builds paths, copies the original into revision staging, converts Office sources or copies PDFs, validates the staged PDF, records artifact checksums, and then:

- for a first high-confidence lecture: atomically copy source/PDF to local, PDF to iCloud, mark artifacts current, and complete the four Canvas checklist steps;
- for a PQ: atomically copy validated PDF to local/iCloud Practice Questions without changing `practice_questions_uploaded`;
- for a lecture with an existing current artifact: mark the revision `PROPOSED` and stop before any final path;
- on any error: keep staged inputs, mark the job/revision failed or review, and do not modify final paths.

The iCloud checklist detail is `Staged for import: {absolute iCloud path}` for first promotion.

- [ ] **Step 4: Add restart recovery and scheduler lifecycle**

`recover_abandoned_jobs` changes `RUNNING` ingest jobs to `QUEUED` only when their immutable input exists and checksum matches; it changes abandoned conversion/promotion jobs to `NEEDS_REVIEW` because Office/final side effects require inspection. Add a five-second Hub job-runner interval to the existing scheduler, but do not add Canvas discovery polling. Initialize recovery once during FastAPI lifespan startup and stop the runner on shutdown.

- [ ] **Step 5: Run failure-injection, idempotency, and regression tests**

Tests must inject converter failure, invalid PDF, local copy failure, iCloud copy failure, repeated `process_revision`, and worker restart. Assert no partial files, prior finals remain byte-identical, at most one job/artifact per unique key, and concise review errors.

Run: `python -m pytest tests/canvas/test_pipeline.py tests/canvas/test_recovery.py tests/test_phase1_acceptance.py -q`

Expected: all tests pass.

```bash
git add src/oms_hub/canvas src/oms_hub/scheduler.py src/oms_hub/app.py tests/canvas
git commit -m "feat: process and promote Canvas artifacts"
```

### Task 10: Revision Approval, Keep, Remap, and Rollback

**Files:**
- Modify: `src/oms_hub/canvas/pipeline.py`
- Modify: `src/oms_hub/canvas/repository.py`
- Test: `tests/canvas/test_revision_workflow.py`

**Interfaces:**
- Produces `approve_replacement(revision_id: int) -> PromotionResult`, `keep_current(revision_id: int) -> None`, `remap_source(source_item_id: int, lecture_id: int) -> None`, and `retry_revision(revision_id: int) -> None`.

- [ ] **Step 1: Write failing workflow tests**

Tests must prove approval archives current artifact flags and atomically replaces local/iCloud bytes, updates revision state to `CURRENT`, and sets Goodnotes detail exactly `Updated PDF staged; Goodnotes re-import may be required`; keep marks the exact signature `KEPT` and prevents repeat review; remap recalculates all proposed paths but preserves original immutable bytes; a failed approval restores prior local/iCloud bytes and current flags; retry refuses an invalid/missing staged source.

- [ ] **Step 2: Run workflow tests and verify failure**

Run: `python -m pytest tests/canvas/test_revision_workflow.py -q`

Expected: methods are missing.

- [ ] **Step 3: Implement approval with recoverable backups**

Before promotion, copy each existing final to a sibling `.oms-backup-{revision_id}` and verify its checksum. Promote the new source/local PDF/iCloud PDF, then update artifact/revision current flags in one database transaction. If any promotion or transaction fails, restore every verified backup with `os.replace`, leave the proposed revision in `NEEDS_REVIEW`, and retain its staged artifacts. Delete only the temporary promotion backups after a successful database commit; immutable revision artifacts remain forever.

- [ ] **Step 4: Implement keep, remap, and retry guards**

Keep operates on the exact `remote_signature`; future metadata replay returns `skip`. Remap requires an existing catalog lecture, updates source match/evidence, rebuilds paths, and returns the source to review if a current artifact already exists at the new target. Retry accepts only `FAILED`/`NEEDS_REVIEW`, verifies the original checksum, and enqueues one action job.

- [ ] **Step 5: Run checks and commit**

Run: `python -m pytest tests/canvas/test_revision_workflow.py tests/canvas/test_pipeline.py -q`

Expected: all tests pass.

```bash
git add src/oms_hub/canvas tests/canvas
git commit -m "feat: approve and remap Canvas revisions"
```

### Task 11: Canvas Setup, Status, Preview, and Daily Review UI

**Files:**
- Create: `src/oms_hub/web/canvas_routes.py`
- Create: `src/oms_hub/web/templates/canvas_setup.html`
- Create: `src/oms_hub/web/templates/canvas_review.html`
- Modify: `src/oms_hub/web/routes.py`
- Modify: `src/oms_hub/web/templates/base.html`
- Modify: `src/oms_hub/web/templates/dashboard.html`
- Modify: `src/oms_hub/web/templates/review.html`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/app.py`
- Test: `tests/canvas/test_canvas_web.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Produces pages `/canvas/setup` and `/canvas/review` plus form actions for pair code creation, mapping save, path confirmation, discovery mode, auto-process enable, scan request, approve, keep, remap, retry, and revoke.
- Consumes pairing, repository, pipeline, and the Phase 1 catalog.

- [ ] **Step 1: Write failing web tests for the full setup gate**

Tests must assert the setup page shows seven ordered readiness checks: extension pairing, exactly eight course mappings, local root, iCloud root, discovery scan completed, preview confirmation, and automatic processing. Submitting automatic processing before all preceding checks returns 409 and leaves it false. Course mapping accepts only the approved eight subjects and unique Canvas IDs. The route detects likely Windows iCloud Drive roots from `%USERPROFILE%\iCloudDrive` and `%USERPROFILE%\iCloudDrive\iCloud~com~apple~CloudDocs`, displays only roots that exist, and always requires explicit selection/confirmation. Path confirmation creates and write-probes roots using a temporary file that is removed.

- [ ] **Step 2: Write failing status and review-action tests**

Assert the dashboard displays connected/last heartbeat/last successful scan/item counts, scan in progress, login required, disconnected, and last bounded error states. Assert review cards include concise evidence, Canvas source link, proposed lecture, local/iCloud destinations, and only valid actions for unmatched/classification/revision/conversion/destination cases. Verify POST actions use 303 redirects and invalid state transitions return 409.

- [ ] **Step 3: Run web tests and verify failure**

Run: `python -m pytest tests/canvas/test_canvas_web.py tests/test_dashboard.py -q`

Expected: Canvas routes/templates are missing.

- [ ] **Step 4: Implement the setup and status routes**

Use server-rendered forms consistent with Phase 1. The setup sequence cannot be skipped. `Scan now` stores a one-shot timestamp/nonce read and cleared by the next extension config request. Discovery preview groups `lecture`, `practice_questions`, `ignore`, and `review` with proposed destinations but provides no download button. Only the explicit final form changes `canvas_auto_process` to true.

- [ ] **Step 5: Implement review queues and safe POST actions**

Render separate sections for unmatched, uncertain, proposed replacements, conversion/validation failures, and destination conflicts. Use `urlparse` to allow source links only on `https://lmunet.instructure.com`; render other values as text. Forms call the Task 10 methods, require integer IDs, and surface domain errors as a friendly conflict page rather than a traceback.

- [ ] **Step 6: Run browser-facing tests and commit**

Run: `python -m pytest tests/canvas/test_canvas_web.py tests/test_dashboard.py tests/test_phase1_acceptance.py -q`

Expected: all tests pass.

```bash
git add src/oms_hub/web src/oms_hub/app.py tests/canvas/test_canvas_web.py tests/test_dashboard.py
git commit -m "feat: add Canvas setup and review dashboard"
```

### Task 12: Windows Startup, Installation, Documentation, and Acceptance

**Files:**
- Modify: `scripts/start-hub.ps1`
- Modify: `scripts/install-windows.ps1`
- Create: `scripts/install-canvas-extension.ps1`
- Create: `docs/canvas-extension-install.md`
- Create: `docs/phase-2-nuc-rollout.md`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `src/oms_hub/cli.py`
- Test: `tests/test_windows_scripts.py`
- Test: `tests/canvas/test_phase2_acceptance.py`

**Interfaces:**
- Produces CLI commands `canvas-status`, `canvas-worker-once`, and `canvas-recover` for diagnostics; normal use remains dashboard-driven.
- Produces a startup flow that starts Chrome only when no Chrome process exists, opens the Canvas base URL, starts the Hub, and relies on the extension alarm for scans.

- [ ] **Step 1: Write failing script-text and end-to-end acceptance tests**

The script test reads PowerShell text and asserts it uses `Get-Process -Name chrome -ErrorAction SilentlyContinue`, calls `Start-Process` only inside the no-process branch, uses the detected Chrome executable and Canvas URL, never uses `Stop-Process`, and starts the Hub afterward. The acceptance test uses a fake extension request, fake download, fake converter, temporary study/iCloud/revision roots, and a real SQLite database to prove:

1. new Neuro lecture `.pptx` reaches canonical local PPTX/PDF and iCloud PDF;
2. duplicate professor PDF is ignored;
3. PQ `.docx` reaches both Practice Questions folders as PDF;
4. replay creates no duplicates;
5. EPC unique topic matches and ambiguous topic reviews;
6. changed lecture remains proposed until approval;
7. approval replaces finals while preserving both immutable revisions; and
8. login-required heartbeat appears in connection status.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python -m pytest tests/test_windows_scripts.py tests/canvas/test_phase2_acceptance.py -q`

Expected: startup assertions and Phase 2 acceptance fail.

- [ ] **Step 3: Update Windows startup and installer**

In `start-hub.ps1`, detect Chrome from the two explicit paths `$env:ProgramFiles\Google\Chrome\Application\chrome.exe` and `${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe`; if neither exists, log a clear warning. Only when `Get-Process -Name chrome -ErrorAction SilentlyContinue` returns nothing, call `Start-Process -FilePath $chromePath -ArgumentList 'https://lmunet.instructure.com/'`, then start `oms-hub.exe serve` as today.

The installer creates `%USERPROFILE%\Downloads\OMSStudyHub\CanvasInbox`, `C:\ProgramData\OMSStudyHub\artifacts\revisions`, and `%USERPROFILE%\Documents\OMS II`; preserves an existing `.env`; installs Windows dependencies; and registers startup in the signed-in user context. `install-canvas-extension.ps1` prints the exact Developer Mode/load-unpacked path and does not modify Chrome policy or profiles.

- [ ] **Step 4: Add operational documentation and `.env.example`**

Document: Git pull/venv upgrade, unpacked extension loading, pairing, eight mappings, local/iCloud root selection, Neuro discovery-only validation, auto-processing enablement, staged Goodnotes import meaning, revision approval, Canvas login recovery after LockDown Browser resets cookies, Office signed-in-session requirement, logs/status commands, extension update procedure, and rollback by disabling the extension plus `OMS_HUB_CANVAS_AUTO_PROCESS=false`. Include this exact rollout order: Neuro discovery-only, Neuro automatic, remaining seven courses, controlled revision, NUC restart.

- [ ] **Step 5: Run the complete verification gate**

Run: `python -m pytest -q`

Expected: all Python tests pass; Windows Office smoke test is skipped off Windows.

Run: `python -m ruff check src tests && python -m mypy src/oms_hub`

Expected: both commands exit 0.

Run: `cd extension/canvas-hub && node --test`

Expected: all extension tests pass.

Run on the NUC after deployment from `C:\Services\oms-study-automation`: `.\.venv\Scripts\python.exe -m pytest -m windows_office tests/files/test_office_windows.py -q`

Expected: the PowerPoint and Word smoke conversions each produce a valid one-page-or-greater PDF without closing any pre-existing Office window.

- [ ] **Step 6: Commit the rollout and acceptance gate**

```bash
git add scripts docs .env.example README.md src/oms_hub/cli.py tests/test_windows_scripts.py tests/canvas/test_phase2_acceptance.py
git commit -m "docs: add Phase 2 Canvas rollout and acceptance"
```

### Task 13: Final Security and Release Verification

**Files:**
- Modify only files required by findings from the commands below.

**Interfaces:**
- Produces a release candidate where the design acceptance criteria and existing Phase 1 behavior pass together.

- [ ] **Step 1: Audit extension permissions and secret handling**

Run: `rg -n 'cookies|<all_urls>|https://\*/|console\.(log|debug).*bearer|Authorization' extension/canvas-hub src/oms_hub`

Expected: no cookie permission, broad host permission, or bearer logging; `Authorization` occurs only in authenticated client/server handling.

- [ ] **Step 2: Audit forbidden scope and destructive behavior**

Run: `rg -n 'grades|submissions|discussions|announcements|quizzes|assignments|Stop-Process|taskkill|rmtree|unlink\(' extension/canvas-hub src/oms_hub scripts`

Expected: matches exist only in explicit deny lists/tests/docs and temporary-file cleanup; there is no Canvas crawl route or Office/Chrome process termination.

- [ ] **Step 3: Exercise clean-database and in-place-schema paths**

Run: `python -m pytest tests/canvas/test_domain_and_schema.py tests/test_phase1_acceptance.py tests/canvas/test_phase2_acceptance.py -q`

Expected: clean database creation, existing Phase 1 tables/data, and Phase 2 flow all pass.

- [ ] **Step 4: Run the full release gate once more**

Run: `python -m pytest -q && python -m ruff check src tests && python -m mypy src/oms_hub && (cd extension/canvas-hub && node --test)`

Expected: every command exits 0.

- [ ] **Step 5: Record the implementation completion commit**

```bash
git status --short
git log --oneline origin/main..HEAD
```

Expected: the worktree is clean and the log contains the Phase 2 task commits. If Step 1–4 required fixes, commit only those reviewed fixes:

```bash
git add src extension scripts tests docs pyproject.toml README.md .env.example
git commit -m "fix: close Phase 2 release findings"
```

Do not push or deploy until the user explicitly chooses the release/deployment action.
