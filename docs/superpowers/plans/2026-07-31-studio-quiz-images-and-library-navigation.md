# Studio Quiz Images and Library Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an image-aware NotebookLM Studio quiz contract, durable private image review, secure shared-image uploads, final preview and explicit publication, public image delivery, and a persistent Quiz Library navigation link.

**Architecture:** Extend the native quiz domain with an optional immutable image reference while keeping lecture prompts text-only and legacy payloads valid. Persist validated Studio drafts, grouped image requirements, per-question overrides, and published media bindings in SQLite; isolate image decoding and atomic storage in a focused media service. Reuse the existing quiz player for private preview and public delivery, with private Studio routes controlling upload, override, and publication.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Pydantic 2, Pillow 11, Jinja2, vanilla JavaScript, CSS, pytest, and Node's built-in test runner.

## Global Constraints

- The workflow applies only to quizzes generated from NotebookLM Studio prompts; lecture automation keeps its text-only contract and automatic publication.
- Each question supports zero or one image, while one image key may be reused by multiple questions.
- Accept PNG, JPEG, and WebP uploads up to 10 MiB and 40 million decoded pixels; reject animated, malformed, unsupported, truncated, or decompression-bomb inputs.
- Correct EXIF orientation, remove metadata, and re-encode every accepted upload as a lossless PNG.
- A Studio quiz with no image references publishes automatically; a quiz with image references remains private until all references are resolved, previewed, and explicitly published.
- Existing published quiz payloads without `image_ref` remain valid without backfill.
- Never expose local paths, answer keys, raw NotebookLM output, or private image-review metadata through public routes.
- Replacement runs leave the existing published quiz online until the reviewed replacement is explicitly published.
- Keep all implementation on `codex/notebooklm-studio-main-hardening`; do not merge into `main`.

---

### Task 1: Image-aware native quiz contract

**Files:**
- Modify: `src/oms_hub/study_generation/domain.py`
- Modify: `src/oms_hub/study_generation/native_quiz.py`
- Test: `tests/study_generation/test_native_quiz.py`
- Test: `tests/study_generation/test_prompts.py`

**Interfaces:**
- Produces: `QuizImageRef(key, source_title, locator, description)` and `QuizQuestion.image_ref: QuizImageRef | None`.
- Produces: `studio_quiz_prompt(str) -> str` with the Studio-only image contract.
- Produces: `image_requirements(NativeQuiz) -> tuple[QuizImageRef, ...]`, deduplicated in first-question order.
- Preserves: `quiz_prompt(PromptSnapshot)` as the lecture text-only contract.

- [ ] **Step 1: Write failing parser and prompt tests**

Add literal fixtures proving a shared image key becomes the same value object on questions 4-7, a missing field remains `None`, and conflicting metadata fails:

```python
def test_studio_image_reference_is_parsed_and_shared_by_key():
    payload = _payload(image_ref={
        "key": "image-1",
        "source_title": "Dr. Wang's website",
        "locator": "Image immediately before question 4",
        "description": "Reference image used for questions 4-7",
    })
    payload["questions"].append(dict(payload["questions"][0]))
    quiz = parse_native_quiz(json.dumps(payload))

    assert quiz.questions[0].image_ref == QuizImageRef(
        "image-1",
        "Dr. Wang's website",
        "Image immediately before question 4",
        "Reference image used for questions 4-7",
    )
    assert image_requirements(quiz) == (quiz.questions[0].image_ref,)


def test_conflicting_metadata_for_one_image_key_is_rejected():
    payload = _payload(image_ref={
        "key": "image-1", "source_title": "Slides",
        "locator": "Slide 4", "description": "Histology",
    })
    second = dict(payload["questions"][0])
    second["image_ref"] = {
        "key": "image-1", "source_title": "Slides",
        "locator": "Slide 8", "description": "Histology",
    }
    payload["questions"].append(second)

    with pytest.raises(QuizContractError, match="conflicting metadata"):
        parse_native_quiz(json.dumps(payload))


def test_studio_prompt_requests_image_locations_without_changing_lecture_contract():
    studio = studio_quiz_prompt("Preserve all questions verbatim.")
    lecture = quiz_prompt(PromptSnapshot(
        Path("Quiz Prompt.md"), "Create a quiz.", "a" * 64, "2026-07-31T12:00:00Z"
    )).content

    assert '"image_ref": null' in studio
    assert "repeat the exact same key" in studio
    assert "image_ref" not in lecture
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/pytest tests/study_generation/test_native_quiz.py tests/study_generation/test_prompts.py -q`

Expected: FAIL because `QuizImageRef`, `image_requirements`, and the image-aware Studio contract do not exist.

