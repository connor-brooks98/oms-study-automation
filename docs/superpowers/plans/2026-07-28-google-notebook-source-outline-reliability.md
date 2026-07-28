# Google, Notebook Source, and Outline Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make native lecture quiz and outline generation use live Google credentials, clean revision-safe NotebookLM sources, exactly two selected lecture sources, formatted outline PDFs, and readable Google Docs quiz labels.

**Architecture:** Replace the custom NotebookLM browser-state probe with a small adapter around the pinned library's supported `login` and `auth check` commands, while retaining official OAuth for Google Docs. Activate the existing notebook mapping tables as a durable local registry, validate the two explicit source IDs immediately before every NotebookLM prompt, and render a bounded Markdown subset into ReportLab flowables. Keep the current durable generation stages and native quiz publisher unchanged.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, SQLite, `notebooklm-py==0.7.3`, Google OAuth/Docs API, ReportLab 4, pypdf, pytest, Node's built-in test runner.

## Global Constraints

- Work only in the isolated `codex/native-study-hub-quizzes` worktree and do not merge to `main`.
- Continue using `notebooklm-py==0.7.3`; do not depend on a pre-release version.
- Continue using the official Google Docs API with installed-app OAuth; an API key and service account are out of scope.
- Execute the NotebookLM CLI with an argument list and `shell=False`; never expose its raw output, cookies, OAuth tokens, client IDs, or client secrets.
- NotebookLM titles come from the current canonical filed path stem and contain no internal lecture ID, enum name, fingerprint, or file extension.
- Every outline and quiz prompt sends exactly the selected lecture's current PDF and cleaned transcript IDs.
- Source replacement uploads and binds the new ready source before deleting the superseded source.
- The PDF renderer accepts only a safe Markdown subset and escapes all source text before emitting ReportLab markup.
- Google Docs displays `Lecture N Quiz` with the entire label hyperlinked to the validated native quiz URL.
- The Queue tab, job cancellation, short URLs, new hostnames, and Cloudflare Access changes are deferred.
- Use test-driven development and make a focused commit after each task.

---

### Task 1: Activate the Durable Notebook Source Registry

**Files:**
- Modify: `src/oms_hub/models.py`
- Modify: `src/oms_hub/migrations.py`
- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `tests/study_generation/test_migration.py`
- Modify: `tests/study_generation/test_repository.py`

**Interfaces:**
- Produces: `NotebookMapping` and `NotebookSourceBinding` frozen dataclasses.
- Produces: `GenerationRepository.notebook_mapping(subject_key: str, exam_number: int) -> NotebookMapping | None`.
- Produces: `GenerationRepository.save_notebook_mapping(subject: str, subject_key: str, exam_number: int, remote_notebook_id: str, title: str) -> NotebookMapping`.
- Produces: `GenerationRepository.source_binding(notebook_mapping_id: int, lecture_id: int, source_kind: SourceKind) -> NotebookSourceBinding | None`.
- Produces: `GenerationRepository.bind_source(..., display_title: str) -> NotebookSourceBinding`.
- Consumes: existing `notebook_mappings` and `notebook_source_mappings` tables.

- [ ] **Step 1: Write failing schema and registry tests**

Update the schema assertion to require version 6 and a `display_title` column:

```python
def test_schema_v6_activates_notebook_source_display_titles(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()

    columns = {
        column["name"]
        for column in inspect(database.engine).get_columns(
            "notebook_source_mappings"
        )
    }
    assert "display_title" in columns
    with database.session() as session:
        version = session.execute(
            text("SELECT version FROM schema_version WHERE id = 1")
        ).scalar_one()
    assert version == LATEST_SCHEMA_VERSION == 6
```

Add repository coverage proving one ready binding per notebook/lecture/kind and
preservation of the old binding as superseded:

```python
def test_binding_new_revision_supersedes_prior_ready_source(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    notebook = repository.save_notebook_mapping(
        "Neuro", "neuro", 1, "nb-1", "Neuro · Exam 1"
    )

    first = repository.bind_source(
        notebook.id,
        lecture_id,
        revision_id=10,
        source_kind=SourceKind.LECTURE_PDF,
        source_sha256="a" * 64,
        remote_source_id="remote-old",
        display_title="Lecture 01 - Seizures",
    )
    second = repository.bind_source(
        notebook.id,
        lecture_id,
        revision_id=11,
        source_kind=SourceKind.LECTURE_PDF,
        source_sha256="b" * 64,
        remote_source_id="remote-new",
        display_title="Lecture 01 - Seizures",
    )

    assert first.remote_source_id == "remote-old"
    assert repository.source_binding(
        notebook.id, lecture_id, SourceKind.LECTURE_PDF
    ) == second
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
python -m pytest tests/study_generation/test_migration.py tests/study_generation/test_repository.py -v
```

