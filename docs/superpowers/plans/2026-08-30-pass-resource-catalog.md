# Pass Resource Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an inline Other-resource editor that atomically saves a lecture pass resource into a global, durable dropdown catalog.

**Architecture:** Schema v31 adds one case-insensitive resource catalog table. The existing pass PATCH owns both catalog insertion and pass assignment in one repository transaction; the lecture route renders the catalog, and the current lecture script reveals a native inline editor for the Other sentinel.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, SQLite, Jinja, browser-native JavaScript and CSS, pytest, Node test runner.

**Spec:** `docs/superpowers/specs/2026-08-30-pass-resource-catalog-design.md`

## Global Constraints

- The resource catalog is global across all courses and lectures.
- Default order is exactly: Lecture, Anki, Lecture outline, Practice questions, Other.
- Resource names are trimmed, nonblank, at most 100 characters, and case-insensitively unique while preserving the first stored spelling.
- Catalog insertion and pass assignment use the existing CSRF-protected PATCH and one database transaction.
- Selecting Other reveals an inline Resource name field and Add & use button without saving `Other`.
- No resource rename/delete/reorder screen, no new endpoint, and no new dependency.
- Preserve pass completion dates, extra-pass behavior, exam overview behavior, concurrent updates, accessibility, output escaping, and reduced-motion support.
- Commit test-only RED before minimum GREEN for each implementation task.
- Do not push, deploy, or mutate an external Study Hub runtime.

---

### Task 1: Durable resource catalog and atomic pass save

**Files:**
- Modify: `src/oms_hub/models.py`
- Modify: `src/oms_hub/migrations.py`
- Modify: `src/oms_hub/anki/rehearsal/path_contract.py`
- Modify: `src/oms_hub/repositories.py`
- Modify: `src/oms_hub/web/routes.py`
- Test: `tests/study_generation/test_migration.py`
- Test: `tests/v2/test_pass_tracker.py`

**Interfaces:**
- Consumes: `LecturePassModel.resource`, `CatalogRepository.update_pass(...)`, and the current lecture detail context.
- Produces: `LecturePassResourceModel`, `CatalogRepository.list_pass_resources() -> list[str]`, schema version 31, and a `pass_resources` lecture-template context value.

- [ ] **Step 1: Write migration and repository contract tests**

Add a v31 migration test that drops `lecture_pass_resources`, sets schema version 30, records `Boards & Beyond`, a case variant, and `Other` on existing passes, migrates twice, and asserts:

```python
assert resources == [
    "Lecture",
    "Anki",
    "Lecture outline",
    "Practice questions",
    "Boards & Beyond",
]
assert version == 31
```

Add an API/page test that PATCHes `Pathoma`, changes that pass to `Anki`, opens another lecture, and asserts one reusable `Pathoma` option still renders. Add a case-variant PATCH and assert only the first spelling remains in `lecture_pass_resources`.

- [ ] **Step 2: Run the focused tests and capture RED**

Run:

```bash
uv run pytest tests/study_generation/test_migration.py -k 'v31 or pass_resource' -q
uv run pytest tests/v2/test_pass_tracker.py -k 'custom_resource or resource_catalog' -q
```

Expected: FAIL because schema v31, its catalog table, and reusable template context do not exist.

- [ ] **Step 3: Commit the test-only RED state**

```bash
git add tests/study_generation/test_migration.py tests/v2/test_pass_tracker.py
git commit -m "test: define reusable pass resource catalog"
```

- [ ] **Step 4: Add the minimal schema v31 model and migration**

Add this model shape beside `LecturePassModel`:

```python
class LecturePassResourceModel(Base):
    __tablename__ = "lecture_pass_resources"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100, collation="NOCASE"))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
```