- [ ] **Step 3: Implement the minimal domain and parser changes**

Add the frozen domain value and optional question field:

```python
@dataclass(frozen=True, slots=True)
class QuizImageRef:
    key: str
    source_title: str
    locator: str
    description: str


@dataclass(frozen=True, slots=True)
class QuizQuestion:
    id: str
    stem: str
    choices: tuple[QuizChoice, ...]
    correct_choice_id: str
    rationale: str
    image_ref: QuizImageRef | None = None
```

Add `_ImageRefInput` with `extra="forbid"`, a key pattern of
`^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`, and bounded non-empty metadata.
Set `_QuestionInput.image_ref` to `None` by default. Build the domain value in
`parse_native_quiz`, include it in `serialize_native_quiz`, and validate that
each repeated key has identical metadata before returning the quiz.

Split the constants so `_QUIZ_OUTPUT_CONTRACT` remains unchanged and
`_STUDIO_QUIZ_OUTPUT_CONTRACT` contains the approved `image_ref` shape and
rules. Make only `studio_quiz_prompt` append the Studio constant.

Implement deterministic grouping:

```python
def image_requirements(quiz: NativeQuiz) -> tuple[QuizImageRef, ...]:
    by_key: dict[str, QuizImageRef] = {}
    for question in quiz.questions:
        if question.image_ref is not None:
            by_key.setdefault(question.image_ref.key, question.image_ref)
    return tuple(by_key.values())
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `.venv/bin/pytest tests/study_generation/test_native_quiz.py tests/study_generation/test_prompts.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the contract slice**

```bash
git add src/oms_hub/study_generation/domain.py src/oms_hub/study_generation/native_quiz.py tests/study_generation/test_native_quiz.py tests/study_generation/test_prompts.py
git commit -m "feat: add Studio quiz image references"
```

---

### Task 2: Durable image-review schema and repository

**Files:**
- Modify: `src/oms_hub/models.py`
- Modify: `src/oms_hub/migrations.py`
- Modify: `src/oms_hub/study_generation/studio_domain.py`
- Modify: `src/oms_hub/study_generation/studio_repository.py`
- Test: `tests/study_generation/test_migration.py`
- Create: `tests/study_generation/test_studio_image_review.py`

**Interfaces:**
- Produces: `StudioRunState.AWAITING_IMAGES` and `StudioRunStage.IMAGE_REVIEW`.
- Produces: `StudioQuizImageRequirement` and `StudioQuizReview` domain records.
- Produces: `StudioRepository.await_image_review(run_id, notebook_id, raw_response, quiz)`.
- Produces: `StudioRepository.quiz_review(run_id) -> StudioQuizReview`.
- Produces: `StudioRepository.bind_image(...)`, `set_image_override(...)`, and `resolved_quiz(run_id)`.

- [ ] **Step 1: Write failing migration and repository tests**

Add a schema assertion for version 11, `studio_runs.draft_payload_json`, and the
three new tables. Add repository behavior using a real temporary SQLite file:

```python
def test_image_review_groups_shared_key_and_survives_repository_reload(tmp_path):
    database, studio, run = _queued_run(tmp_path)
    quiz = _quiz_with_shared_image("image-1", question_count=4)

    studio.await_image_review(run.id, "notebook-1", "raw", quiz)
    review = StudioRepository(database).quiz_review(run.id)

    assert review.run.state is StudioRunState.AWAITING_IMAGES
    assert review.run.stage is StudioRunStage.IMAGE_REVIEW
    assert review.requirements[0].image_key == "image-1"
    assert review.requirements[0].question_ids == ("q1", "q2", "q3", "q4")
    assert review.unresolved_keys == ("image-1",)


def test_no_image_override_affects_only_selected_question(tmp_path):
    database, studio, run = _awaiting_run(tmp_path)

    studio.set_image_override(run.id, "q1", True)
    review = studio.quiz_review(run.id)

    assert review.overridden_question_ids == frozenset({"q1"})
    assert review.unresolved_keys == ("image-1",)
    studio.set_image_override(run.id, "q1", False)
    assert studio.quiz_review(run.id).overridden_question_ids == frozenset()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/pytest tests/study_generation/test_migration.py tests/study_generation/test_studio_image_review.py -q`

Expected: FAIL because schema version 11, review models, states, and repository methods are absent.

- [ ] **Step 3: Add additive SQLAlchemy models and migration**

Add nullable `StudioRunModel.draft_payload_json`. Add:

