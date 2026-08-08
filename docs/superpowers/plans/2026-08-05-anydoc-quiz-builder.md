# Anydoc Quiz Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn NotebookLM Studio into Quiz Builder, preserve NotebookLM quiz generation, add Anydoc-backed direct practice-question imports with images and answer provenance, and publish quizzes into separate Quizzes and Practice Questions library views.

**Architecture:** Add a format-neutral document-processing package with Anydoc, PPTX provenance, PDF, web, and text adapters. Extend the existing Studio durable-run and review infrastructure with a direct-import workflow, structured extraction, NotebookLM-first missing-answer resolution, and hard verification gates for AI-generated answers. Keep publication and the public quiz player shared, and leave all Anki implementation files unchanged.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/SQLite, Pydantic v2, `firecrawl-anydoc==0.1.3`, python-pptx, PyMuPDF, PDF-Inspector, selectolax, httpx, Pillow, NotebookLM, existing multi-provider LLM adapters, vanilla JavaScript, pytest, Node test runner, mypy, and Ruff.

## Global Constraints

- Support Python `>=3.12,<3.14`; deployment verification must run on Windows with Python 3.12.
- Pin Anydoc to `firecrawl-anydoc==0.1.3` in a `document-processing` optional dependency group until its wheel and corpus gates pass.
- Retain the direct PDF-Inspector optional dependency and the existing `pypdf` fallback.
- Do not remove PowerPoint-to-PDF conversion used by NotebookLM, Goodnotes, and canonical artifacts.
- Do not modify files under `src/oms_hub/anki`, `src/oms_anki_agent`, `tests/anki`, `tests/agent`, or `tests/js/anki.test.js`.
- Existing Anki tests remain an independent regression gate.
- Existing native quiz JSON without content-kind or provenance metadata must remain parseable and publishable.
- Existing NotebookLM Generate Quiz behavior must remain available.
- Direct imports must not attach question documents to NotebookLM unless the user explicitly chooses that action.
- A NotebookLM transport, authentication, quota, or service failure is not a no-answer result and must never trigger fallback answer generation.
- Every fallback AI-generated answer blocks publication until that individual question is explicitly verified; do not add bulk verification.
- Ambiguous answer matches and required unresolved images block publication.
- URL imports accept only HTTP/HTTPS, validate every redirect destination, reject private/loopback/link-local addresses, cap redirects at 4, use a 15-second total timeout with a 5-second connection timeout, and enforce `Settings.max_upload_file_bytes` while streaming.
- Automated tests mock NotebookLM and model providers; no automated test calls a live provider.
- Anydoc-primary routing is enabled per format only after the shadow corpus report has zero known silent question drops, wrong answer pairings, wrong required-image bindings, or lost required page/slide provenance.

---

## File structure

### New document-processing package

- `src/oms_hub/document_processing/domain.py` — immutable source, locator, segment, asset, parsed-document, and processor contracts.
- `src/oms_hub/document_processing/router.py` — content-signature routing, parser mode, fallback selection, and parser diagnostics.
- `src/oms_hub/document_processing/assets.py` — sanitize and atomically persist extracted assets.
- `src/oms_hub/document_processing/anydoc_adapter.py` — lazy Anydoc import and shared-model conversion.
- `src/oms_hub/document_processing/pptx_locator.py` — exact slide boundaries, notes, and media relationship enrichment.
- `src/oms_hub/document_processing/presentation_render.py` — bounded PowerPoint-to-PDF slide renders for charts, grouped shapes, SmartArt, and vector content.
- `src/oms_hub/document_processing/pdf_adapter.py` — page-aware PDF text, classification, images, and OCR-required warnings.
- `src/oms_hub/document_processing/web_adapter.py` — safe stored-HTML normalization and image references.
- `src/oms_hub/document_processing/text_adapter.py` — plain-text and lightweight structured-text conversion.
- `src/oms_hub/document_processing/snapshots.py` — immutable streamed URL snapshots with SSRF protections.
- `src/oms_hub/document_processing/shadow.py` — legacy/Anydoc comparison reports and promotion criteria.
- `src/oms_hub/document_processing/__init__.py` — public package exports only.

### New direct-import components

- `src/oms_hub/study_generation/practice_domain.py` — workflow, source-role, content-kind, answer-provenance, source-reference, draft, and diagnostic types.
- `src/oms_hub/study_generation/practice_contracts.py` — strict Pydantic structured-output contracts.
- `src/oms_hub/study_generation/practice_matching.py` — deterministic question/answer pairing and explicit ambiguity results.
- `src/oms_hub/study_generation/practice_extraction.py` — locality-preserving extraction prompts and one schema-correction retry.
- `src/oms_hub/study_generation/practice_answers.py` — NotebookLM answer parsing and conditional fallback generation.
- `src/oms_hub/study_generation/quiz_import_worker.py` — resumable direct-import stage orchestration.
- `src/oms_hub/study_generation/practice_review.py` — draft editing, verification transitions, publication blockers, and native quiz normalization.

### New review and evaluation surfaces

- `src/oms_hub/web/templates/studio_quiz_review.html` — private question, provenance, ambiguity, and image review.
- `src/oms_hub/web/static/studio_quiz_review.js` — edit, verify, image, preview, and publication controls.
- `scripts/evaluate_anydoc_corpus.py` — deterministic corpus comparison command.

### Existing files changed

- `pyproject.toml`, `.env.example`, and `README.md` — dependency, parser mode, setup, and operator commands.
- `src/oms_hub/config.py` — parser mode and document artifact limits.
- `src/oms_hub/models.py` and `src/oms_hub/migrations.py` — additive run, source, artifact, review, and content-kind persistence.
- `src/oms_hub/llm/domain.py`, `repository.py`, `service.py` — extraction and answer-generation task assignments.
- `src/oms_hub/study_generation/studio_domain.py`, `studio_repository.py`, `studio_service.py`, and `studio_worker.py` — dual workflows and delegation.
- `src/oms_hub/study_generation/notebook.py` — question-level NotebookLM answer query with explicit answered/no-support result.
- `src/oms_hub/study_generation/domain.py`, `native_quiz.py`, and `repository.py` — content kind and publication audit data.
- `src/oms_hub/study_generation/quiz_images.py` — parsed-asset candidates and conservative locator binding.
- `src/oms_hub/slides/pipeline.py` — non-blocking shadow parse and configured Anydoc-primary validation.
- `src/oms_hub/app.py` — construct and wire processors, import services, and workers.
- `src/oms_hub/web/studio_routes.py`, `public_quiz_routes.py`, and `settings_routes.py` — APIs and separate library views.
- `src/oms_hub/web/templates/base.html`, `notebook_studio.html`, `public_quiz_library.html`, and `settings.html` — labels, workflows, library tabs, and model tasks.
- `src/oms_hub/web/static/notebook_studio.js`, `public_quiz_library.js`, `app.css`, and `settings.js` — workflow behavior and presentation.

---

### Task 1: Canonical document contracts and parser router

**Files:**
- Create: `src/oms_hub/document_processing/__init__.py`
- Create: `src/oms_hub/document_processing/domain.py`
- Create: `src/oms_hub/document_processing/router.py`
- Test: `tests/document_processing/test_domain.py`
- Test: `tests/document_processing/test_router.py`

**Interfaces:**
- Consumes: `Path` source snapshots and `Settings.max_upload_file_bytes`.
- Produces: `SourceSnapshot`, `DocumentLocator`, `ParsedAsset`, `ParsedSegment`, `ParsedDocument`, `DocumentProcessor`, `ParserMode`, and `DocumentProcessorRouter.parse(snapshot, asset_root)`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_parsed_document_rejects_duplicate_segment_keys(tmp_path: Path) -> None:
    locator = DocumentLocator(label="slide 1", slide_number=1)
    segment = ParsedSegment("s1", SegmentKind.PARAGRAPH, "Question", locator)
    with pytest.raises(ValueError, match="segment keys must be unique"):
        ParsedDocument(
            source_id="source-1",
            source_sha256="a" * 64,
            source_format="pptx",
            parser_name="fixture",
            parser_version="1",
            segments=(segment, segment),
            assets=(),
            warnings=(),
        )


def test_router_falls_back_and_records_primary_failure(tmp_path: Path) -> None:
    router = DocumentProcessorRouter(
        primary=RaisingProcessor("anydoc failed"),
        fallbacks=(TextFixtureProcessor(),),
        mode=ParserMode.ANYDOC,
    )
    parsed = router.parse(_snapshot(tmp_path, ".txt"), tmp_path / "assets")
    assert parsed.parser_name == "text-fixture"
    assert parsed.warnings == ("primary parser failed: anydoc failed",)