Set `LATEST_SCHEMA_VERSION = 31`. Add `_upgrade_lecture_pass_resources_v31()` to create the table and insert, in order, the four defaults followed by valid existing pass values. Use SQLite conflict-ignore semantics, trim names, and exclude blank/Other values. Add `_validate_lecture_pass_resources_v31()` for table presence, required columns/nullability, primary key, and unique name constraint. Call upgrade/validation beside v30 and register schema 31 as introducing no path field:

```python
_PATH_COLUMNS[31] = _PATH_COLUMNS[30]
```

- [ ] **Step 5: Make catalog insertion and pass assignment atomic**

Add:

```python
def list_pass_resources(self) -> list[str]:
    with self.database.session() as session:
        return list(session.scalars(
            select(LecturePassResourceModel.name).order_by(LecturePassResourceModel.id)
        ))
```

Inside the existing `update_pass()` session, insert each nonblank, non-Other resource with conflict-ignore, select the matching catalog row through its NOCASE comparison, and assign that canonical spelling to `LecturePassModel.resource` in the same transaction. Do not catalog clears or the Other sentinel. Pass `list_pass_resources()` into `lecture_detail()` as `pass_resources`.

- [ ] **Step 6: Run focused and adjacent backend tests**

Run:

```bash
uv run pytest tests/study_generation/test_migration.py tests/v2/test_pass_tracker.py tests/anki/test_rehearsal_capsule.py -q
uv run ruff check src/oms_hub/models.py src/oms_hub/migrations.py src/oms_hub/repositories.py src/oms_hub/web/routes.py tests/study_generation/test_migration.py tests/v2/test_pass_tracker.py
uv run mypy src/oms_hub/models.py src/oms_hub/migrations.py src/oms_hub/repositories.py src/oms_hub/web/routes.py
```

Expected: all pass.

- [ ] **Step 7: Commit backend GREEN**

```bash
git add src/oms_hub/models.py src/oms_hub/migrations.py src/oms_hub/anki/rehearsal/path_contract.py src/oms_hub/repositories.py src/oms_hub/web/routes.py
git commit -m "feat: persist reusable pass resources"
```

---

### Task 2: Inline Other-resource editor

**Files:**
- Modify: `src/oms_hub/web/templates/lecture.html`
- Modify: `src/oms_hub/web/static/lecture.js`
- Modify: `src/oms_hub/web/static/app.css`
- Test: `tests/js/lecture.test.js`
- Test: `tests/v2/test_lecture_generation_ui.py`
- Test: `tests/v2/test_ui_design_standard.py`

**Interfaces:**
- Consumes: `pass_resources: list[str]` from Task 1 and the unchanged pass PATCH response.
- Produces: `[data-pass-resource-custom]`, `[data-pass-resource-name]`, and `[data-add-pass-resource]` hooks plus inline Other selection behavior.

- [ ] **Step 1: Write failing DOM and interaction tests**

Extend the server-rendered UI tests to require one hidden, labeled custom-resource editor per pass with `maxlength="100"`, `required`, and an Add & use button. Extend the fake DOM just enough to model `hidden`, `focus()`, `options`, and `document.createElement("option")`.

Add JavaScript tests asserting:

```javascript
select.value = "Other";
await select.dispatch("change");
assert.equal(custom.hidden, false);
assert.equal(customInput.focused, true);
assert.equal(patchRequests.length, 0);
```

Then type `"  Pathoma  "`, click Add & use, and assert the PATCH body is `{ resource: "Pathoma" }`, every resource select receives a text-safe Pathoma option, the current select chooses it, and the editor closes. Cover blank input without a request and failed PATCH preserving the previous selection/editor value.

- [ ] **Step 2: Run the focused UI tests and capture RED**

Run:

```bash
node --test tests/js/lecture.test.js
uv run pytest tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py -q
```

Expected: FAIL because the custom editor hooks and Other behavior do not exist.

- [ ] **Step 3: Commit the test-only RED state**

```bash
git add tests/js/lecture.test.js tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py
git commit -m "test: define custom pass resource editor"
```