```python
class StudioQuizImageRequirementModel(Base):
    __tablename__ = "studio_quiz_image_requirements"
    __table_args__ = (UniqueConstraint("run_id", "image_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("studio_runs.id"))
    image_key: Mapped[str] = mapped_column(String(64))
    source_title: Mapped[str] = mapped_column(String(500))
    locator: Mapped[str] = mapped_column(String(1000))
    description: Mapped[str] = mapped_column(String(1000))
    asset_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    width: Mapped[int | None] = mapped_column(nullable=True)
    height: Mapped[int | None] = mapped_column(nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)


class StudioQuizImageOverrideModel(Base):
    __tablename__ = "studio_quiz_image_overrides"
    __table_args__ = (UniqueConstraint("run_id", "question_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("studio_runs.id"))
    question_id: Mapped[str] = mapped_column(String(4))
    image_key: Mapped[str] = mapped_column(String(64))


class PublishedQuizMediaModel(Base):
    __tablename__ = "published_quiz_media"
    __table_args__ = (UniqueConstraint("quiz_token", "image_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    quiz_token: Mapped[str] = mapped_column(ForeignKey("published_quizzes.token"))
    image_key: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(50))
    width: Mapped[int]
    height: Mapped[int]
    alt_text: Mapped[str] = mapped_column(String(1000))
```

Set `LATEST_SCHEMA_VERSION = 11`; after `create_schema()`, add the nullable
draft column with `ALTER TABLE` when upgrading an existing database. Tables are
created by SQLAlchemy metadata and require no legacy backfill.

- [ ] **Step 4: Implement immutable review domain and repository behavior**

Add `StudioStoredImage`, `StudioQuizImageRequirement`, and `StudioQuizReview`.
Include `draft_payload_json` on `StudioRun`. In `await_image_review`, serialize
the validated quiz, replace requirement rows for that run, clear overrides,
set notebook/raw response, and set the awaiting state/stage in one session.

`quiz_review` must parse `draft_payload_json`, derive ordered `question_ids`
from the quiz, and compute unresolved keys as keys where at least one
non-overridden question exists and no stored image is bound.

`set_image_override` must verify the question exists and owns an image
reference, insert on `True`, and delete on `False`. `bind_image` must update only
an existing requirement in an awaiting-images run. `resolved_quiz` must raise
`ValueError("quiz images are still required: ...")` until resolved, then return
a copy with overridden question image references removed.

- [ ] **Step 5: Run focused repository tests and verify GREEN**

Run: `.venv/bin/pytest tests/study_generation/test_migration.py tests/study_generation/test_studio_image_review.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the persistence slice**

```bash
git add src/oms_hub/models.py src/oms_hub/migrations.py src/oms_hub/study_generation/studio_domain.py src/oms_hub/study_generation/studio_repository.py tests/study_generation/test_migration.py tests/study_generation/test_studio_image_review.py
git commit -m "feat: persist Studio quiz image reviews"
```

---

### Task 3: Safe image decoding and atomic storage

**Files:**
- Create: `src/oms_hub/study_generation/quiz_images.py`
- Modify: `pyproject.toml`
- Create: `tests/study_generation/test_quiz_images.py`
- Modify: `tests/test_packaging_dependencies.py`

**Interfaces:**
- Produces: `sanitize_quiz_image(payload: bytes) -> SanitizedQuizImage`.
- Produces: `StudioQuizImageService.upload(run_id, image_key, original_filename, payload)`.
- Consumes: `StudioRepository.bind_image(...)` from Task 2.

- [ ] **Step 1: Add Pillow to the environment for the test cycle**

Run: `.venv/bin/pip install 'Pillow>=11,<13'`

Expected: installation succeeds in the branch-local virtual environment.

- [ ] **Step 2: Write failing real-image tests**

Create in-memory PNG, JPEG-with-orientation, animated WebP, and oversized
fixtures using Pillow. Assert observable decoded output rather than internal
calls:

```python
def test_jpeg_is_oriented_stripped_and_saved_as_lossless_png():
    source = _oriented_jpeg(width=2, height=3, orientation=6, comment=b"private")

    sanitized = sanitize_quiz_image(source)
    decoded = Image.open(BytesIO(sanitized.payload))

    assert sanitized.media_type == "image/png"
    assert (sanitized.width, sanitized.height) == (3, 2)
    assert decoded.format == "PNG"
    assert decoded.info == {}


@pytest.mark.parametrize("payload", [b"not an image", _animated_webp()])
def test_unsafe_image_payload_is_rejected(payload):
    with pytest.raises(QuizImageError):
        sanitize_quiz_image(payload)