Expected: failures because schema version 6, `display_title`, the dataclasses,
and registry methods do not exist.

- [ ] **Step 3: Add the schema migration and domain records**

Add a non-null default for fresh and upgraded databases:

```python
class NotebookSourceMappingModel(Base):
    display_title: Mapped[str] = mapped_column(String(500), default="")
```

Set `LATEST_SCHEMA_VERSION = 6` and, before the version early return, inspect
`notebook_source_mappings`; add the column when absent:

```python
source_columns = {
    column["name"]
    for column in inspect(database.engine).get_columns(
        "notebook_source_mappings"
    )
}
if "display_title" not in source_columns:
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE notebook_source_mappings "
                "ADD COLUMN display_title VARCHAR(500) NOT NULL DEFAULT ''"
            )
        )
```

Add frozen dataclasses containing only secret-safe mapping metadata:

```python
@dataclass(frozen=True, slots=True)
class NotebookMapping:
    id: int
    subject: str
    subject_key: str
    exam_number: int
    remote_notebook_id: str
    title: str


@dataclass(frozen=True, slots=True)
class NotebookSourceBinding:
    id: int
    notebook_mapping_id: int
    lecture_id: int
    revision_id: int
    source_kind: SourceKind
    source_sha256: str
    remote_source_id: str
    display_title: str
    state: str
```

- [ ] **Step 4: Implement repository mapping operations**

Normalize subject keys with the same casefold/whitespace rule used by Google
Docs. `bind_source()` must mark all prior `ready` rows for the same notebook,
lecture, and kind as `superseded`, then insert or update the binding for the
given revision:

```python
def bind_source(
    self,
    notebook_mapping_id: int,
    lecture_id: int,
    revision_id: int,
    source_kind: SourceKind,
    source_sha256: str,
    remote_source_id: str,
    display_title: str,
) -> NotebookSourceBinding:
    with self.database.session() as session:
        session.execute(
            update(NotebookSourceMappingModel)
            .where(
                NotebookSourceMappingModel.notebook_mapping_id
                == notebook_mapping_id,
                NotebookSourceMappingModel.lecture_id == lecture_id,
                NotebookSourceMappingModel.source_kind == source_kind.value,
                NotebookSourceMappingModel.state == "ready",
            )
            .values(state="superseded")
        )
        model = session.scalar(
            select(NotebookSourceMappingModel).where(
                NotebookSourceMappingModel.notebook_mapping_id
                == notebook_mapping_id,
                NotebookSourceMappingModel.study_revision_id == revision_id,
                NotebookSourceMappingModel.source_kind == source_kind.value,
            )
        )
        verified_at = datetime.now(UTC).isoformat()
        if model is None:
            model = NotebookSourceMappingModel(
                notebook_mapping_id=notebook_mapping_id,
                lecture_id=lecture_id,
                study_revision_id=revision_id,
                source_kind=source_kind.value,
                source_sha256=source_sha256,
                remote_source_id=remote_source_id,
                display_title=display_title,
                state="ready",
                verified_at=verified_at,
            )
            session.add(model)
        else:
            model.lecture_id = lecture_id
            model.source_sha256 = source_sha256
            model.remote_source_id = remote_source_id
            model.display_title = display_title
            model.state = "ready"
            model.verified_at = verified_at
        session.flush()
        return self._source_binding(model)
```

Validate a 64-character lowercase SHA-256 and a non-empty display title before
writing. Repository reads order by newest verified/created row and return only
`state == "ready"`.

- [ ] **Step 5: Run registry tests**

Run:

```bash
python -m pytest tests/study_generation/test_migration.py tests/study_generation/test_repository.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/models.py src/oms_hub/migrations.py \
  src/oms_hub/study_generation/domain.py \
  src/oms_hub/study_generation/repository.py \
  tests/study_generation/test_migration.py \
  tests/study_generation/test_repository.py
git commit -m "feat: activate NotebookLM source registry"
```

---

### Task 2: Clean NotebookLM Titles and Enforce Two-Source Isolation