```

- [ ] **Step 2: Run the focused tests and confirm missing-module failures**

Run: `python -m pytest tests/document_processing/test_domain.py tests/document_processing/test_router.py -q`

Expected: collection fails because `oms_hub.document_processing` does not exist.

- [ ] **Step 3: Implement immutable contracts and strict invariants**

```python
class ParserMode(StrEnum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    ANYDOC = "anydoc"


class SegmentKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    NOTE = "note"
    IMAGE = "image"


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    id: str
    title: str
    path: Path
    media_type: str
    sha256: str
    original_url: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentLocator:
    label: str
    page_number: int | None = None
    slide_number: int | None = None
    block_index: int | None = None


class DocumentProcessor(Protocol):
    name: str
    version: str

    def supports(self, snapshot: SourceSnapshot) -> bool:
        raise NotImplementedError

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        raise NotImplementedError
```

`ParsedDocument.__post_init__` must reject missing source files, invalid SHA-256 values, duplicate segment/asset keys, references to unknown assets, and non-positive page/slide numbers. `DocumentProcessorRouter` must support legacy, shadow, and Anydoc-primary modes without swallowing both primary and fallback failures.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/document_processing/test_domain.py tests/document_processing/test_router.py -q`

Expected: all tests pass.

- [ ] **Step 5: Run static checks for the package**

Run: `python -m mypy src/oms_hub/document_processing`

Run: `python -m ruff check src/oms_hub/document_processing tests/document_processing`

Expected: both commands pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/document_processing tests/document_processing
git commit -m "feat: add canonical document processing contracts"
```

### Task 2: Anydoc adapter, asset persistence, and PPTX locator enrichment

**Files:**
- Modify: `pyproject.toml`
- Create: `src/oms_hub/document_processing/assets.py`
- Create: `src/oms_hub/document_processing/anydoc_adapter.py`
- Create: `src/oms_hub/document_processing/pptx_locator.py`
- Create: `src/oms_hub/document_processing/presentation_render.py`
- Create: `tests/document_processing/pptx_factory.py`
- Test: `tests/document_processing/test_anydoc_adapter.py`
- Test: `tests/document_processing/test_pptx_locator.py`

**Interfaces:**
- Consumes: Task 1's `SourceSnapshot` and canonical document contracts.
- Produces: `AnydocProcessor.parse(snapshot, asset_root)`, `PptxLocatorEnricher.enrich(snapshot, parsed)`, `PresentationRenderer.render(source, asset_root)`, and `persist_asset(asset_root, key, media_type, payload)`.

- [ ] **Step 1: Add the exact optional dependency and write failing adapter tests**

```toml
[project.optional-dependencies]
document-processing = [
  "firecrawl-anydoc==0.1.3",
]
```

```python
def test_pptx_keeps_slide_numbers_notes_and_image_origin(tmp_path: Path) -> None:
    source = build_pptx(
        tmp_path / "questions.pptx",
        slides=(
            SlideFixture("Question 1", "Which structure?", note="Answer: A", image=True),
            SlideFixture("Question 2", "Which pathway?", note="Answer: B", image=False),
        ),
    )
    snapshot = snapshot_for(source)
    parsed = AnydocProcessor(PptxLocatorEnricher()).parse(snapshot, tmp_path / "assets")
    assert {segment.locator.slide_number for segment in parsed.segments} == {1, 2}
    assert any(segment.kind is SegmentKind.NOTE for segment in parsed.segments)
    assert parsed.assets[0].locator.slide_number == 1
```

- [ ] **Step 2: Install and verify the pinned binding in the implementation worktree**

Run: `python -m pip install -e ".[dev,document-processing,pdf-inspection]"`

Run: `python -c "import anydoc; print(anydoc.__name__)"`

Expected: editable install succeeds and prints `anydoc`.

- [ ] **Step 3: Run adapter tests and confirm missing-class failures**

Run: `python -m pytest tests/document_processing/test_anydoc_adapter.py tests/document_processing/test_pptx_locator.py -q`

Expected: collection fails because the adapter and locator enricher do not exist.

- [ ] **Step 4: Implement lazy Anydoc conversion and atomic assets**

```python
class AnydocProcessor:
    name = "anydoc"

    def __init__(self, pptx_enricher: PptxLocatorEnricher) -> None:
        self.pptx_enricher = pptx_enricher

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        import anydoc

        document = anydoc.to_document(
            snapshot.path.read_bytes(),
            format=format_from_snapshot(snapshot),
        )
        parsed = convert_anydoc_document(snapshot, document, asset_root)
        return (
            self.pptx_enricher.enrich(snapshot, parsed)
            if parsed.source_format == "pptx"
            else parsed
        )
```

`persist_asset` must validate MIME type, sanitize supported raster images through the existing image safety rules, hash the sanitized bytes, and write under `<asset_root>/<key>-<sha256>.<suffix>` using `verified_atomic_write`. Unsupported object payloads remain diagnostic metadata and are never served.

- [ ] **Step 5: Implement PPTX relationship-based enrichment**

Use `python-pptx` for slide order, notes, shape text, and picture locations, plus ZIP relationship paths for Anydoc asset origin matching. Every emitted PPTX segment must have `slide_number`; notes must use `slide N notes`; embedded images must use `slide N image M`. If an Anydoc asset cannot be matched to one slide, retain it with a warning and no automatic question binding.

- [ ] **Step 6: Add bounded full-slide render candidates**

`PresentationRenderer` must use the existing `OfficeConverter` to create a temporary PDF on Windows, rasterize each page through PyMuPDF, sanitize the PNG, and persist `slide-N-render` assets with exact slide locators. These renders cover visual content that is not stored as a standalone Office image. A renderer-unavailable warning is non-blocking until an extracted question explicitly requires that unresolved visual. Delete the temporary PDF in a `finally` block and enforce the same asset byte/pixel limits as uploaded quiz images.

- [ ] **Step 7: Run focused and package checks**

Run: `python -m pytest tests/document_processing/test_anydoc_adapter.py tests/document_processing/test_pptx_locator.py -q`

Run: `python -m mypy src/oms_hub/document_processing`

Run: `python -m ruff check src/oms_hub/document_processing tests/document_processing pyproject.toml`

Expected: all commands pass.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/oms_hub/document_processing tests/document_processing
git commit -m "feat: parse office documents with anydoc"
```

### Task 3: PDF, text, web, and immutable URL snapshot adapters

**Files:**
- Create: `src/oms_hub/document_processing/pdf_adapter.py`
- Create: `src/oms_hub/document_processing/text_adapter.py`
- Create: `src/oms_hub/document_processing/web_adapter.py`
- Create: `src/oms_hub/document_processing/snapshots.py`
- Modify: `src/oms_hub/document_processing/router.py`
- Test: `tests/document_processing/test_pdf_adapter.py`
- Test: `tests/document_processing/test_text_adapter.py`
- Test: `tests/document_processing/test_web_adapter.py`
- Test: `tests/document_processing/test_snapshots.py`

**Interfaces:**
- Consumes: Task 1 contracts, `inspect_pdf`, PyMuPDF, selectolax, httpx, and `Settings.max_upload_file_bytes`.
- Produces: `PdfProcessor`, `TextProcessor`, `WebProcessor`, `URLSnapshotService.fetch(source_id, title, url) -> SourceSnapshot`, and `URLSnapshotService.fetch_asset(base_url, asset_url, asset_root) -> ParsedAsset`.

- [ ] **Step 1: Write failing PDF and web safety tests**

```python
def test_scanned_pdf_returns_ocr_blocker_instead_of_empty_success(tmp_path: Path) -> None:
    snapshot = scanned_pdf_snapshot(tmp_path)
    parsed = PdfProcessor().parse(snapshot, tmp_path / "assets")
    assert parsed.segments == ()
    assert "OCR required for page 1" in parsed.warnings


@respx.mock
def test_url_snapshot_rechecks_redirect_destination(tmp_path: Path) -> None:
    respx.get("https://professor.example/questions").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    )
    service = URLSnapshotService(tmp_path, max_bytes=1024)
    with pytest.raises(ValueError, match="public address"):
        service.fetch("source-1", "Questions", "https://professor.example/questions")


@respx.mock
def test_web_image_is_snapshotted_with_the_same_ssrf_rules(tmp_path: Path) -> None:
    respx.get("https://professor.example/figure.png").mock(
        return_value=httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})
    )
    asset = URLSnapshotService(tmp_path, max_bytes=1024 * 1024).fetch_asset(
        "https://professor.example/questions",
        "/figure.png",
        tmp_path / "assets",
    )
    assert asset.media_type == "image/png"
    assert asset.path.is_file()
```

- [ ] **Step 2: Run focused tests and confirm missing-module failures**

Run: `python -m pytest tests/document_processing/test_pdf_adapter.py tests/document_processing/test_text_adapter.py tests/document_processing/test_web_adapter.py tests/document_processing/test_snapshots.py -q`

Expected: collection fails for the new adapters.

- [ ] **Step 3: Implement page-aware PDF parsing**

`PdfProcessor` must call `inspect_pdf` first, emit one locator-bearing text segment per non-empty page, extract raster images through PyMuPDF with `page N image M` locators, and add an OCR-required warning for every `pages_needing_ocr` entry. A completely scanned PDF must not return a successful empty parse.

- [ ] **Step 4: Implement text and stored-HTML parsing**

```python
class TextProcessor:
    name = "text"
    version = "1"

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        text = snapshot.path.read_text(encoding="utf-8")
        segments = tuple(
            ParsedSegment(
                key=f"block-{index}",
                kind=SegmentKind.PARAGRAPH,
                text=block.strip(),
                locator=DocumentLocator(f"block {index}", block_index=index),
            )
            for index, block in enumerate(re.split(r"\n\s*\n", text), start=1)
            if block.strip()
        )
        return parsed_document(snapshot, self.name, self.version, segments)
```

`WebProcessor` must parse only the stored snapshot, remove scripts/styles/forms, preserve headings, lists, tables, and visible image references in order, and never execute active content. Relevant HTTP/HTTPS `<img>` sources are resolved against the final snapshotted page URL and downloaded through `URLSnapshotService.fetch_asset`; every redirect receives the same public-address checks, raster content is decoded and sanitized, per-asset and cumulative source limits are enforced, and failed image downloads become warnings rather than substituted content.

- [ ] **Step 5: Implement streamed, immutable URL acquisition**

`URLSnapshotService` must reuse the public-address validation semantics from `StudioService`, resolve and validate each redirect manually, stream into memory under the configured byte cap, derive a safe extension from validated content type, atomically write `<root>/<source_id>/snapshot.<suffix>`, and return the SHA-256 plus final URL. Supported direct-import types are HTML, PDF, plain text, JSON, XML, Word, and PowerPoint.

- [ ] **Step 6: Run focused checks**

Run: `python -m pytest tests/document_processing/test_pdf_adapter.py tests/document_processing/test_text_adapter.py tests/document_processing/test_web_adapter.py tests/document_processing/test_snapshots.py -q`

Run: `python -m mypy src/oms_hub/document_processing`

Run: `python -m ruff check src/oms_hub/document_processing tests/document_processing`

Expected: all commands pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/document_processing tests/document_processing
git commit -m "feat: add safe pdf web and text document adapters"
```

### Task 4: Add direct-import persistence, provenance, and content kind

**Files:**
- Modify: `src/oms_hub/models.py`
- Modify: `src/oms_hub/migrations.py`
- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/studio_domain.py`
- Create: `src/oms_hub/study_generation/practice_domain.py`
- Modify: `src/oms_hub/study_generation/studio_repository.py`
- Test: `tests/study_generation/test_migration.py`
- Test: `tests/study_generation/test_studio_repository.py`
- Test: `tests/study_generation/test_practice_domain.py`

**Interfaces:**
- Consumes: existing Studio source/run identifiers and native quiz records.
- Produces: `QuizWorkflowKind`, `QuizContentKind`, `StudioSourcePurpose`, `ImportSourceRole`, `ImportSourceSelection`, `AnswerProvenance`, `QuestionDraft`, run artifacts, import-source bindings, and repository persistence methods.

- [ ] **Step 1: Write migration and repository tests**

```python
def test_v13_migration_backfills_existing_quiz_and_studio_rows(tmp_path: Path) -> None:
    database = legacy_v12_database(tmp_path)
    database.migrate()
    with database.session() as session:
        lecture_quiz = session.get(PublishedQuizModel, "lecture-token")
        studio_quiz = session.get(PublishedQuizModel, "studio-token")
        assert lecture_quiz.content_kind == "lecture_quiz"
        assert studio_quiz.content_kind == "exam_review"


def test_import_run_persists_ordered_source_roles_and_stage_artifact(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    questions = ready_local_source(repository, "Questions")
    answers = ready_local_source(repository, "Answers")
    run = repository.queue_import_run(
        "Neuro",
        1,
        "Exam review",
        "Neuro",
        1,
        QuizContentKind.PRACTICE_QUESTIONS,
        (
            ImportSourceSelection(questions.id, ImportSourceRole.QUESTIONS),
            ImportSourceSelection(answers.id, ImportSourceRole.ANSWER_KEY),
        ),
    )
    repository.save_run_artifact(run.id, f"parse:{questions.id}", "a" * 64, "{}")
    assert [source.role for source in repository.import_sources(run.id)] == [
        ImportSourceRole.QUESTIONS,
        ImportSourceRole.ANSWER_KEY,
    ]
```

- [ ] **Step 2: Run focused tests and verify schema failures**

Run: `python -m pytest tests/study_generation/test_migration.py tests/study_generation/test_studio_repository.py tests/study_generation/test_practice_domain.py -q`

Expected: failures show missing v13 columns, tables, and domain types.

- [ ] **Step 3: Add enums and immutable draft types**

```python
class QuizWorkflowKind(StrEnum):
    NOTEBOOK_GENERATION = "notebook_generation"
    DIRECT_IMPORT = "direct_import"


class QuizContentKind(StrEnum):
    LECTURE_QUIZ = "lecture_quiz"
    EXAM_REVIEW = "exam_review"
    PRACTICE_QUESTIONS = "practice_questions"


class StudioSourcePurpose(StrEnum):
    NOTEBOOK = "notebook"
    LOCAL_IMPORT = "local_import"


class ImportSourceRole(StrEnum):
    QUESTIONS = "questions"
    ANSWER_KEY = "answer_key"
    SUPPORTING_REFERENCE = "supporting_reference"
    COMBINED = "combined_questions_answers"


class AnswerProvenance(StrEnum):
    PROVIDED_BY_SOURCE = "provided_by_source"
    NOTEBOOKLM = "notebooklm"
    GENERATED_BY_AI = "generated_by_ai"
    MANUALLY_CORRECTED = "manually_corrected"


@dataclass(frozen=True, slots=True)
class ImportSourceSelection:
    source_id: str
    role: ImportSourceRole
    attach_to_notebook: bool = False


class DiagnosticSeverity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class DraftDiagnostic:
    code: str
    message: str
    severity: DiagnosticSeverity


@dataclass(frozen=True, slots=True)
class QuestionSourceRef:
    source_id: str
    segment_key: str
    locator: str


@dataclass(frozen=True, slots=True)
class QuestionDraft:
    question_id: str
    original_identifier: str | None
    stem: str
    choices: tuple[str, ...]
    correct_index: int | None
    rationale: str | None
    image_ref: QuizImageRef | None
    source_refs: tuple[QuestionSourceRef, ...]
    answer_provenance: AnswerProvenance | None
    extraction_confidence: float
    diagnostics: tuple[DraftDiagnostic, ...]
    verification_required: bool
    verified_at: str | None

    @property
    def blocking_diagnostics(self) -> tuple[str, ...]:
        return tuple(
            diagnostic.message
            for diagnostic in self.diagnostics
            if diagnostic.severity is DiagnosticSeverity.BLOCKER
        )
```

`QuestionDraft` must carry `question_id`, `original_identifier`, `stem`, ordered choices, optional correct index/rationale, optional image reference, source references, provenance, confidence, diagnostics, `verification_required`, and `verified_at`.
Add `READY = "ready"` to `StudioSourceState` for verified local snapshots and `AWAITING_REVIEW = "awaiting_review"` to `StudioRunState` for complete drafts with publication blockers or pending human review.

- [ ] **Step 4: Add additive schema**

Add these columns with safe defaults:

- `studio_sources.purpose`: `VARCHAR(30) NOT NULL DEFAULT 'notebook'`
- `studio_sources.snapshot_sha256`: nullable `VARCHAR(64)`
- `studio_sources.media_type`: nullable `VARCHAR(100)`
- `studio_sources.final_url`: nullable `TEXT`
- `studio_runs.workflow_kind`: `VARCHAR(30) NOT NULL DEFAULT 'notebook_generation'`
- `studio_runs.content_kind`: `VARCHAR(30) NOT NULL DEFAULT 'exam_review'`
- `published_quizzes.content_kind`: `VARCHAR(30) NOT NULL DEFAULT 'lecture_quiz'`

Create:

- `studio_import_run_sources(run_id, source_id, source_role, attach_to_notebook, remote_notebook_id, remote_source_id, position)` with unique run/source and run/position constraints.
- `studio_run_artifacts(run_id, artifact_key, signature_sha256, payload_json, provider, model, request_id, created_at, updated_at)` with unique run/artifact key.
- `studio_question_reviews(run_id, question_id, answer_provenance, verification_required, verified_at, source_refs_json, extraction_confidence, diagnostics_json, original_identifier, created_at, updated_at)` with unique run/question.

Bump `LATEST_SCHEMA_VERSION` from 12 to 13. Backfill Studio publications with `exam_review` and lecture publications with `lecture_quiz`. Run the migration twice in the idempotency test.

- [ ] **Step 5: Implement repository methods**

```python
def queue_import_run(
    self,
    subject: str,
    exam_number: int,
    label: str,
    destination_subject: str,
    destination_exam_number: int,
    content_kind: QuizContentKind,
    sources: Sequence[ImportSourceSelection],
) -> StudioRun:
    with self.database.session() as session:
        model = StudioRunModel(
            id=str(uuid4()),
            subject=subject,
            subject_key=normalize_subject(subject),
            exam_number=exam_number,
            destination_subject=destination_subject,
            destination_subject_key=normalize_subject(destination_subject),
            destination_exam_number=destination_exam_number,
            label=label,
            label_key=normalize_subject(label),
            prompt="",
            workflow_kind=QuizWorkflowKind.DIRECT_IMPORT.value,
            content_kind=content_kind.value,
        )
        session.add(model)
        session.flush()
        for position, source in enumerate(sources):
            session.add(
                StudioImportRunSourceModel(
                    run_id=model.id,
                    source_id=source.source_id,
                    source_role=source.role.value,
                    attach_to_notebook=source.attach_to_notebook,
                    position=position,
                )
            )
        session.flush()
        return self._run_domain(session, model)

def save_run_artifact(
    self,
    run_id: str,
    artifact_key: str,
    signature_sha256: str,
    payload_json: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    request_id: str | None = None,
) -> None:
    with self.database.session() as session:
        stored = session.scalar(
            select(StudioRunArtifactModel).where(
                StudioRunArtifactModel.run_id == run_id,
                StudioRunArtifactModel.artifact_key == artifact_key,
            )
        )
        if stored is None:
            stored = StudioRunArtifactModel(run_id=run_id, artifact_key=artifact_key)
            session.add(stored)
        stored.signature_sha256 = signature_sha256
        stored.payload_json = payload_json
        stored.provider = provider
        stored.model = model
        stored.request_id = request_id

def save_question_reviews(self, run_id: str, drafts: Sequence[QuestionDraft]) -> None:
    with self.database.session() as session:
        session.execute(
            delete(StudioQuestionReviewModel).where(
                StudioQuestionReviewModel.run_id == run_id
            )
        )
        session.add_all(
            StudioQuestionReviewModel(
                run_id=run_id,
                question_id=draft.question_id,
                answer_provenance=(
                    draft.answer_provenance.value
                    if draft.answer_provenance is not None
                    else None
                ),
                verification_required=draft.verification_required,
                verified_at=draft.verified_at,
                source_refs_json=json.dumps(
                    [asdict(source_ref) for source_ref in draft.source_refs]
                ),
                extraction_confidence=draft.extraction_confidence,
                diagnostics_json=json.dumps(
                    [asdict(diagnostic) for diagnostic in draft.diagnostics]
                ),
                original_identifier=draft.original_identifier,
            )
            for draft in drafts
        )
```

All writes must be idempotent by run plus artifact/question key. `queue_run` for NotebookLM must retain its current attached-source validation.

- [ ] **Step 6: Run focused and migration checks**

Run: `python -m pytest tests/study_generation/test_migration.py tests/study_generation/test_studio_repository.py tests/study_generation/test_practice_domain.py -q`

Run: `python -m mypy src/oms_hub/models.py src/oms_hub/migrations.py src/oms_hub/study_generation/practice_domain.py src/oms_hub/study_generation/studio_repository.py`

Expected: all commands pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/models.py src/oms_hub/migrations.py src/oms_hub/study_generation/domain.py src/oms_hub/study_generation/studio_domain.py src/oms_hub/study_generation/practice_domain.py src/oms_hub/study_generation/studio_repository.py tests/study_generation
git commit -m "feat: persist quiz import runs and provenance"
```

### Task 5: Add local import source intake and run queueing

**Files:**
- Modify: `src/oms_hub/study_generation/studio_service.py`
- Modify: `src/oms_hub/study_generation/studio_repository.py`
- Modify: `src/oms_hub/web/studio_routes.py`
- Create: `tests/study_generation/test_studio_service.py`
- Test: `tests/v2/test_quiz_builder_routes.py`

**Interfaces:**
- Consumes: Task 3's `URLSnapshotService` and Task 4's source purpose, roles, and import-run repository methods.
- Produces: `StudioService.add_import_file`, `add_import_text`, `add_import_url`, and `queue_import_run`; `/studio/import/sources/*` and `/studio/import/runs` APIs.

- [ ] **Step 1: Write failing intake and route tests**

```python
def test_import_file_is_ready_locally_without_notebook_attachment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = service.add_import_file("Neuro", 1, "Questions", "questions.docx", b"PK fixture")
    assert source.purpose is StudioSourcePurpose.LOCAL_IMPORT
    assert source.state is StudioSourceState.READY
    assert source.remote_source_id is None
    assert source.snapshot_sha256 is not None


def test_queue_import_accepts_separate_question_and_answer_urls(client) -> None:
    questions = ready_import_source(client.app, title="Questions", filename="questions.html")
    answers = ready_import_source(client.app, title="Answers", filename="answers.html")
    response = client.post(
        "/studio/import/runs",
        json={
            "subject": "Neuro",
            "exam_number": 1,
            "label": "Professor practice",
            "destination_subject": "Neuro",
            "destination_exam_number": 1,
            "content_kind": "practice_questions",
            "sources": [
                {"source_id": questions.id, "role": "questions"},
                {"source_id": answers.id, "role": "answer_key"},
            ],
        },
        headers=csrf_headers(client),
    )
    assert response.status_code == 202
```

- [ ] **Step 2: Run focused tests and verify missing-method failures**

Run: `python -m pytest tests/study_generation/test_studio_service.py tests/v2/test_quiz_builder_routes.py -q`

Expected: failures identify absent import methods and routes.

- [ ] **Step 3: Implement local file and text snapshots**

Use the existing suffix allowlist and upload-size checks. Local import methods must write immutable originals, calculate SHA-256 after the verified write, store purpose `local_import`, and transition directly to `ready`; they must never enter `StudioRepository.claim_next`, which remains the NotebookLM attachment queue.

- [ ] **Step 4: Implement URL import through `URLSnapshotService`**

Create the source record first, fetch into its immutable directory, persist final URL/media type/checksum, and mark it ready. On failure, mark the source failed with diagnostic source `source_processing`; do not leave a ready row without a verified payload.

- [ ] **Step 5: Add strict request schemas and CSRF-protected routes**

```python
class ImportRunSourceInput(BaseModel):
    source_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f-]{36}$")]
    role: ImportSourceRole
    attach_to_notebook: bool = False


class ImportRunInput(BaseModel):
    subject: str
    exam_number: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=300)
    destination_subject: str
    destination_exam_number: int = Field(ge=1)
    content_kind: QuizContentKind = QuizContentKind.PRACTICE_QUESTIONS
    sources: list[ImportRunSourceInput] = Field(min_length=1, max_length=50)
```

Reject duplicate source IDs, cross-course sources, non-ready local sources, and imports without a Questions or Combined source.
Only Supporting Reference and Combined sources may set `attach_to_notebook=true`; question-only and answer-key sources remain local.

- [ ] **Step 6: Run focused tests and lint**

Run: `python -m pytest tests/study_generation/test_studio_service.py tests/v2/test_quiz_builder_routes.py -q`

Run: `python -m ruff check src/oms_hub/study_generation/studio_service.py src/oms_hub/web/studio_routes.py tests/study_generation/test_studio_service.py tests/v2/test_quiz_builder_routes.py`

Expected: all commands pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/study_generation/studio_service.py src/oms_hub/study_generation/studio_repository.py src/oms_hub/web/studio_routes.py tests/study_generation/test_studio_service.py tests/v2/test_quiz_builder_routes.py
git commit -m "feat: queue local practice question imports"
```

### Task 6: Configure extraction and answer-generation model assignments

**Files:**
- Modify: `src/oms_hub/llm/domain.py`
- Modify: `src/oms_hub/llm/repository.py`
- Modify: `src/oms_hub/llm/service.py`
- Modify: `src/oms_hub/migrations.py`
- Modify: `src/oms_hub/web/settings_routes.py`
- Modify: `src/oms_hub/web/templates/settings.html`
- Modify: `src/oms_hub/web/static/settings.js`
- Test: `tests/llm/test_repository.py`
- Test: `tests/llm/test_service.py`
- Test: `tests/v2/test_llm_migration.py`
- Test: `tests/v2/test_llm_settings_routes.py`
- Test: `tests/v2/test_llm_settings_ui.py`
- Test: `tests/js/settings.test.js`

**Interfaces:**
- Consumes: existing provider/model assignments and credentials.
- Produces: `LLMTask.QUIZ_EXTRACTION`, `LLMTask.QUIZ_ANSWER_GENERATION`, and `LLMService.generate_text_for_task`.

- [ ] **Step 1: Write failing assignment tests**

```python
def test_quiz_tasks_have_independent_provider_assignments(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.set_assignment(LLMTask.QUIZ_EXTRACTION, ProviderName.OPENROUTER, "deepseek/model")
    repository.set_assignment(LLMTask.QUIZ_ANSWER_GENERATION, ProviderName.OPENAI, "gpt-answer")
    assert repository.assignment(LLMTask.QUIZ_EXTRACTION).model == "deepseek/model"
    assert repository.assignment(LLMTask.QUIZ_ANSWER_GENERATION).model == "gpt-answer"


def test_generate_text_for_task_uses_current_assignment(service, provider) -> None:
    service.generate_text_for_task(
        LLMTask.QUIZ_EXTRACTION,
        "Extract questions",
        "source",
        output_schema={"type": "object"},
    )
    assert provider.generated_requests[0].model == "extractor-model"
```

- [ ] **Step 2: Run focused tests and confirm missing enum failures**

Run: `python -m pytest tests/llm/test_repository.py tests/llm/test_service.py tests/v2/test_llm_migration.py tests/v2/test_llm_settings_routes.py tests/v2/test_llm_settings_ui.py -q`

Expected: failures show the two absent task assignments.

- [ ] **Step 3: Add tasks and task-scoped generation**

```python
class LLMTask(StrEnum):
    TRANSCRIPTS = "transcripts"
    ANKI_CURATION = "anki_curation"
    ACCURACY_REVIEW = "accuracy_review"
    QUIZ_EXTRACTION = "quiz_extraction"
    QUIZ_ANSWER_GENERATION = "quiz_answer_generation"


def generate_text_for_task(
    self,
    task: LLMTask,
    instruction: str,
    input_text: str,
    *,
    output_schema: dict[str, object],
) -> GeneratedText:
    assignment = self.settings.assignment(task)
    return self.generate_text(
        instruction,
        input_text,
        output_schema=output_schema,
        provider=assignment.provider,
        model=assignment.model,
    )
```

Default both new tasks to the existing OpenAI default only when no assignment exists. Never copy a credential into the database.

- [ ] **Step 4: Render and update both assignments in Settings**

Use labels **Quiz question extraction** and **Missing-answer generation**. Reuse the existing provider/model dropdown behavior and secret-safe API. Update task-count text tests from three tasks to five tasks.

- [ ] **Step 5: Run Python and JavaScript tests**

Run: `python -m pytest tests/llm/test_repository.py tests/llm/test_service.py tests/v2/test_llm_migration.py tests/v2/test_llm_settings_routes.py tests/v2/test_llm_settings_ui.py -q`

Run: `node --test tests/js/settings.test.js`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/llm src/oms_hub/migrations.py src/oms_hub/web/settings_routes.py src/oms_hub/web/templates/settings.html src/oms_hub/web/static/settings.js tests/llm tests/v2/test_llm_migration.py tests/v2/test_llm_settings_routes.py tests/v2/test_llm_settings_ui.py tests/js/settings.test.js
git commit -m "feat: configure quiz extraction and answer models"
```

### Task 7: Structured question extraction and deterministic answer matching

**Files:**
- Create: `src/oms_hub/study_generation/practice_contracts.py`
- Create: `src/oms_hub/study_generation/practice_matching.py`
- Create: `src/oms_hub/study_generation/practice_extraction.py`
- Test: `tests/study_generation/test_practice_contracts.py`
- Test: `tests/study_generation/test_practice_matching.py`
- Test: `tests/study_generation/test_practice_extraction.py`

**Interfaces:**
- Consumes: canonical parsed documents, `LLMService.generate_text_for_task`, and Task 4 draft types.
- Produces: `PracticeQuestionExtractor.extract(documents) -> ExtractionResult` and `pair_supplied_answers(questions, answers) -> tuple[QuestionDraft, ...]`.

- [ ] **Step 1: Write failing deterministic matching tests**

```python
def test_exact_question_numbers_pair_before_semantic_matching() -> None:
    drafts = pair_supplied_answers(
        questions=(question("1", "First?"), question("2", "Second?")),
        answers=(answer("2", 1), answer("1", 0)),
    )
    assert [draft.correct_index for draft in drafts] == [0, 1]
    assert all(draft.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE for draft in drafts)


def test_conflicting_answer_entries_create_blocker() -> None:
    drafts = pair_supplied_answers(
        questions=(question("1", "First?"),),
        answers=(answer("1", 0), answer("1", 1)),
    )
    assert drafts[0].correct_index is None
    assert "conflicting supplied answers" in drafts[0].blocking_diagnostics
```

- [ ] **Step 2: Write failing extraction retry test**

```python
def test_extractor_retries_schema_failure_once(structured_generator) -> None:
    structured_generator.responses = ["not-json", valid_extraction_json()]
    result = PracticeQuestionExtractor(structured_generator).extract((parsed_fixture(),))
    assert len(result.questions) == 2
    assert len(structured_generator.requests) == 2
    assert "previous response failed schema validation" in structured_generator.requests[1].instruction
```

- [ ] **Step 3: Run focused tests and verify missing-module failures**

Run: `python -m pytest tests/study_generation/test_practice_contracts.py tests/study_generation/test_practice_matching.py tests/study_generation/test_practice_extraction.py -q`

Expected: collection fails for the new extraction modules.

- [ ] **Step 4: Implement strict extraction contracts**

```python
class ExtractedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_identifier: str | None = Field(default=None, max_length=100)
    stem: str = Field(min_length=1, max_length=10_000)
    choices: list[str] = Field(min_length=2, max_length=8)
    supplied_correct_index: int | None = Field(default=None, ge=0, le=7)
    rationale: str | None = Field(default=None, max_length=20_000)
    source_segment_keys: list[str] = Field(min_length=1, max_length=50)
    candidate_asset_keys: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_identifier: str | None = Field(default=None, max_length=100)
    correct_index: int = Field(ge=0, le=7)
    rationale: str | None = Field(default=None, max_length=20_000)
    source_segment_keys: list[str] = Field(min_length=1, max_length=50)


class ExtractionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[ExtractedQuestion] = Field(min_length=1, max_length=500)
    answers: list[ExtractedAnswer] = Field(default_factory=list, max_length=500)
```

Choices must be distinct after case-folding, and a supplied correct index must be within that question's choice list. Every cited segment and candidate asset key must exist in the canonical source input. `ExtractionResult` stores immutable tuples of the validated questions, answers, raw provider metadata, and extraction diagnostics for persistence by Task 9.

- [ ] **Step 5: Implement bounded locality-preserving prompts**

Serialize segments in source order with source title, role, locator, segment key, text, and nearby asset keys. Chunk on question boundaries or headings under 60,000 input characters. Merge chunk outputs by normalized original identifier and source reference; duplicate conflicting questions become blockers rather than overwrite each other.

- [ ] **Step 6: Implement deterministic pairing**

Normalize identifiers such as `1`, `1.`, `Q1`, and `Question 1`. Pair exact normalized IDs first, then aligned complete source order only when question and answer counts match and neither side has duplicate IDs. Record all remaining answers and questions as unmatched diagnostics. Do not finalize a model-suggested low-confidence pair.

- [ ] **Step 7: Run focused and static checks**

Run: `python -m pytest tests/study_generation/test_practice_contracts.py tests/study_generation/test_practice_matching.py tests/study_generation/test_practice_extraction.py -q`

Run: `python -m mypy src/oms_hub/study_generation/practice_contracts.py src/oms_hub/study_generation/practice_matching.py src/oms_hub/study_generation/practice_extraction.py`

Expected: all commands pass.

- [ ] **Step 8: Commit**

```bash
git add src/oms_hub/study_generation/practice_contracts.py src/oms_hub/study_generation/practice_matching.py src/oms_hub/study_generation/practice_extraction.py tests/study_generation/test_practice_contracts.py tests/study_generation/test_practice_matching.py tests/study_generation/test_practice_extraction.py
git commit -m "feat: extract and pair imported practice questions"
```

### Task 8: NotebookLM-first missing-answer resolution

**Files:**
- Modify: `src/oms_hub/study_generation/notebook.py`
- Create: `src/oms_hub/study_generation/practice_answers.py`
- Test: `tests/study_generation/test_stored_notebook_gateway.py`
- Test: `tests/study_generation/test_practice_answers.py`

**Interfaces:**
- Consumes: imported drafts, selected NotebookLM supporting-source IDs, `LLMTask.QUIZ_ANSWER_GENERATION`, and existing NotebookLM connection errors.
- Produces: `NotebookQuestionResult`, `StoredNotebookLMGateway.answer_studio_question`, and `PracticeAnswerResolver.resolve`.

- [ ] **Step 1: Write failing answer-order and outage tests**

```python
def test_supplied_answer_never_calls_notebook_or_fallback() -> None:
    resolver = PracticeAnswerResolver(FailingNotebook(), FailingFallback())
    resolved = resolver.resolve(provided_draft(), _scope())
    assert resolved.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE


def test_notebook_outage_does_not_call_fallback() -> None:
    fallback = RecordingFallback()
    resolver = PracticeAnswerResolver(RaisingNotebook(NotebookServiceError("offline")), fallback)
    with pytest.raises(NotebookServiceError, match="offline"):
        resolver.resolve(unanswered_draft(), _scope())
    assert fallback.requests == []


def test_supported_no_answer_calls_fallback_and_requires_verification() -> None:
    resolver = PracticeAnswerResolver(NoSupportNotebook(), GeneratedFallback(index=1))
    resolved = resolver.resolve(unanswered_draft(), _scope())
    assert resolved.correct_index == 1
    assert resolved.answer_provenance is AnswerProvenance.GENERATED_BY_AI
    assert resolved.verification_required is True
    assert resolved.verified_at is None
```

- [ ] **Step 2: Run focused tests and confirm missing-interface failures**

Run: `python -m pytest tests/study_generation/test_stored_notebook_gateway.py tests/study_generation/test_practice_answers.py -q`

Expected: failures show absent question-level NotebookLM result and resolver.

- [ ] **Step 3: Implement explicit NotebookLM answer status**

```python
class NotebookQuestionStatus(StrEnum):
    ANSWERED = "answered"
    NO_SUPPORT = "no_support"


@dataclass(frozen=True, slots=True)
class NotebookQuestionResult:
    status: NotebookQuestionStatus
    correct_index: int | None
    rationale: str | None
    evidence: tuple[str, ...]
```

`answer_studio_question` must ensure the course/exam notebook exists, validate selected remote source IDs against that notebook, ask one question with its choices, and parse a strict JSON response. Empty, malformed, or hedged output is a contract failure, not `NO_SUPPORT`.

- [ ] **Step 4: Implement resolver ordering and fallback generation**

The resolver returns supplied answers unchanged. For missing answers it calls NotebookLM. `ANSWERED` becomes provenance `notebooklm` without the special verification gate. Only `NO_SUPPORT` invokes `LLMTask.QUIZ_ANSWER_GENERATION`. Fallback output must include one valid choice index, rationale, evidence list, and uncertainty note; the returned draft always sets `verification_required=True`.

- [ ] **Step 5: Run focused and static checks**

Run: `python -m pytest tests/study_generation/test_stored_notebook_gateway.py tests/study_generation/test_practice_answers.py -q`

Run: `python -m mypy src/oms_hub/study_generation/notebook.py src/oms_hub/study_generation/practice_answers.py`

Expected: all commands pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/study_generation/notebook.py src/oms_hub/study_generation/practice_answers.py tests/study_generation/test_stored_notebook_gateway.py tests/study_generation/test_practice_answers.py
git commit -m "feat: resolve missing quiz answers through notebooklm"
```

### Task 9: Resumable direct-import worker pipeline

**Files:**
- Create: `src/oms_hub/study_generation/quiz_import_worker.py`
- Modify: `src/oms_hub/study_generation/studio_worker.py`
- Modify: `src/oms_hub/study_generation/studio_repository.py`
- Modify: `src/oms_hub/study_generation/studio_domain.py`
- Modify: `src/oms_hub/app.py`
- Test: `tests/study_generation/test_quiz_import_worker.py`
- Test: `tests/study_generation/test_studio_worker.py`

**Interfaces:**
- Consumes: parser router, extractor, matcher, answer resolver, run artifacts, and Studio run claims.
- Produces: `stage_signature(stage, source_hashes, parser_versions, provider_model, prompt_version) -> str`, `QuizImportWorker.run(run) -> None`; `StudioWorker` delegates `direct_import` runs while preserving NotebookLM run behavior.

- [ ] **Step 1: Write failing stage-resume tests**

```python
def test_retry_reuses_completed_parse_artifacts(tmp_path: Path) -> None:
    worker, repository, parser = import_worker(tmp_path, extractor=FailOnceExtractor())
    run = queued_import(repository)
    worker.run(run)
    assert repository.get_run(run.id).state is StudioRunState.RETRYING
    worker.run(repository.claim_next_run())
    assert parser.calls == 2  # one call per source, never reparsed on extraction retry


def test_extraction_signature_changes_with_model_or_source() -> None:
    first = stage_signature(
        "extract",
        source_hashes=("a" * 64,),
        parser_versions=("anydoc:0.1.3",),
        provider_model="openrouter:model-a",
        prompt_version="practice-extraction-v1",
    )
    changed_model = stage_signature(
        "extract",
        source_hashes=("a" * 64,),
        parser_versions=("anydoc:0.1.3",),
        provider_model="openrouter:model-b",
        prompt_version="practice-extraction-v1",
    )
    changed_source = stage_signature(
        "extract",
        source_hashes=("b" * 64,),
        parser_versions=("anydoc:0.1.3",),
        provider_model="openrouter:model-a",
        prompt_version="practice-extraction-v1",
    )
    assert len({first, changed_model, changed_source}) == 3
```

- [ ] **Step 2: Run focused tests and confirm missing-worker failures**

Run: `python -m pytest tests/study_generation/test_quiz_import_worker.py tests/study_generation/test_studio_worker.py -q`

Expected: collection fails because `QuizImportWorker` does not exist.

- [ ] **Step 3: Add explicit import stages**

Add `ACQUIRE`, `PARSE`, `EXTRACT`, `PAIR`, `ANSWER_NOTEBOOK`, `ANSWER_FALLBACK`, `NORMALIZE`, `REVIEW`, and `ACCURACY` values to `StudioRunStage`. Existing NotebookLM stages retain their current values.

- [ ] **Step 4: Implement stage signatures and artifact reuse**

Use source checksums, roles, parser/version, prompt version, provider/model assignments, and stage input artifact hashes to calculate SHA-256 signatures. A matching stored artifact skips that stage. A changed signature deletes only downstream artifacts and question-review rows, never immutable source snapshots or a current publication.

- [ ] **Step 5: Implement orchestration and retry classification**

`QuizImportWorker` processes parse, extract, pair, answer, normalize, and review stages. Before asking NotebookLM for a missing answer, it attaches each Supporting Reference or Combined source whose binding has `attach_to_notebook=true`, persists the resulting notebook/source IDs on that import binding, and reuses those IDs on retries. It sends the question and only those verified remote source IDs to Task 8's answer method. Question-only and answer-key sources are never attached by this path. Provider-auth/model errors fail immediately; retryable provider and SQLite-busy errors use the existing capped exponential retry; structured contract errors receive the one extraction retry from Task 7 and then fail with retained raw response. Runs containing blockers finish in `awaiting_review`, not `failed`.

- [ ] **Step 6: Delegate from `StudioWorker` and wire dependencies**

```python
if run.workflow_kind is QuizWorkflowKind.DIRECT_IMPORT:
    self.import_worker.run(run)
    return True
```

Construct the router, snapshot service, extraction service, answer resolver, and import worker in `create_app`. Keep `StudioWorker._run_source` dedicated to NotebookLM source attachment.

- [ ] **Step 7: Run focused and regression tests**

Run: `python -m pytest tests/study_generation/test_quiz_import_worker.py tests/study_generation/test_studio_worker.py tests/study_generation/test_studio_repository.py -q`

Expected: all tests pass and current NotebookLM worker tests remain unchanged.

- [ ] **Step 8: Commit**

```bash
git add src/oms_hub/study_generation/quiz_import_worker.py src/oms_hub/study_generation/studio_worker.py src/oms_hub/study_generation/studio_repository.py src/oms_hub/study_generation/studio_domain.py src/oms_hub/app.py tests/study_generation/test_quiz_import_worker.py tests/study_generation/test_studio_worker.py
git commit -m "feat: orchestrate resumable practice question imports"
```

### Task 10: Shared question review, editing, verification, and publication gates

**Files:**
- Create: `src/oms_hub/study_generation/practice_review.py`
- Modify: `src/oms_hub/study_generation/studio_repository.py`
- Modify: `src/oms_hub/study_generation/quiz_images.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/web/studio_routes.py`
- Create: `src/oms_hub/web/templates/studio_quiz_review.html`
- Create: `src/oms_hub/web/static/studio_quiz_review.js`
- Modify: `src/oms_hub/web/templates/studio_quiz_preview.html`
- Test: `tests/study_generation/test_practice_review.py`
- Test: `tests/study_generation/test_studio_repository.py`
- Test: `tests/v2/test_quiz_builder_routes.py`
- Test: `tests/js/studio_quiz_review.test.js`

**Interfaces:**
- Consumes: imported `QuestionDraft` rows and existing native quiz/image publication services.
- Produces: `PracticeReviewService.update_question`, `verify_generated_answer`, `blockers`, and `to_native_quiz`; review/edit/verify endpoints.

- [ ] **Step 1: Write failing hard-gate tests**

```python
def test_generated_answer_blocks_until_same_question_is_verified(review_service) -> None:
    run_id = review_service.store((generated_draft("q1"), supplied_draft("q2")))
    assert review_service.blockers(run_id) == ("q1: AI-generated answer requires verification",)
    with pytest.raises(ValueError, match="requires verification"):
        review_service.to_native_quiz(run_id)
    review_service.verify_generated_answer(run_id, "q1")
    assert review_service.blockers(run_id) == ()


def test_editing_generated_answer_clears_prior_verification(review_service) -> None:
    run_id = review_service.store((generated_draft("q1"),))
    review_service.verify_generated_answer(run_id, "q1")
    review_service.update_question(run_id, "q1", corrected_input())
    assert review_service.question(run_id, "q1").verified_at is None
```

- [ ] **Step 2: Run focused tests and confirm missing-service failures**

Run: `python -m pytest tests/study_generation/test_practice_review.py tests/v2/test_quiz_builder_routes.py -q`

Expected: collection fails for the new review service.

- [ ] **Step 3: Implement edit and verification state transitions**

Allow editing stem, 2–8 distinct choices, correct index, rationale, topic, area, learning objective, and chosen image. Preserve source references and original identifier. Editing an answer or choices changes provenance to `manually_corrected`; if the prior answer was generated, clear `verified_at` and retain `verification_required=True`. Verification accepts only a complete generated/manual answer and records UTC timestamp.

- [ ] **Step 4: Implement blockers and native normalization**

Block on missing/conflicting answers, unmatched answer diagnostics, unverified generated answers, required unresolved images, schema-invalid choices, and enabled medical-accuracy failures. Rank parsed image candidates by exact source and page/slide locator first, then segment adjacency; auto-bind only when one candidate has the unique highest exact match. Offer embedded and full-slide/page render candidates in review, label their origin, and retain ambiguity when multiple candidates tie. `to_native_quiz` creates existing `QuizQuestion` values only after blockers are empty. Do not add private provenance or filesystem paths to `public_quiz_content`.

- [ ] **Step 5: Add review APIs and page**

Add:

- `GET /studio/runs/{run_id}/review`
- `GET /studio/runs/{run_id}/review/data`
- `PATCH /studio/runs/{run_id}/questions/{question_id}`
- `POST /studio/runs/{run_id}/questions/{question_id}/verify-answer`
- Existing image upload/override endpoints under the same review page.

The page must show source locator, answer provenance badge, confidence, blockers, editable fields, image candidates, and a per-question verification button. The publish button remains disabled while server-returned blockers exist. `/runs/{run_id}/images` must redirect to the unified review page for backward compatibility.

- [ ] **Step 6: Make server publication gates authoritative**

`GenerationRepository.publish_reviewed_studio_quiz` must call the review service/repository blocker check inside the publication transaction before copying media or replacing the current version. Client-side disabled buttons are presentation only.
Construct `PracticeReviewService` in `create_app` as `app.state.practice_review` and inject it into the publication route/service; do not instantiate a second repository per request.

- [ ] **Step 7: Run Python and JavaScript tests**

Run: `python -m pytest tests/study_generation/test_practice_review.py tests/study_generation/test_studio_repository.py tests/v2/test_quiz_builder_routes.py -q`

Run: `node --test tests/js/studio_quiz_review.test.js`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/oms_hub/study_generation/practice_review.py src/oms_hub/study_generation/studio_repository.py src/oms_hub/study_generation/quiz_images.py src/oms_hub/study_generation/repository.py src/oms_hub/app.py src/oms_hub/web/studio_routes.py src/oms_hub/web/templates/studio_quiz_review.html src/oms_hub/web/static/studio_quiz_review.js src/oms_hub/web/templates/studio_quiz_preview.html tests/study_generation/test_practice_review.py tests/study_generation/test_studio_repository.py tests/v2/test_quiz_builder_routes.py tests/js/studio_quiz_review.test.js
git commit -m "feat: review and verify imported quiz answers"
```

### Task 11: Rename Studio to Quiz Builder and expose both workflows

**Files:**
- Modify: `src/oms_hub/web/templates/base.html`
- Modify: `src/oms_hub/web/templates/notebook_studio.html`
- Modify: `src/oms_hub/web/static/notebook_studio.js`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/web/studio_routes.py`
- Create: `tests/js/notebook_studio.test.js`
- Modify: `tests/v2/test_quiz_builder_routes.py`

**Interfaces:**
- Consumes: Notebook generation APIs, import source/run APIs, and run status payloads.
- Produces: one Quiz Builder page with Generate Quiz and Import Practice Questions panels.

- [ ] **Step 1: Write failing page and JavaScript tests**

```python
def test_quiz_builder_keeps_generate_and_import_workflows(client) -> None:
    response = client.get("/studio")
    assert response.status_code == 200
    assert "Quiz Builder" in response.text
    assert "Generate Quiz" in response.text
    assert "Import Practice Questions" in response.text
    assert "NotebookLM Studio" not in response.text
```

```javascript
test("import payload preserves explicit question and answer roles", () => {
  const payload = buildImportRunPayload(formFixture());
    assert.deepEqual(payload.sources, [
    { source_id: "questions-id", role: "questions", attach_to_notebook: false },
    { source_id: "answers-id", role: "answer_key", attach_to_notebook: false },
  ]);
});
```

- [ ] **Step 2: Run UI tests and verify current-label failures**

Run: `python -m pytest tests/v2/test_quiz_builder_routes.py -q`

Run: `node --test tests/js/notebook_studio.test.js`

Expected: tests fail because the page has one NotebookLM-only workflow.

- [ ] **Step 3: Implement accessible workflow switching**

Render two real buttons with `aria-pressed` and two panels that remain in the DOM. Generate Quiz retains existing file/text/URL/image attachment, source deletion, filter, select-all, vertically resizable prompt, destination, and run history behavior. Import Practice Questions adds file/text/URL rows with explicit role selects, an **Use in NotebookLM for missing answers** checkbox enabled only for Supporting Reference or Combined roles, and defaults destination to `practice_questions`.

- [ ] **Step 4: Render workflow-aware run status**

Notebook runs continue to show attachment/chat stages. Import runs show parse/extract/pair/answer/review stages and link to **Review questions** whenever review data exists. Preserve polling backoff and already-rendered content on refresh failure.

- [ ] **Step 5: Keep the public route stable while renaming labels**

Retain `/studio` and existing API URLs so bookmarks and deployments do not break. Change navigation, page title, headings, helper text, and error copy from NotebookLM Studio to Quiz Builder where the text refers to the whole feature. Keep NotebookLM named inside Generate Quiz.

- [ ] **Step 6: Run UI tests**

Run: `python -m pytest tests/v2/test_quiz_builder_routes.py -q`

Run: `node --test tests/js/notebook_studio.test.js tests/js/studio_quiz_review.test.js`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/web/templates/base.html src/oms_hub/web/templates/notebook_studio.html src/oms_hub/web/static/notebook_studio.js src/oms_hub/web/static/app.css src/oms_hub/web/studio_routes.py tests/js/notebook_studio.test.js tests/v2/test_quiz_builder_routes.py
git commit -m "feat: turn notebook studio into quiz builder"
```

### Task 12: Separate Quizzes and Practice Questions library views

**Files:**
- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/web/public_quiz_routes.py`
- Modify: `src/oms_hub/web/templates/public_quiz_library.html`
- Modify: `src/oms_hub/web/static/public_quiz_library.js`
- Modify: `src/oms_hub/web/templates/base.html`
- Test: `tests/study_generation/test_repository.py`
- Test: `tests/v2/test_public_quiz_routes.py`
- Test: `tests/js/public_quiz_library.test.js`

**Interfaces:**
- Consumes: Task 4's `QuizContentKind` stored on published quizzes.
- Produces: filtered `published_quizzes(content_kinds: frozenset[QuizContentKind])`, `/public/quizzes`, and `/public/practice-questions` views using one player.

- [ ] **Step 1: Write failing library separation tests**

```python
def test_practice_questions_are_not_listed_as_lecture_quizzes(tmp_path: Path) -> None:
    app, lecture_quiz, practice = published_mixed_app(tmp_path)
    client = TestClient(app)
    quizzes = client.get("/public/quizzes")
    practice_page = client.get("/public/practice-questions")
    assert lecture_quiz.title in quizzes.text
    assert practice.title not in quizzes.text
    assert practice.title in practice_page.text
    assert lecture_quiz.title not in practice_page.text
```

- [ ] **Step 2: Run focused tests and verify mixed-library failure**

Run: `python -m pytest tests/study_generation/test_repository.py tests/v2/test_public_quiz_routes.py -q`

Expected: the current single library lists both records together and the new route is absent.

- [ ] **Step 3: Persist content kind on every publication path**

Lecture generation publishes `lecture_quiz`. Notebook Quiz Builder defaults to `exam_review`. Direct imports use the run's selected kind, defaulting to `practice_questions`. Replacement publication preserves or explicitly updates the successor run's selected kind atomically.

- [ ] **Step 4: Add filtered views with shared rendering**

Refactor grouping into one private helper that accepts allowed kinds and a page title. `/public/quizzes` allows lecture and exam-review kinds. `/public/practice-questions` allows practice-question kind. Quiz content, answer, media, reset, flags, navigation, and progress endpoints remain token-based and unchanged.

- [ ] **Step 5: Add library navigation and JavaScript state isolation**

Render **Quizzes** and **Practice Questions** links in the app navigation and library header. Continue keying browser progress by quiz token and version, so moving a quiz between library views does not merge or discard progress.

- [ ] **Step 6: Run Python and JavaScript tests**

Run: `python -m pytest tests/study_generation/test_repository.py tests/v2/test_public_quiz_routes.py -q`

Run: `node --test tests/js/public_quiz_library.test.js tests/js/public_quiz.test.js`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/study_generation/domain.py src/oms_hub/study_generation/repository.py src/oms_hub/web/public_quiz_routes.py src/oms_hub/web/templates/public_quiz_library.html src/oms_hub/web/static/public_quiz_library.js src/oms_hub/web/templates/base.html tests/study_generation/test_repository.py tests/v2/test_public_quiz_routes.py tests/js/public_quiz_library.test.js
git commit -m "feat: separate quiz and practice question libraries"
```

### Task 13: Add Anydoc shadow evaluation and safe lecture-parser activation

**Files:**
- Modify: `src/oms_hub/config.py`
- Modify: `.env.example`
- Create: `src/oms_hub/document_processing/shadow.py`
- Create: `scripts/evaluate_anydoc_corpus.py`
- Modify: `src/oms_hub/slides/pipeline.py`
- Modify: `src/oms_hub/app.py`
- Test: `tests/document_processing/test_shadow.py`
- Test: `tests/v2/test_slide_pipeline_document_shadow.py`

**Interfaces:**
- Consumes: Task 1 router, current PowerPoint source, and persisted Study Hub data root.
- Produces: `DocumentShadowEvaluator.compare`, JSON corpus reports, and `Settings.document_parser_mode` with `legacy|shadow|anydoc`.

- [ ] **Step 1: Write failing non-blocking shadow tests**

```python
def test_shadow_failure_does_not_fail_slide_filing(tmp_path: Path) -> None:
    pipeline = slide_pipeline(tmp_path, parser_mode="shadow", processor=RaisingProcessor("bad deck"))
    revision = pipeline.process(staged_slide_item(tmp_path))
    assert revision.current is True
    report = json.loads(next((tmp_path / "document-processing" / "shadow").glob("*.json")).read_text())
    assert report["candidate_error"] == "bad deck"


def test_anydoc_mode_falls_back_with_degraded_report(tmp_path: Path) -> None:
    evaluator = DocumentShadowEvaluator(raising_anydoc(), legacy_processor())
    result = evaluator.parse_primary(snapshot_fixture(tmp_path), tmp_path / "assets")
    assert result.document.parser_name == "legacy"
    assert result.degraded is True
```

- [ ] **Step 2: Run focused tests and confirm missing-evaluator failures**

Run: `python -m pytest tests/document_processing/test_shadow.py tests/v2/test_slide_pipeline_document_shadow.py -q`

Expected: collection fails for the shadow evaluator and parser setting.

- [ ] **Step 3: Add validated parser mode configuration**

```python
document_parser_mode: Literal["legacy", "shadow", "anydoc"] = "shadow"
```

Document `.env` as `OMS_HUB_DOCUMENT_PARSER_MODE=shadow`. `shadow` must never block slide filing. `anydoc` may use the legacy fallback and must record degraded status.

- [ ] **Step 4: Implement deterministic comparison reports**

Reports include source checksum, parser names/versions, duration, segment counts by kind, page/slide coverage, notes, tables, assets, warnings, normalized text hashes, candidate error, fallback use, and promotion blockers. Do not include secrets or source document text in reports.

- [ ] **Step 5: Add corpus command**

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_anydoc_corpus.py `
  --root "$env:USERPROFILE\Documents\OMS II" `
  --output "C:\ProgramData\OMSStudyHub\document-processing\corpus-report.json"
```

The command recursively evaluates supported documents read-only, writes one aggregate JSON report atomically, exits 1 when promotion blockers exist, and never changes parser mode.

- [ ] **Step 6: Integrate lecture shadow parsing outside Anki**

After preserving the immutable PPTX, `SlidePipeline` invokes the configured non-Anki parser. Shadow errors are captured in reports. Anydoc-primary mode returns enriched semantic output for downstream main-Hub consumers while leaving PPTX/PDF artifacts and all Anki extraction unchanged.

- [ ] **Step 7: Run focused tests**

Run: `python -m pytest tests/document_processing/test_shadow.py tests/v2/test_slide_pipeline_document_shadow.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/oms_hub/config.py .env.example src/oms_hub/document_processing/shadow.py scripts/evaluate_anydoc_corpus.py src/oms_hub/slides/pipeline.py src/oms_hub/app.py tests/document_processing/test_shadow.py tests/v2/test_slide_pipeline_document_shadow.py
git commit -m "feat: evaluate anydoc safely on lecture documents"
```

### Task 14: End-to-end acceptance, Windows install verification, and operator documentation

**Files:**
- Modify: `README.md`
- Create: `docs/operations/quiz-builder.md`
- Create: `tests/v2/test_quiz_builder_acceptance.py`
- Create: `tests/v2/test_anydoc_release.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: release gates, Windows installation coverage, and complete setup/recovery instructions.

- [ ] **Step 1: Add end-to-end acceptance tests**

```python
def test_direct_import_with_supplied_answers_never_calls_notebook(tmp_path: Path) -> None:
    app = acceptance_app(tmp_path, notebook=FailIfCalledNotebook())
    run = import_question_and_answer_files(app)
    drain_studio_worker(app)
    review = app.state.practice_review.review(run.id)
    assert review.blockers == ()
    assert all(q.answer_provenance is AnswerProvenance.PROVIDED_BY_SOURCE for q in review.questions)


def test_generated_answer_cannot_publish_before_verification(tmp_path: Path) -> None:
    app = acceptance_app(tmp_path, notebook=NoSupportNotebook(), fallback=GeneratedFallback())
    run = import_unanswered_question(app)
    drain_studio_worker(app)
    client = TestClient(app)
    blocked = client.post(f"/studio/runs/{run.id}/publication", headers=csrf_headers(client))
    assert blocked.status_code == 409
    verify = client.post(
        f"/studio/runs/{run.id}/questions/q1/verify-answer",
        headers=csrf_headers(client),
    )
    assert verify.status_code == 200
```

- [ ] **Step 2: Add release-package assertions**

`test_anydoc_release.py` must assert the exact Anydoc pin, Python range, document-processing extra, included new packages/templates/static files, retained PDF-Inspector extra, and absence of changes to Anki package manifests.

- [ ] **Step 3: Add Windows Python 3.12 CI installation job**

The Windows job must run:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,document-processing,pdf-inspection]"
.\.venv\Scripts\python.exe -c "import anydoc, pdf_inspector; print('document processors ready')"
.\.venv\Scripts\python.exe -m pytest tests\document_processing tests\study_generation tests\v2 -q
```

Do not require Microsoft Office in this job; Office automation tests retain the `windows_office` marker.

- [ ] **Step 4: Document installation, model setup, and recovery**

`docs/operations/quiz-builder.md` must include:

- Python 3.12 environment creation.
- Install command with `dev`, `document-processing`, and `pdf-inspection` extras.
- Settings steps for Quiz question extraction, Missing-answer generation, and Accuracy review.
- `OMS_HUB_DOCUMENT_PARSER_MODE` meanings and rollback to `legacy`.
- Corpus-evaluation command and promotion blockers.
- Why NotebookLM failure does not trigger fallback generation.
- How generated-answer verification works.
- Main Hub port 8765 and test Hub port 8787 examples without copying live secrets.

- [ ] **Step 5: Run the complete non-Anki suite**

Run: `python -m pytest tests/document_processing tests/llm tests/study_generation tests/v2 tests/test_progress.py -q`

Run: `node --test tests/js/lecture.test.js tests/js/notebook_studio.test.js tests/js/public_quiz.test.js tests/js/public_quiz_library.test.js tests/js/settings.test.js tests/js/studio_quiz_images.test.js tests/js/studio_quiz_review.test.js tests/js/uploads.test.js`

Expected: all tests pass.

- [ ] **Step 6: Run independent Anki regressions without changing Anki code**

Run: `python -m pytest tests/anki tests/agent -q`

Run: `node --test tests/js/anki.test.js`

Expected: all tests pass. If an Anki test fails, fix only a shared contract regression unless a separate Anki-specific change is approved.

- [ ] **Step 7: Run final static and packaging checks**

Run: `python -m mypy src`

Run: `python -m ruff check src tests`

Run: `python -m pytest tests/v2/test_release_package.py tests/v2/test_notebooklm_release_package.py tests/v2/test_anydoc_release.py -q`

Run: `git diff --check`

Expected: all commands pass and the worktree is clean except for intended changes.

- [ ] **Step 8: Commit**

```bash
git add README.md docs/operations/quiz-builder.md tests/v2/test_quiz_builder_acceptance.py tests/v2/test_anydoc_release.py .github/workflows/ci.yml
git commit -m "docs: verify and operate anydoc quiz builder"
```

## Final implementation review gate

- Confirm `git diff --name-only <base>...HEAD` contains no path under `src/oms_hub/anki`, `src/oms_anki_agent`, `tests/anki`, or `tests/agent` except documentation-only references explicitly approved during review.
- Review the Anydoc corpus report before changing any production host from `shadow` to `anydoc`.
- Exercise Generate Quiz and Import Practice Questions manually on port 8787 with copied test data before touching the live Hub on port 8765.
- Verify a supplied-answer import, a NotebookLM-answer import, and an AI-fallback import.
- Verify the AI-fallback quiz cannot publish before question-level verification.
- Verify both Quizzes and Practice Questions libraries, player navigation, reset, flags, summaries, and media.
- Record the tested Anydoc, PDF-Inspector, Python, provider, and model versions in the release notes.