def test_failed_replacement_keeps_existing_bound_image(tmp_path):
    service, repository, run = _media_service(tmp_path)
    first = service.upload(run.id, "image-1", "first.jpg", _jpeg())

    with pytest.raises(QuizImageError):
        service.upload(run.id, "image-1", "bad.webp", _animated_webp())

    assert repository.quiz_review(run.id).requirements[0].image == first
```

- [ ] **Step 3: Run the image tests and verify RED**

Run: `.venv/bin/pytest tests/study_generation/test_quiz_images.py tests/test_packaging_dependencies.py -q`

Expected: FAIL because the sanitizer/service and Pillow runtime declaration do not exist.

- [ ] **Step 4: Implement safe decoding and storage**

Declare `Pillow>=11,<13` in runtime dependencies. In `quiz_images.py`, reject
empty and over-10-MiB payloads before decoding. Use `Image.open(BytesIO(...))`,
turn decompression warnings into errors, verify format is PNG/JPEG/WEBP,
check `width * height <= 40_000_000` before decoding, reject
`getattr(image, "n_frames", 1) != 1`, call `image.verify()`, reopen, apply
`ImageOps.exif_transpose`, convert
unsupported modes to RGB or RGBA, and save a new PNG to an empty `BytesIO`
without copying source metadata.

Return:

```python
@dataclass(frozen=True, slots=True)
class SanitizedQuizImage:
    payload: bytes
    sha256: str
    width: int
    height: int
    media_type: str = "image/png"
```

`StudioQuizImageService.upload` sanitizes first, writes to
`<data_dir>/studio-quiz-media/<run_id>/<image_key>-<sha256>.png` with
`verified_atomic_write`, then calls `repository.bind_image`. It never deletes or
changes the previous binding unless all three steps succeed.

- [ ] **Step 5: Run the image tests and verify GREEN**

Run: `.venv/bin/pytest tests/study_generation/test_quiz_images.py tests/test_packaging_dependencies.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the media slice**

```bash
git add pyproject.toml src/oms_hub/study_generation/quiz_images.py tests/study_generation/test_quiz_images.py tests/test_packaging_dependencies.py
git commit -m "feat: sanitize Studio quiz images"
```

---

### Task 4: Worker publication gate and replacement safety

**Files:**
- Modify: `src/oms_hub/study_generation/studio_worker.py`
- Modify: `src/oms_hub/study_generation/studio_repository.py`
- Test: `tests/study_generation/test_studio_publication.py`
- Test: `tests/study_generation/test_studio_runs.py`

**Interfaces:**
- Consumes: `image_requirements(quiz)` and `StudioRepository.await_image_review(...)`.
- Preserves: existing `GenerationRepository.publish_studio_quiz` path for image-free Studio quizzes.

- [ ] **Step 1: Write failing worker-state tests**

```python
def test_image_dependent_chat_answer_waits_for_private_review(tmp_path):
    image_quiz = json.dumps({
        "title": "Review",
        "questions": [{
            "stem": "Use the figure.",
            "choices": ["A", "B"],
            "correct_index": 0,
            "rationale": "A is correct.",
            "image_ref": {
                "key": "image-1",
                "source_title": "Slides",
                "locator": "Slide 4",
                "description": "Histology image",
            },
        }],
    })
    database, studio, service, published, _gateway, worker = _components(
        tmp_path, [image_quiz]
    )
    run = service.queue_run("Neuro", 1, "Create", [], "Review", "Neuro", 1)

    worker.run_once()

    waiting = studio.get_run(run.id)
    assert waiting.state is StudioRunState.AWAITING_IMAGES
    assert waiting.stage is StudioRunStage.IMAGE_REVIEW
    assert waiting.published_token is None
    assert published.published_quizzes() == ()


def test_replacement_waiting_for_images_leaves_current_quiz_active(tmp_path):
    image_quiz = json.dumps({
        "title": "Replacement",
        "questions": [{
            "stem": "Use the figure.",
            "choices": ["A", "B"],
            "correct_index": 0,
            "rationale": "A is correct.",
            "image_ref": {
                "key": "image-1",
                "source_title": "Slides",
                "locator": "Slide 4",
                "description": "Histology image",
            },
        }],
    })
    database, studio, service, published, _gateway, worker = _components(
        tmp_path, [_quiz(stem="Original?"), image_quiz]
    )
    first = service.queue_run("Neuro", 1, "Create", [], "Review", "Neuro", 1)
    worker.run_once()
    original_run = studio.get_run(first.id)
    original = published.published_quiz(original_run.published_token or "")
    assert original is not None

    replacement = studio.rerun(first.id)
    worker.run_once()

    assert studio.get_run(replacement.id).state is StudioRunState.AWAITING_IMAGES
    still_public = published.published_quiz(original.token)
    assert still_public is not None
    assert still_public.version == 1
    assert still_public.quiz.questions[0].stem == "Original?"
```