**Files:**
- Modify: `src/oms_hub/study_generation/notebook.py`
- Modify: `src/oms_hub/app.py`
- Modify: `tests/study_generation/test_notebook_gateway.py`
- Create: `tests/study_generation/test_stored_notebook_gateway.py`
- Modify: `tests/study_generation/test_worker.py`

**Interfaces:**
- Consumes: Task 1 `NotebookMapping`, `NotebookSourceBinding`, and repository methods.
- Produces: `StoredNotebookLMGateway(storage_path: Path, repository: GenerationRepository)`.
- Produces: `_source_display_title(source: RevisionSource) -> str`, returning `source.path.stem`.
- Preserves: `NotebookGateway.ask(notebook, sources, prompt) -> NotebookAnswer`.

- [ ] **Step 1: Write failing title, reuse, replacement, and isolation tests**

Build an async fake client whose notebook contains sources for two lectures.
Cover these behaviors:

```python
def test_source_upload_uses_canonical_path_stem(tmp_path):
    pdf = revision_source(
        tmp_path / "Lecture 02 - Demyelinating Disease.pdf",
        SourceKind.LECTURE_PDF,
        revision_id=10,
        sha256="a" * 64,
    )
    transcript = revision_source(
        tmp_path / "Lecture 02 - Demyelinating Disease - Transcript.txt",
        SourceKind.CLEANED_TRANSCRIPT,
        revision_id=11,
        sha256="b" * 64,
    )

    sources = gateway.ensure_sources(notebook, 2, pdf, transcript)

    assert fake_client.upload_titles == [
        "Lecture 02 - Demyelinating Disease",
        "Lecture 02 - Demyelinating Disease - Transcript",
    ]
    assert sources.remote_ids == ["pdf-2", "txt-2"]
```

```python
def test_ask_ignores_other_lecture_sources(tmp_path):
    sources = lecture_source_set(
        lecture_id=2,
        pdf_id="pdf-2",
        transcript_id="txt-2",
    )
    fake_client.remote_sources = [
        ready("pdf-1"),
        ready("txt-1"),
        ready("pdf-2"),
        ready("txt-2"),
    ]

    gateway.ask(notebook_ref("nb-1"), sources, prompt_snapshot(tmp_path))

    assert fake_client.chat.calls[-1]["source_ids"] == ["pdf-2", "txt-2"]
```

Also assert:

- an unchanged binding reuses the remote ID and does not upload;
- a changed SHA uploads a replacement, saves the binding, then deletes the old
  remote ID;
- deletion is not attempted if upload or binding persistence fails;
- the selected lecture's legacy
  `OMS-2-cleaned_transcript-<16 hex>` source is removed after replacement;
- another lecture's legacy source is untouched;
- a missing, stale, duplicate, or non-ready selected ID raises
  `SourceIsolationError` before `chat.ask`; and
- both outline and quiz worker paths pass the same `LectureSourceSet`.

- [ ] **Step 2: Run the new gateway tests and verify failure**

Run:

```bash
python -m pytest tests/study_generation/test_notebook_gateway.py \
  tests/study_generation/test_stored_notebook_gateway.py \
  tests/study_generation/test_worker.py -v
```

Expected: failures because the stored gateway has no registry and still uploads
hashed titles.

- [ ] **Step 3: Make notebook reuse mapping-aware**

Change the stored gateway constructor:

```python
class StoredNotebookLMGateway:
    def __init__(
        self,
        storage_path: Path,
        repository: GenerationRepository,
    ) -> None:
        self.storage_path = storage_path
        self.repository = repository
```

`_ensure_notebook()` first checks the stored course/exam mapping. Reuse it only
when its remote ID appears in `client.notebooks.list()`. Otherwise find or
create the canonical `{subject} · Exam {exam_number}` notebook and persist the
new mapping.

Update `create_app()` to pass the existing generation repository to the
gateway.

- [ ] **Step 4: Implement clean, revision-safe source synchronization**

For each `RevisionSource`, use `source.path.stem` as the display title. Look up
the current binding and list remote sources once per `ensure_sources()` call.

Reuse only when:

```python
binding is not None
and binding.revision_id == source.revision_id
and binding.source_sha256 == source.sha256
and binding.remote_source_id in remote_by_id
and _is_ready(remote_by_id[binding.remote_source_id])
```

If the title differs, call
`client.sources.rename(notebook.id, remote_id, display_title)` before reuse.

For a replacement:

```python
uploaded = await client.sources.add_file(
    notebook.id,
    source.path,
    wait=True,
    title=display_title,
)
if not _is_ready(uploaded):
    raise SourceIsolationError("NotebookLM source did not become ready")
saved = self.repository.bind_source(
    notebook_mapping.id,
    source.lecture_id,
    source.revision_id,
    source.kind,
    source.sha256,
    str(uploaded.id),
    display_title,
)
```

Only after `bind_source()` returns, delete the prior bound ID and exact legacy
IDs belonging to that lecture/kind. Never delete the newly uploaded ID.

- [ ] **Step 5: Add the final prompt-time isolation gate**

Immediately before `chat.ask`, list the remote sources and validate the exact
two IDs from `LectureSourceSet`. Reject missing, duplicate, non-ready, or
binding-mismatched IDs. Then call:

```python
selected_ids = sources.remote_ids
if len(selected_ids) != 2 or len(set(selected_ids)) != 2:
    raise SourceIsolationError("exactly two distinct lecture sources are required")
result = await client.chat.ask(
    notebook.id,
    prompt.content,
    source_ids=selected_ids,
)
```

Do not read or mutate NotebookLM browser checkbox state.

- [ ] **Step 6: Run gateway and worker tests**

Run:

```bash
python -m pytest tests/study_generation/test_notebook_gateway.py \
  tests/study_generation/test_stored_notebook_gateway.py \
  tests/study_generation/test_worker.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/study_generation/notebook.py src/oms_hub/app.py \
  tests/study_generation/test_notebook_gateway.py \
  tests/study_generation/test_stored_notebook_gateway.py \
  tests/study_generation/test_worker.py
git commit -m "feat: isolate clean NotebookLM lecture sources"
```

---

### Task 3: Render NotebookLM Markdown as a Structured Outline PDF

**Files:**
- Create: `src/oms_hub/study_generation/outline_markup.py`
- Modify: `src/oms_hub/study_generation/outline.py`
- Create: `tests/study_generation/test_outline_markup.py`
- Modify: `tests/study_generation/test_outline_pdf.py`

**Interfaces:**
- Produces: `OutlineBlock(kind: str, text: str, level: int = 0, marker: str | None = None)`.
- Produces: `parse_outline_blocks(content: str) -> tuple[OutlineBlock, ...]`.
- Produces: `safe_inline_markup(text: str) -> str`, returning escaped ReportLab paragraph markup.
- Consumes: `OutlinePdfRenderer.render(title: str, content: str) -> bytes`.

- [ ] **Step 1: Write failing parser and PDF formatting tests**

Use representative NotebookLM Markdown:

```python
MARKDOWN = """# Neurodegeneration

**Core concept:** protein aggregation

- Alzheimer disease
  - **Amyloid-beta** plaques
  - Tau tangles
1. Identify the syndrome
2. Localize the lesion

***

Use `MRI` when indicated.
"""
```

Assert structural parsing:

```python
def test_outline_parser_preserves_hierarchy_and_emphasis():
    blocks = parse_outline_blocks(MARKDOWN)

    assert [(block.kind, block.level, block.marker) for block in blocks] == [
        ("heading", 1, None),
        ("paragraph", 0, None),
        ("list_item", 0, "•"),
        ("list_item", 1, "•"),
        ("list_item", 1, "•"),
        ("list_item", 0, "1."),
        ("list_item", 0, "2."),
        ("rule", 0, None),
        ("paragraph", 0, None),
    ]
    assert "<b>Core concept:</b>" in safe_inline_markup(
        "**Core concept:** protein aggregation"
    )
```

Assert extracted PDF text contains `Core concept:`, `Amyloid-beta`, and `MRI`
but not `**`, backticks, or a literal `***`. Assert the rule does not increase
the page count by itself.

- [ ] **Step 2: Run formatting tests and verify failure**

Run:

```bash
python -m pytest tests/study_generation/test_outline_markup.py \
  tests/study_generation/test_outline_pdf.py -v
```

Expected: import failure for `outline_markup` and literal Markdown markers in
the current PDF.

- [ ] **Step 3: Implement the bounded block parser**

Preserve original leading whitespace. Recognize:

```python
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LIST_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])\s+(?P<text>.+)$")
_RULE = re.compile(r"^\s*(?:-{3,}|_{3,}|\*{3,})\s*$")
```

Convert every two leading spaces, or one tab, to one nesting level. Join
ordinary continuation lines into the preceding paragraph, but retain list-item
boundaries. Unsupported syntax remains paragraph text.

- [ ] **Step 4: Implement safe inline markup**

