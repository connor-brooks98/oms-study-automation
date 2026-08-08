# Anki Lecture Selector and Source Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair lecture source discovery and deliver an aligned Course → Exam → lecture selector that fills an editable canonical lecture tag.

**Architecture:** The server will construct one deterministic lecture payload and grouped navigation view from the catalog and current ingestion revisions. The template will embed the payload in an `application/json` script element, and client code will resolve lecture buttons by numeric ID instead of parsing quote-sensitive JSON attributes.

**Tech Stack:** FastAPI, Jinja2, SQLAlchemy repositories, vanilla JavaScript, CSS Grid, pytest, Node test runner.

## Global Constraints

- Preserve the existing `CreateCurationJobRequest` API and database schema.
- Do not mutate ingestion records, Anki indexes, or the acceptance-copy collection.
- Use the established `oms_hub.anki.paths.target_tag` canonical tag format.
- Slides and transcript revisions remain independently selectable and checked by default.
- The lecture tag is normal editable input text, not placeholder text.
- The page remains one column below the existing 720px responsive breakpoint.

---

### Task 1: Build Safe Grouped Lecture Payloads

**Files:**
- Modify: `src/oms_hub/web/anki_routes.py`
- Modify: `tests/anki/test_web.py`

**Interfaces:**
- Consumes: `CatalogRepository.list_lectures()`, `IngestionRepository.list_current_revisions(lecture_id)`, `LectureIdentity`, and `target_tag`.
- Produces: `_lecture_context(request) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]`, returning flat serialized lecture data plus Course → Exam groups.

- [ ] **Step 1: Write failing route tests**

Add a second lecture containing quotes and a transcript revision, then assert `/api/anki/bootstrap` returns literal current revision data, the canonical editable tag, and deterministic Course → Exam grouping.

```python
response = client.get("/api/anki/bootstrap")
lecture = next(item for item in response.json()["lectures"] if item["id"] == lecture_id)
assert lecture["revisions"] == [
    {"id": revision_id, "kind": "slides", "source_sha256": SHA}
]
assert lecture["target_tag"] == (
    "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"
)
assert response.json()["lecture_groups"][0]["course"] == "Heme Lymph"
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest tests/anki/test_web.py -k "bootstrap_groups or bootstrap_exposes" -q
```

Expected: failure because `target_tag` and `lecture_groups` are absent.

- [ ] **Step 3: Implement the grouped context**

Import `LectureIdentity` and `target_tag`, create each flat payload once, group by course and exam in catalog order, and return both collections from `_page_context`. Include only literal revision ID, kind, and source hash fields.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
pytest tests/anki/test_web.py -k "bootstrap_groups or bootstrap_exposes" -q
```

Expected: pass.

---

### Task 2: Render the Accordion and Bind Source Data Safely

**Files:**
- Modify: `src/oms_hub/web/templates/anki.html`
- Modify: `src/oms_hub/web/static/anki.js`
- Create: `tests/js/anki.test.js`
- Modify: `tests/anki/test_web.py`

**Interfaces:**
- Consumes: the flat `lectures` payload and `lecture_groups` from Task 1.
- Produces: `parseLecturePayload(value)`, `resolveLecture(lectures, lectureId)`, and the unchanged curation-job JSON request.

- [ ] **Step 1: Write failing template and JavaScript tests**

The Python integration test parses the `application/json` script element and
asserts topics containing quotes round-trip as JSON. It also asserts there is
no `data-revisions` attribute. The Node test asserts lecture resolution returns
the selected revisions and canonical tag as literal values.

```javascript
const selected = anki.resolveLecture(
  [{
    id: 42,
    topic: 'Anemia "I"',
    target_tag: "AnkiHub_Optional::LMU_OMS_II::Heme::Block1::Lec4_Anemia_I",
    revisions: [{ id: 7, kind: "slides", source_sha256: "a".repeat(64) }],
  }],
  "42",
);
assert.equal(selected.revisions[0].kind, "slides");
assert.equal(selected.target_tag.endsWith("Lec4_Anemia_I"), true);
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest tests/anki/test_web.py -k "page_embeds_safe_lecture_json" -q
node --test tests/js/anki.test.js
```

Expected: Python failure because the template still uses `data-revisions`;
Node failure because `resolveLecture` is not exported.

- [ ] **Step 3: Implement the accordion and client selection**

Render nested native `details` elements, lecture buttons, a hidden required
`lecture_id` field, and one `application/json` payload element. Parse the
payload once during initialization. On lecture selection:

- set the hidden lecture ID;
- set exactly one button to `aria-pressed="true"`;
- render current slides/transcript checkboxes checked;
- set the actual `target_tag` input value to the selected lecture default;
- display a parse/setup error if the embedded payload is invalid.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
pytest tests/anki/test_web.py -k "page_embeds_safe_lecture_json" -q
node --test tests/js/anki.test.js
```

Expected: pass.

---

### Task 3: Align the Curation Form and Verify the Complete Change

**Files:**
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/web/templates/anki.html`
- Modify: `README.md`

**Interfaces:**
- Consumes: the accordion markup from Task 2.
- Produces: aligned desktop and single-column mobile Anki start-page layout.

- [ ] **Step 1: Add focused layout classes**

Use full-width classes for the accordion, sources, lecture tag, and focus
textarea. Align paired field labels and controls with consistent grid rows,
`min-width: 0`, full-width controls, and equal gaps. Preserve the 720px mobile
collapse.

- [ ] **Step 2: Update operator documentation**

Document the Course → Exam → lecture selector and editable auto-filled tag in
the Integrated Anki curation section.

- [ ] **Step 3: Run all verification gates**

Run:

```bash
pytest -q
node --test tests/js/*.test.js
ruff check src tests scripts
mypy src
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 4: Commit and push**

```bash
git add README.md src/oms_hub/web tests/anki/test_web.py tests/js/anki.test.js
git commit -m "fix: repair Anki lecture source selection"
git push origin codex/anki-v4-implementation
```