- [ ] **Step 2: Run worker tests and verify RED**

Run: `.venv/bin/pytest tests/study_generation/test_studio_publication.py tests/study_generation/test_studio_runs.py -q`

Expected: FAIL because every valid Studio quiz currently publishes immediately.

- [ ] **Step 3: Add the minimal worker branch**

After parsing and relabeling the quiz, check `image_requirements(quiz)`. When
non-empty, call `await_image_review` and return without entering `PUBLISH`.
When empty, retain the current publication and completion path unchanged.
Ensure `recover_interrupted_jobs` requeues only `RUNNING` rows, so the new
awaiting state is stable across restart.

- [ ] **Step 4: Run worker tests and verify GREEN**

Run: `.venv/bin/pytest tests/study_generation/test_studio_publication.py tests/study_generation/test_studio_runs.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the worker slice**

```bash
git add src/oms_hub/study_generation/studio_worker.py src/oms_hub/study_generation/studio_repository.py tests/study_generation/test_studio_publication.py tests/study_generation/test_studio_runs.py
git commit -m "feat: hold image quizzes for Studio review"
```

---

### Task 5: Private image review routes and interface

**Files:**
- Modify: `src/oms_hub/app.py`
- Modify: `src/oms_hub/web/studio_routes.py`
- Create: `src/oms_hub/web/templates/studio_quiz_images.html`
- Create: `src/oms_hub/web/static/studio_quiz_images.js`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/web/static/notebook_studio.js`
- Test: `tests/v2/test_studio_routes.py`
- Create: `tests/js/studio_quiz_images.test.js`
- Modify: `tests/js/notebook_studio.test.js`
- Modify: `tests/v2/test_release_package.py`

**Interfaces:**
- Produces: private `GET /studio/runs/{run_id}/images` review page.
- Produces: `GET /studio/runs/{run_id}/image-review` JSON status.
- Produces: `POST /studio/runs/{run_id}/images/{image_key}` multipart upload.
- Produces: `PUT` and `DELETE /studio/runs/{run_id}/questions/{question_id}/image-override`.

- [ ] **Step 1: Write failing route and browser-behavior tests**

Use a real app and CSRF client to prove the page is private, upload is bounded,
shared keys are grouped, overrides reverse, and invalid uploads preserve the
previous image:

```python
def test_image_review_upload_and_override_routes_are_private_and_csrf_protected(tmp_path):
    app, run = _awaiting_image_app(tmp_path)
    public = TestClient(app)
    client = csrf_client(app)

    assert public.post(
        f"/studio/runs/{run.id}/images/image-1",
        files={"file": ("figure.png", _png(), "image/png")},
    ).status_code == 403
    uploaded = client.post(
        f"/studio/runs/{run.id}/images/image-1",
        files={"file": ("figure.png", _png(), "image/png")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["width"] > 0

    assert client.put(
        f"/studio/runs/{run.id}/questions/q1/image-override"
    ).status_code == 200
    assert client.delete(
        f"/studio/runs/{run.id}/questions/q1/image-override"
    ).status_code == 200
```

Add Node tests proving `renderRuns` displays **Images needed** and an **Add
images** link, and `renderReview` builds one safe text-only group with all linked
question numbers and upload/override controls. Extend the release-package test
before creating the new files and assert that both `hotfix_names` and
`source_names` contain `quiz_images.py`, `studio_quiz_images.html`, and
`studio_quiz_images.js`.

- [ ] **Step 2: Run focused route and JS tests and verify RED**

Run: `.venv/bin/pytest tests/v2/test_studio_routes.py -q`

Run: `node --test tests/js/notebook_studio.test.js tests/js/studio_quiz_images.test.js`

Run: `.venv/bin/pytest tests/v2/test_release_package.py -q`

Expected: FAIL because the routes, template, service state, and UI module are missing.

- [ ] **Step 3: Wire the media service and private routes**

Construct `StudioQuizImageService` in `create_app` with
`resolved.data_dir / "studio-quiz-media"`. Add route helpers that map `KeyError`
to 404, invalid state/unresolved conflicts to 409, and `QuizImageError` to 422.
Read at most `10 * 1024 * 1024 + 1` bytes from the upload before calling the
service. Apply `require_form_csrf` to every mutation.

Return review JSON containing only the run ID, label, state, resolved status,
question number/stem, reference metadata, override flag, and safe upload
metadata. Never return `asset_path`, digest, raw response, or answers.