Tokenize only:

- `**text**` and `__text__` to `<b>text</b>`;
- `*text*` and `_text_` to `<i>text</i>`; and
- `` `text` `` to `<font name="Courier">text</font>`.

Escape text with `html.escape()` before inserting the allowlisted tags. Reject
unclosed markers as formatting and retain them as escaped readable text. Do
not pass raw NotebookLM HTML to ReportLab.

- [ ] **Step 5: Render blocks with hierarchy**

Replace the line-by-line stripping loop in `OutlinePdfRenderer`:

- headings use level-specific bold styles;
- paragraphs use the existing body style;
- list items use a `Paragraph` with `bulletText=block.marker`, a style whose
  `leftIndent` is `18 + block.level * 18`, and `firstLineIndent=-10`;
- numbered markers remain the original number;
- rules use `HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#AAB3C2"))`;
- blank paragraph boundaries add normal spacing; and
- `***` never creates `PageBreak`.

Keep PDF validation and page numbering unchanged.

- [ ] **Step 6: Run parser, PDF, and outline artifact tests**

Run:

```bash
python -m pytest tests/study_generation/test_outline_markup.py \
  tests/study_generation/test_outline_pdf.py \
  tests/v2/test_outline_artifact.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/oms_hub/study_generation/outline_markup.py \
  src/oms_hub/study_generation/outline.py \
  tests/study_generation/test_outline_markup.py \
  tests/study_generation/test_outline_pdf.py
git commit -m "feat: format NotebookLM outline PDFs"
```

---

### Task 4: Use notebooklm-py's Supported Login and Live Auth Check

**Files:**
- Create: `src/oms_hub/study_generation/notebook_auth.py`
- Modify: `src/oms_hub/study_generation/google_connection.py`
- Modify: `src/oms_hub/study_generation/browser_profile.py`
- Modify: `src/oms_hub/app.py`
- Create: `tests/study_generation/test_notebook_auth.py`
- Modify: `tests/study_generation/test_google_connection.py`

**Interfaces:**
- Produces: `NotebookAuthCheck(connected: bool, message: str | None)`.
- Produces: `NotebookCLIAuth(storage_path: Path, executable: Path | None = None)`.
- Produces: `NotebookCLIAuth.login() -> None`.
- Produces: `NotebookCLIAuth.check() -> NotebookAuthCheck`.
- Consumes: the exact `storage_path` passed to `StoredNotebookLMGateway`.

- [ ] **Step 1: Write failing command and JSON validation tests**

Inject a fake process runner and assert exact commands:

```python
def test_login_uses_system_chrome_and_exact_worker_storage(tmp_path):
    runner = RecordingRunner(returncode=0, stdout="", stderr="")
    auth = NotebookCLIAuth(
        tmp_path / "notebooklm-storage.json",
        executable=tmp_path / "notebooklm.exe",
        runner=runner,
    )

    auth.login()

    assert runner.calls == [[
        str(tmp_path / "notebooklm.exe"),
        "--storage",
        str(tmp_path / "notebooklm-storage.json"),
        "login",
        "--browser",
        "chrome",
    ]]
    assert runner.shell_values == [False]
```

```python
@pytest.mark.parametrize(
    ("payload", "connected"),
    [
        ('{"status":"ok","checks":{"token_fetch":true}}', True),
        ('{"status":"error","checks":{"token_fetch":false}}', False),
        ('{"status":"ok","checks":{"token_fetch":false}}', False),
        ("not-json", False),
    ],
)
def test_live_check_requires_ok_and_token_fetch(tmp_path, payload, connected):
    auth = cli_auth(tmp_path, stdout=payload)
    assert auth.check().connected is connected
```

Also test missing executable, timeout, non-zero return, output larger than the
accepted structured limit, and sanitized messages that contain no raw command
output.

- [ ] **Step 2: Run auth tests and verify failure**

Run:

```bash
python -m pytest tests/study_generation/test_notebook_auth.py \
  tests/study_generation/test_google_connection.py -v
```

Expected: import failure for `NotebookCLIAuth`.

- [ ] **Step 3: Implement the CLI adapter**

Resolve the default executable beside `sys.executable`:

```python
name = "notebooklm.exe" if os.name == "nt" else "notebooklm"
self.executable = executable or Path(sys.executable).with_name(name)
```

Use `subprocess.run()` with:

```python
completed = self.runner(
    arguments,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=timeout_seconds,
    check=False,
    shell=False,
)
```