- [ ] **Step 4: Render the catalog and accessible inline editor**

Remove the template-local fixed tuple. Render every `pass_resources` name followed by the Other sentinel. In each row, add a hidden `.pass-resource-custom.t-page-enter` containing a unique labeled input and button:

```html
<div class="pass-resource-custom t-page-enter" data-pass-resource-custom hidden>
  <label class="sr-only" for="pass-resource-name-{{ pass_item.position }}">Resource name for pass {{ pass_item.position }}</label>
  <input class="sh-input" id="pass-resource-name-{{ pass_item.position }}" maxlength="100" required autocomplete="off" data-pass-resource-name>
  <button class="sh-btn sh-btn--secondary" type="button" data-add-pass-resource>Add &amp; use</button>
</div>
```

Keep all resource labels text-escaped by Jinja.

- [ ] **Step 5: Implement native Other reveal and save behavior**

On select change, if the value is Other, reveal the editor, focus its input, and return without PATCHing. Otherwise hide/clear the editor and use the existing PATCH path. On Add & use:

1. Trim the input and reject blank with the existing live region.
2. Disable the row controls while saving.
3. PATCH `{ resource: name }` through `patchPass()`.
4. Before `renderPass()`, create missing options with `documentRef.createElement("option")`, `option.value`, and `option.textContent` for every pass select, comparing case-insensitively.
5. Render the canonical response name, update `savedResource`, hide/clear the editor, and announce success.
6. On failure, restore the prior select value, keep the editor/value visible, and announce the server detail.

- [ ] **Step 6: Add the smallest responsive layout rules**

Make the existing resource cell a minimum-width-zero grid container. Lay out the custom input and button as `minmax(0, 1fr) auto`, stack them on narrow screens, and rely on `t-page-enter` plus the global reduced-motion rule rather than adding another animation.

- [ ] **Step 7: Run focused and adjacent frontend tests**

Run:

```bash
node --test tests/js/lecture.test.js
node --test tests/js/*.test.js
uv run pytest tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py tests/v2/test_pass_tracker.py -q
```

Expected: all pass.

- [ ] **Step 8: Commit frontend GREEN**

```bash
git add src/oms_hub/web/templates/lecture.html src/oms_hub/web/static/lecture.js src/oms_hub/web/static/app.css
git commit -m "feat: add custom pass resources inline"
```

---

### Task 3: Whole-feature verification

**Files:**
- Verify only; modify production or tests only to correct a demonstrated failure.

**Interfaces:**
- Consumes: schema v31, atomic pass PATCH behavior, and the inline editor from Tasks 1 and 2.
- Produces: broad automated and browser evidence for the exact final tree.

- [ ] **Step 1: Run full automated verification**

```bash
uv run pytest -q
node --test tests/js/*.test.js
uv run ruff check src tests
uv run mypy src/oms_hub
```

- [ ] **Step 2: Check changed-code coverage**

Run the repository's established changed-coverage commands for the Python and JavaScript files changed by this plan. Require at least the existing project thresholds and add a focused test for any uncovered user-visible branch.

- [ ] **Step 3: Exercise the live local page**

On desktop and narrow mobile widths, select Other, verify focus/reveal with no request, reject blank, save a new resource containing harmless punctuation, confirm it appears in every row, reload, open another lecture, and confirm it remains selectable. Verify keyboard operation, failure feedback, no console errors, and reduced-motion compatibility.

- [ ] **Step 4: Verify repository identity and scope**

```bash
git status --short --branch
git log --oneline --decorate -8
git diff --check main...HEAD
git merge-base --is-ancestor main HEAD
git rev-parse HEAD
git rev-parse HEAD^{tree}
```

- [ ] **Step 5: Obtain independent final review**

Give a fresh reviewer the exact merge-base-to-HEAD diff plus the approved spec and test evidence. Resolve every load-bearing finding before completion.