- [ ] **Step 4: Build the grouped review UI**

Create a base-template page headed **Quiz images**. Render groups using DOM
creation and `textContent`; each group includes source, locator, description,
affected questions, current sanitized dimensions, upload/replace form, and a
per-question **No image needed** toggle. Confirm before setting an override.
Show **Preview quiz** only when the JSON response says `resolved: true`.

Update `renderRuns`:

```javascript
if (run.state === "awaiting_images") {
  status.textContent = `Images needed · ${run.stage} · attempt ${run.attempts}`;
  const images = documentRef.createElement("a");
  images.className = "button primary compact";
  images.href = `/studio/runs/${encodeURIComponent(run.id)}/images`;
  images.textContent = "Add images";
  card.append(images);
}
```

- [ ] **Step 5: Run focused route and JS tests and verify GREEN**

Run: `.venv/bin/pytest tests/v2/test_studio_routes.py -q`

Run: `node --test tests/js/notebook_studio.test.js tests/js/studio_quiz_images.test.js`

Run: `.venv/bin/pytest tests/v2/test_release_package.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the private review slice**

```bash
git add src/oms_hub/app.py src/oms_hub/web/studio_routes.py src/oms_hub/web/templates/studio_quiz_images.html src/oms_hub/web/static/studio_quiz_images.js src/oms_hub/web/static/app.css src/oms_hub/web/static/notebook_studio.js tests/v2/test_studio_routes.py tests/v2/test_release_package.py tests/js/studio_quiz_images.test.js tests/js/notebook_studio.test.js
git commit -m "feat: add Studio image review interface"
```

---

### Task 6: Final preview, atomic publication, and public media

**Files:**
- Modify: `src/oms_hub/study_generation/repository.py`
- Modify: `src/oms_hub/study_generation/quiz_images.py`
- Modify: `src/oms_hub/web/studio_routes.py`
- Create: `src/oms_hub/web/templates/studio_quiz_preview.html`
- Create: `src/oms_hub/web/static/studio_quiz_preview.js`
- Modify: `src/oms_hub/web/public_quiz_routes.py`
- Modify: `src/oms_hub/study_generation/native_quiz.py`
- Test: `tests/study_generation/test_studio_image_review.py`
- Test: `tests/v2/test_studio_routes.py`
- Test: `tests/v2/test_public_quiz_routes.py`
- Create: `tests/js/studio_quiz_preview.test.js`
- Modify: `tests/v2/test_release_package.py`

**Interfaces:**
- Produces: `GenerationRepository.publish_reviewed_studio_quiz(run_id, quiz) -> PublishedQuizRecord`.
- Produces: private preview page/content/answer/media routes under `/studio/runs/{run_id}/preview`.
- Produces: explicit `POST /studio/runs/{run_id}/publication`.
- Produces: public `GET /public/quizzes/{token}/media/{image_key}`.
- Extends: `public_quiz_content(quiz, image_urls=None)` with optional image URL and alt text only.

- [ ] **Step 1: Write failing preview, transaction, and public-boundary tests**

```python
def test_publish_is_blocked_until_every_non_overridden_key_has_an_image(tmp_path):
    app, run = _awaiting_image_app(tmp_path)
    response = csrf_client(app).post(f"/studio/runs/{run.id}/publication")
    assert response.status_code == 409
    assert app.state.generation_repository.published_quizzes() == ()


def test_explicit_publish_reuses_asset_across_questions_without_private_metadata(tmp_path):
    app, run = _resolved_image_app(tmp_path)
    client = csrf_client(app)
    preview = client.get(f"/studio/runs/{run.id}/preview/content")
    published = client.post(f"/studio/runs/{run.id}/publication")
    token = published.json()["token"]
    content = client.get(f"/public/quizzes/{token}/content").json()

    assert preview.json()["questions"][0]["image_url"].startswith("/studio/")
    assert content["questions"][0]["image_url"] == f"/public/quizzes/{token}/media/image-1"
    assert content["questions"][1]["image_url"] == f"/public/quizzes/{token}/media/image-1"
    assert "locator" not in json.dumps(content)
    assert "correct_index" not in json.dumps(content)


def test_unpublished_or_unbound_media_returns_not_found(tmp_path):
    app, run = _resolved_image_app(tmp_path)
    client = csrf_client(app)
    token = client.post(f"/studio/runs/{run.id}/publication").json()["token"]
    assert client.get(f"/public/quizzes/{token}/media/image-1").status_code == 200
    assert client.get(f"/public/quizzes/{token}/media/image-9").status_code == 404
    client.delete(f"/studio/runs/{run.id}/publication")
    assert client.get(f"/public/quizzes/{token}/media/image-1").status_code == 404