Use a 330-second login timeout and a 60-second check timeout. Parse at most
64 KiB of stdout. Return only allowlisted diagnostics such as
`NotebookLM login is required`, `NotebookLM login timed out`, or
`NotebookLM authentication could not be verified`.

- [ ] **Step 4: Replace the Playwright NotebookLM probe**

Inject `NotebookCLIAuth` into the Google connection probe. Google Docs retains
`InstalledAppFlow`, but NotebookLM connection becomes:

```python
self.notebook_auth.login()
result = self.notebook_auth.check()
if not result.connected:
    raise RuntimeError(result.message or "NotebookLM login is required")
```

`account_email(GoogleSurface.NOTEBOOK)` calls the same live check and returns
the already stored connected email only after it succeeds. Remove
`launch_google_profile()` from this connection path. Delete
`browser_profile.py` only if no remaining imports exist after the change.

Create one `notebook_storage_path` in `create_app()` and pass it to both
`NotebookCLIAuth` and `StoredNotebookLMGateway`.

- [ ] **Step 5: Run auth and existing Google tests**

Run:

```bash
python -m pytest tests/study_generation/test_notebook_auth.py \
  tests/study_generation/test_google_connection.py \
  tests/v2/test_google_settings_routes.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/oms_hub/study_generation/notebook_auth.py \
  src/oms_hub/study_generation/google_connection.py \
  src/oms_hub/study_generation/browser_profile.py src/oms_hub/app.py \
  tests/study_generation/test_notebook_auth.py \
  tests/study_generation/test_google_connection.py
git commit -m "fix: use live NotebookLM authentication"
```

---

### Task 5: Persist Accurate Google Status and Preflight Generation

**Files:**
- Modify: `src/oms_hub/study_generation/google_connection.py`
- Modify: `src/oms_hub/study_generation/service.py`
- Modify: `src/oms_hub/study_generation/worker.py`
- Modify: `src/oms_hub/study_generation/notebook.py`
- Modify: `src/oms_hub/study_generation/google_docs.py`
- Modify: `src/oms_hub/web/generation_routes.py`
- Modify: `src/oms_hub/web/templates/settings.html`
- Modify: `src/oms_hub/web/static/settings.js`
- Modify: `tests/study_generation/test_google_connection.py`
- Modify: `tests/study_generation/test_service.py`
- Modify: `tests/study_generation/test_worker.py`
- Modify: `tests/v2/test_google_settings_routes.py`
- Modify: `tests/js/settings-generation.test.js`

**Interfaces:**
- Consumes: Task 4 `NotebookCLIAuth.check()`.
- Extends: `GoogleConnectionStatus.oauth_client_configured: bool`.
- Produces: `GoogleConnectionService.require_live() -> GoogleConnectionStatus`.
- Produces: `GoogleConnectionService.invalidate(surface: GoogleSurface, message: str) -> GoogleConnectionStatus`.
- Produces: typed `NotebookAuthenticationError` and `GoogleDocsAuthenticationError`.

- [ ] **Step 1: Write failing status, skip, preflight, and invalidation tests**

Add route coverage:

```python
def test_google_status_reports_saved_client_without_exposing_it(tmp_path):
    app = configured_app(tmp_path)
    save_valid_oauth_client(app.state.google_connection.oauth_clients)

    response = TestClient(app).get("/settings/google/status")

    assert response.json()["oauth_client_configured"] is True
    assert "client_secret" not in response.text
    assert str(tmp_path) not in response.text
```

Add service tests proving:

- `start_interactive()` skips `InstalledAppFlow` when the existing Docs token
  passes a live user-info request;
- it still runs NotebookLM login when only NotebookLM is invalid;
- `require_live()` refuses to queue when NotebookLM token fetch fails;
- a NotebookLM worker auth error marks only NotebookLM failed and pauses the
  job; and
- a Docs auth error marks only Docs failed.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
python -m pytest tests/study_generation/test_google_connection.py \
  tests/study_generation/test_service.py \
  tests/study_generation/test_worker.py \
  tests/v2/test_google_settings_routes.py -v
node --test tests/js/settings-generation.test.js
```

Expected: failures for missing configured state, live preflight, and typed
surface invalidation.

- [ ] **Step 3: Extend the secret-safe status contract**

Add a defaulted field:

```python
@dataclass(frozen=True, slots=True)
class GoogleConnectionStatus:
    state: str
    account_email: str | None
    surfaces: tuple[GoogleSurfaceStatus, ...]
    message: str | None
    oauth_client_configured: bool = False