```

Add a replacement test: resolve and publish a rerun, then assert the original
token is retained, the version increments once, and both payload/media bindings
switch in the same transaction. Before creating the preview files, extend the
release test to require `studio_quiz_preview.html` and
`studio_quiz_preview.js` in both `hotfix_names` and `source_names`.

- [ ] **Step 2: Run focused publication tests and verify RED**

Run: `.venv/bin/pytest tests/study_generation/test_studio_image_review.py tests/v2/test_studio_routes.py tests/v2/test_public_quiz_routes.py -q`

Run: `node --test tests/js/studio_quiz_preview.test.js`

Run: `.venv/bin/pytest tests/v2/test_release_package.py -q`

Expected: FAIL because reviewed publication, preview, and media routes are absent.

- [ ] **Step 3: Implement atomic reviewed publication**

In one `GenerationRepository` session, reload the awaiting run, validated draft,
requirements, and overrides. Recompute unresolved keys. Create or update the
`PublishedQuizModel` using the existing stable-token/supersedes behavior. Delete
and recreate `PublishedQuizMediaModel` bindings for the token from resolved,
non-overridden requirements. Update `StudioRunModel` to complete with the token
only after every model mutation is ready to commit.

If a run is already complete with a matching active publication, return it
without incrementing the version. If any requirement is unresolved, raise the
exact conflict before changing a publication row.

- [ ] **Step 4: Implement private preview and public media routes**

Use a dedicated preview template with the existing player assets, a back link to
the image review, and a CSRF-protected **Publish quiz** button. Preview content
uses a synthetic token `preview-{run_id}`, the draft version `1`, and private
image URLs. Preview answer grading calls `grade_answer` on the resolved draft.

`studio_quiz_preview.js` posts to the template's `data-publish-url` with the
CSRF header, disables the button while publishing, renders a text-only error on
failure, and assigns `window.location` to the returned `published_url` on
success. Add a Node test using a real button-like DOM fixture and a deterministic
fetch response to prove the request uses POST/CSRF and navigates only after a
successful JSON response.

The public media route validates key syntax, calls a repository method that
joins an active `PublishedQuizModel` to `PublishedQuizMediaModel`, verifies the
stored file is present and its SHA-256 still matches, applies the public quiz
rate limiter, and returns `FileResponse(..., media_type="image/png")`. Missing,
inactive, unbound, or checksum-invalid assets return 404.

Extend public content by accepting a mapping:

```python
def public_quiz_content(
    quiz: NativeQuiz,
    image_urls: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, object]:
    questions: list[dict[str, object]] = []
    for question in quiz.questions:
        item: dict[str, object] = {
            "id": question.id,
            "stem": question.stem,
            "choices": [
                {"id": choice.id, "text": choice.text}
                for choice in question.choices
            ],
        }
        if question.image_ref is not None and image_urls is not None:
            media = image_urls.get(question.image_ref.key)
            if media is not None:
                item["image_url"], item["image_alt"] = media
        questions.append(item)
    return {"title": quiz.title, "questions": questions}
```

- [ ] **Step 5: Run focused publication tests and verify GREEN**

Run: `.venv/bin/pytest tests/study_generation/test_studio_image_review.py tests/v2/test_studio_routes.py tests/v2/test_public_quiz_routes.py -q`

Run: `node --test tests/js/studio_quiz_preview.test.js`

Run: `.venv/bin/pytest tests/v2/test_release_package.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the publication slice**

```bash
git add src/oms_hub/study_generation/repository.py src/oms_hub/study_generation/quiz_images.py src/oms_hub/study_generation/native_quiz.py src/oms_hub/web/studio_routes.py src/oms_hub/web/templates/studio_quiz_preview.html src/oms_hub/web/static/studio_quiz_preview.js src/oms_hub/web/public_quiz_routes.py tests/study_generation/test_studio_image_review.py tests/v2/test_studio_routes.py tests/v2/test_public_quiz_routes.py tests/v2/test_release_package.py tests/js/studio_quiz_preview.test.js
git commit -m "feat: preview and publish Studio image quizzes"
```

---

### Task 7: Player image rendering and Quiz Library navigation

**Files:**
- Modify: `src/oms_hub/web/static/public_quiz.js`
- Modify: `src/oms_hub/web/static/public_quiz.css`
- Modify: `src/oms_hub/web/templates/base.html`
- Modify: `tests/js/public_quiz.test.js`
- Modify: `tests/test_interface_improvements.py`

**Interfaces:**
- Consumes: optional `question.image_url` and `question.image_alt` from Task 6.
- Produces: safe responsive image rendering above the stem with click/tap enlargement.
- Produces: persistent private **Quiz Library** navigation link to `/public/quizzes`.

- [ ] **Step 1: Write failing player and navigation tests**

Add a DOM test around an exported helper that verifies real element attributes:

```javascript
test("question image is rendered above the stem with safe enlargement", () => {
  const documentRef = fakeDocument();
  const media = renderQuestionImage(documentRef, {
    image_url: "/public/quizzes/token/media/image-1",
    image_alt: "Reference image used for questions 4-7",
  });

  assert.equal(media.querySelector("img").src, "/public/quizzes/token/media/image-1");
  assert.equal(media.querySelector("img").alt, "Reference image used for questions 4-7");
  assert.equal(media.querySelector("a").href, "/public/quizzes/token/media/image-1");
});
```

Add a rendered-page assertion that private navigation contains exactly one
`href="/public/quizzes"` link labeled **Quiz Library**.

- [ ] **Step 2: Run player and interface tests and verify RED**

Run: `node --test tests/js/public_quiz.test.js`

Run: `.venv/bin/pytest tests/test_interface_improvements.py -q`

Expected: FAIL because the renderer and navigation link are absent.

- [ ] **Step 3: Implement safe player rendering and responsive CSS**

Build all nodes with `createElement`, set `src`, `alt`, `loading="eager"`, and
`decoding="async"`, and wrap the image in a same-origin anchor whose `href`
equals the validated server-provided URL. Insert the media figure immediately
before the stem. Do not use `innerHTML`. Build the header metadata from the
course and exam for every quiz, adding `Lecture N` only when
`content.lecture_number` is an integer so Studio quizzes and previews never show
`Lecture undefined`.

Add CSS that constrains the image to `max-width: 100%`, `max-height: min(65vh,
48rem)`, `object-fit: contain`, preserves aspect ratio, and shows a visible
focus ring on the enlargement link.

Add this item to `base.html` after NotebookLM Studio and before Settings:

```html
<a href="/public/quizzes" {% if current_path.startswith("/public/quizzes") %}aria-current="page"{% endif %}>Quiz Library</a>
```

- [ ] **Step 4: Run player and interface tests and verify GREEN**

Run: `node --test tests/js/public_quiz.test.js`

Run: `.venv/bin/pytest tests/test_interface_improvements.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the player/navigation slice**

```bash
git add src/oms_hub/web/static/public_quiz.js src/oms_hub/web/static/public_quiz.css src/oms_hub/web/templates/base.html tests/js/public_quiz.test.js tests/test_interface_improvements.py
git commit -m "feat: render quiz images and link quiz library"
```

---

### Task 8: Rollout documentation and full verification

**Files:**
- Modify: `docs/notebooklm-studio-rollout.md`
- Modify: this plan file to check completed steps

**Interfaces:**
- Verifies: the release coverage added in Tasks 3, 5, and 6 includes Pillow, new runtime modules, templates, scripts, styles, and schema migration.
- Verifies: the complete Python, JavaScript, lint, and type-check suites.

- [ ] **Step 1: Update rollout guidance**

Update the Studio rollout guidance to state that an upgrade must reinstall
dependencies for Pillow, restart once for schema version 11, then test one
image-free auto-publication and one image-review publication. Include the
expected **Images needed**, **Add images**, **Preview quiz**, and **Publish
quiz** checkpoints plus the persistent **Quiz Library** navigation link.

- [ ] **Step 2: Re-run release coverage**

Run: `.venv/bin/pytest tests/v2/test_release_package.py -q`

Expected: PASS.

- [ ] **Step 3: Run fresh full verification**

Run: `.venv/bin/pytest`

Expected: all Python tests pass with zero warnings.

Run: `node --test tests/js/*.test.js`

Expected: all JavaScript tests pass.

Run: `.venv/bin/ruff check src tests`

Expected: no lint errors.

Run: `.venv/bin/mypy src`

Expected: no type errors.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 4: Review requirements against the approved specification**

Confirm with fresh evidence that image-free Studio output auto-publishes,
image-dependent output waits, shared keys reuse one upload, per-question
overrides reverse, final preview precedes explicit publish, existing quizzes
still parse, public responses omit private data, public media follows active
tokens, and the private nav exposes Quiz Library.

- [ ] **Step 5: Commit the rollout and verification slice**

```bash
git add docs/notebooklm-studio-rollout.md docs/superpowers/plans/2026-07-31-studio-quiz-images-and-library-navigation.md
git commit -m "docs: finalize Studio quiz image rollout"
```