```

Every status construction derives the boolean from
`self.oauth_clients.status().configured`. Include it in `_google_payload()`.
Never include the client filename or path.

- [ ] **Step 4: Make Connect Google skip valid Docs authorization**

Before starting `InstalledAppFlow`, build credentials from the saved refresh
token and call the existing `_oauth_email()` live. Run OAuth only when that
probe fails. Always run the NotebookLM login during an explicit Connect action,
then call `test()` for both surfaces.

Do not erase a valid surface when the other surface fails. Persist each result
independently and compute the overall state afterward.

- [ ] **Step 5: Preflight new generation and invalidate worker failures**

Change `GenerationService._queue()` from persisted
`self.google.status().state` to:

```python
status = self.google.require_live()
if status.state != "connected":
    raise GenerationPrerequisiteError(
        status.message or "Connect Google in Settings before generating"
    )
```

Wrap NotebookLM auth failures as `NotebookAuthenticationError` and Docs
credential failures as `GoogleDocsAuthenticationError`. Inject the connection
service into `GenerationWorker`; when those typed errors cross the durable
boundary, call `invalidate()` for the matching surface before pausing the job.
Keep transient retry behavior unchanged.

- [ ] **Step 6: Render the persisted OAuth client state**

Add an element with `data-google-client-state` showing **Client file not
saved** initially. Extend `renderGoogleStatus()` so a true
`oauth_client_configured` value shows **Client file saved** after upload and
after page reload. Update the card copy to explain that OAuth is a one-time
Docs setup and Connect Google handles whichever live service needs attention.

Use `textContent` only.

- [ ] **Step 7: Run Python and JavaScript connection tests**

Run:

```bash
python -m pytest tests/study_generation/test_google_connection.py \
  tests/study_generation/test_service.py \
  tests/study_generation/test_worker.py \
  tests/v2/test_google_settings_routes.py -v
node --test tests/js/settings-generation.test.js
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/oms_hub/study_generation/google_connection.py \
  src/oms_hub/study_generation/service.py \
  src/oms_hub/study_generation/worker.py \
  src/oms_hub/study_generation/notebook.py \
  src/oms_hub/study_generation/google_docs.py \
  src/oms_hub/web/generation_routes.py \
  src/oms_hub/web/templates/settings.html \
  src/oms_hub/web/static/settings.js \
  tests/study_generation/test_google_connection.py \
  tests/study_generation/test_service.py \
  tests/study_generation/test_worker.py \
  tests/v2/test_google_settings_routes.py \
  tests/js/settings-generation.test.js
git commit -m "fix: keep Google connection status live"
```

---

### Task 6: Embed Clean Google Docs Quiz Labels

**Files:**
- Modify: `src/oms_hub/study_generation/google_docs.py`
- Modify: `tests/study_generation/test_google_docs.py`

**Interfaces:**
- Preserves: `GoogleDocsGateway.sync_quiz_link(tab, lecture_number, url) -> None`.
- Changes visible text from `Lecture N: <raw URL>` to linked `Lecture N Quiz`.

- [ ] **Step 1: Write a failing request-shape test**

Use a recording Docs service with one empty exam tab:

```python
def test_sync_inserts_readable_label_and_links_entire_phrase(tmp_path):
    docs = RecordingDocs(empty_exam_tab())
    gateway = gateway_for(tmp_path, docs)

    gateway.sync_quiz_link(
        ExamTabRef("doc-1", "tab-1", 1),
        2,
        QUIZ_URL,
    )

    requests = docs.batch_update_body["requests"]
    assert requests[0]["insertText"]["text"] == "Lecture 2 Quiz\n"
    style = requests[1]["updateTextStyle"]
    assert style["range"]["startIndex"] == 1
    assert style["range"]["endIndex"] == 15
    assert style["textStyle"]["link"]["url"] == QUIZ_URL
```

Add replacement coverage proving an existing named range containing a raw URL
is deleted and recreated with the readable label.

- [ ] **Step 2: Run the test and verify failure**

Run:

```bash
python -m pytest tests/study_generation/test_google_docs.py -v
```

Expected: failure because the inserted text contains the raw URL.

- [ ] **Step 3: Update label and link ranges**

Use:

```python
label = f"Lecture {lecture_number} Quiz"
line = f"{label}\n"
```

Link from `insertion_index` through
`insertion_index + len(label)`. Keep the newline outside the hyperlink, and
keep the existing named range around the complete line so idempotent
replacement and lecture ordering remain unchanged.

- [ ] **Step 4: Run Google Docs and worker tests**

Run:

```bash
python -m pytest tests/study_generation/test_google_docs.py \
  tests/study_generation/test_worker.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/oms_hub/study_generation/google_docs.py \
  tests/study_generation/test_google_docs.py
git commit -m "feat: label shared lecture quiz links"
```

---

### Task 7: Release Packaging, NUC Runbook, and Full Verification

**Files:**
- Modify: `docs/native-quizzes-nuc-rollout.md`
- Modify: `README.md`
- Modify: `scripts/build-v2-release.py`
- Modify: `tests/v2/test_notebooklm_release_package.py`
- Modify: `tests/v2/test_notebooklm_acceptance_contract.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a release archive containing `outline_markup.py` and
  `notebook_auth.py`, but no OAuth or NotebookLM credential files.
- Produces: updated NUC rollout and acceptance instructions.

- [ ] **Step 1: Write failing release contract tests**

Extend archive assertions:

```python
def test_release_contains_new_runtime_and_excludes_google_secrets(tmp_path):
    archive = build_release(tmp_path)
    names = archive_names(archive)

    assert "src/oms_hub/study_generation/notebook_auth.py" in names
    assert "src/oms_hub/study_generation/outline_markup.py" in names
    assert all("oauth-client.json" not in name for name in names)
    assert all("notebooklm-storage.json" not in name for name in names)
    assert all("browser-profile" not in name for name in names)
```

Update acceptance strings to require **Client file saved**,
`Lecture N Quiz`, canonical NotebookLM titles, and exact two-source selection.

- [ ] **Step 2: Run release tests and verify failure**

Run:

```bash
python -m pytest tests/v2/test_notebooklm_release_package.py \
  tests/v2/test_notebooklm_acceptance_contract.py -v
```

Expected: failures because the archive/runbook contracts have not been
updated.

- [ ] **Step 3: Update packaging and documentation**

Ensure the release builder includes the complete `src/oms_hub` runtime while
retaining explicit exclusions for:

```text
oauth-client.json
notebooklm-storage.json
browser-profile
*.db
```

Update the rollout guide with:

- branch `codex/native-study-hub-quizzes`;
- service-stop-before-install instructions to avoid the locked
  `oms-hub.exe`;
- the schema version 6 additive migration;
- **Client file saved** behavior;
- one Connect Google action with conditional Docs consent and NotebookLM
  Chrome login;
- a live Test connection check;
- canonical NotebookLM source titles;
- the fact that browser checkboxes do not control the API request;
- outline PDF formatting checks;
- the linked `Lecture N Quiz` Google Docs label; and
- the institution-only Cloudflare Access quiz-path policy already selected by
  the user, never a public Bypass policy.

- [ ] **Step 4: Run focused release tests**

Run:

```bash
python -m pytest tests/v2/test_notebooklm_release_package.py \
  tests/v2/test_notebooklm_acceptance_contract.py -v
```

Expected: all pass.

- [ ] **Step 5: Run the complete verification gate**

Run:

```bash
python -m pytest
node --test tests/js/*.test.js
python -m ruff check .
python -m mypy src/oms_hub
git diff --check
```

Expected:

- all Python tests pass;
- all JavaScript tests pass;
- Ruff reports no violations;
- mypy reports no errors;
- no whitespace errors; and
- `git status --short` contains only the intended documentation changes before
  the final commit.

- [ ] **Step 6: Build and inspect the release archive**

Run:

```bash
python scripts/build-v2-release.py
```

Inspect the produced archive listing. Confirm the new runtime modules exist and
no database, browser profile, OAuth client, refresh token, cookie storage, or
prompt contents are present.

- [ ] **Step 7: Commit**

```bash
git add README.md docs/native-quizzes-nuc-rollout.md \
  scripts/build-v2-release.py \
  tests/v2/test_notebooklm_release_package.py \
  tests/v2/test_notebooklm_acceptance_contract.py
git commit -m "docs: update native quiz connection rollout"
```

- [ ] **Step 8: Re-run the exact pushed-tree gate and push**

Run the complete verification commands once more after the final commit. Then:

```bash
git push origin codex/native-study-hub-quizzes
git status --short --branch
git rev-parse HEAD
```

Expected: the branch tracks `origin/codex/native-study-hub-quizzes`, the
worktree is clean, and local HEAD equals the remote branch commit.
