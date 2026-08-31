# Exam Countdown and Editable Pass Dates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an exam-level date picker and 8:00 AM countdown to exam overview pages, and let lecture pass completion dates default to the browser's local current date and remain editable.

**Architecture:** Keep the existing `lectures.exam_date` storage and add one exact-scope bulk update for all lectures sharing `(subject, exam_number)`; do not introduce a new table. Extend the existing pass PATCH contract with a validated optional `completed_on` date, then use native date inputs and the existing Study Hub modal/number motion hooks in two small page scripts.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy/SQLite, Jinja2, vanilla JavaScript, Node test runner, pytest/selectolax.

**Spec:** `/Users/connor/.codex/visualizations/2026/08/30/01a0547d-a15d-7c02-8c28-09f79c876c8b/exam-countdown-pass-date-mock/index.html`

## Global Constraints

- The countdown target is exactly 8:00 AM in the browser's local timezone on the selected exam date.
- A newly checked pass sends the browser-local calendar date built with `getFullYear()`, `getMonth()`, and `getDate()`; never derive a local date with `toISOString()`.
- Existing pass completion and resource updates remain independent, CSRF-protected, and race-safe.
- Exam date writes update only the exact `subject` plus `exam_number` scope and use parameterized SQLAlchemy statements.
- Malformed dates fail with HTTP 422 before any database mutation.
- If legacy lecture rows disagree about an exam date, render an unscheduled/conflict state until the next exam-scoped save normalizes them.
- Reuse native `<input type="date">`, the shared `.sh-dialog.t-modal`, `.t-number`, `.sh-card`, `.sh-btn`, and existing reduced-motion rules; add no dependency.
- Preserve the existing exam progress table, processing checklist, pass resources, add-pass behavior, and lecture metadata placement.
- Do not push, deploy, restart the NUC, or alter unrelated untracked files.

---

### Task 1: Validated exam and pass date APIs

**Files:**
- Modify: `tests/v2/test_pass_tracker.py`
- Modify: `src/oms_hub/web/schemas.py`
- Modify: `src/oms_hub/repositories.py`
- Modify: `src/oms_hub/web/routes.py`

**Interfaces:**
- Consumes: existing `CatalogRepository.list_exam_lectures()` and pass PATCH route.
- Produces: `ExamDateUpdate.exam_date: date`; `LecturePassUpdate.completed_on: date | None`; `CatalogRepository.update_exam_date(subject: str, exam_number: int, exam_date: str) -> str`; `PATCH /api/lectures/exams/{exam_number}/date?subject=...` returning `{"exam_date":"YYYY-MM-DD"}`.

- [ ] **Step 1: Write failing API tests**

Add tests that prove an explicit `completed_on` replaces a stored pass date, invalid dates return 422 without mutation, an exam-date PATCH updates two target lectures but not a neighboring exam or similarly named subject, missing CSRF returns 403, and conflicting pre-existing dates render no authoritative date.

```python
changed = client.patch(
    f"/api/lectures/{lecture_id}/passes/1",
    json={"completed_on": "2026-08-29"},
    headers=headers,
)
assert changed.json()["completed_on"] == "2026-08-29"

scheduled = client.patch(
    "/api/lectures/exams/1/date",
    params={"subject": "Neuro"},
    json={"exam_date": "2026-09-18"},
    headers=headers,
)
assert scheduled.json() == {"exam_date": "2026-09-18"}
```

- [ ] **Step 2: Run the focused API tests and verify RED**

Run: `.venv/bin/pytest -q tests/v2/test_pass_tracker.py`

Expected: failures because `completed_on`, `ExamDateUpdate`, the bulk repository method, and exam-date endpoint do not exist.

- [ ] **Step 3: Implement the minimum validated contracts**

```python
class LecturePassUpdate(BaseModel):
    completed: bool = False
    completed_on: date | None = None
    resource: str | None = Field(default=None, max_length=100)


class ExamDateUpdate(BaseModel):
    exam_date: date
```

Treat `completed_on` field presence as an explicit replacement and retain the existing server-timezone fallback for legacy `{"completed": true}` clients. Reject requests that contain both completion fields. Replace the repository `coalesce` only for explicit date ownership; resource-only writes must not touch the date.

```python
def update_exam_date(self, subject: str, exam_number: int, exam_date: str) -> str:
    with self.database.session() as session:
        result = session.execute(
            update(LectureModel)
            .where(
                LectureModel.subject == subject,
                LectureModel.exam_number == exam_number,
            )
            .values(exam_date=exam_date)
        )
        if not result.rowcount:
            raise KeyError((subject, exam_number))
    return exam_date
```

- [ ] **Step 4: Run focused API tests and verify GREEN**

Run: `.venv/bin/pytest -q tests/v2/test_pass_tracker.py`

Expected: all tests pass with CSRF and invalid-date coverage.

---

### Task 2: Editable lecture pass dates with a browser-local default

**Files:**
- Modify: `tests/js/lecture.test.js`
- Modify: `tests/v2/test_lecture_generation_ui.py`
- Modify: `src/oms_hub/web/templates/lecture.html`
- Modify: `src/oms_hub/web/static/lecture.js`
- Modify: `src/oms_hub/web/static/app.css`

**Interfaces:**
- Consumes: Task 1's `completed_on` PATCH field.
- Produces: exported `localDateValue(date = new Date()) -> YYYY-MM-DD`; enabled `input[data-pass-date]` for completed rows; checkbox PATCH body `{"completed_on":"YYYY-MM-DD"}`; date-change PATCH body with the edited value.

- [ ] **Step 1: Write failing template and JavaScript tests**

Name the breaks: a checkbox sends no browser date, an ISO date can be shifted through UTC conversion, a completed date is not editable, an unchecked date is enabled, or a failed edit loses the prior value.

```javascript
assert.equal(lecture.localDateValue(new Date(2026, 7, 31, 23, 30)), "2026-08-31");
assert.deepEqual(JSON.parse(completionRequest.options.body), {
  completed_on: "2026-08-31",
});
assert.deepEqual(JSON.parse(dateEditRequest.options.body), {
  completed_on: "2026-08-29",
});
```

- [ ] **Step 2: Run lecture UI tests and verify RED**

Run: `node --test tests/js/lecture.test.js && .venv/bin/pytest -q tests/v2/test_lecture_generation_ui.py`

Expected: failures because the date remains a `<time>` node and the browser-local helper/date-edit handler do not exist.

- [ ] **Step 3: Implement the native date field and minimal handlers**

Render a real labeled date input. It is disabled and empty while incomplete; checking sets `localDateValue()`, enables the field, and PATCHes it; unchecking PATCHes `{"completed": false}`; editing while checked PATCHes the chosen value. Disable only the field being saved, restore its previous value after failure, and keep resource rendering independent.

```javascript
const localDateValue = (value = new Date()) => {
  const pad = (part) => String(part).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
};
```

- [ ] **Step 4: Run lecture UI tests and verify GREEN**

Run: `node --test tests/js/lecture.test.js && .venv/bin/pytest -q tests/v2/test_lecture_generation_ui.py`

Expected: all tests pass, including failure rollback and independent resource updates.

---

### Task 3: Exam countdown card and Set/Change Exam Date modal

**Files:**
- Create: `tests/js/exam_passes.test.js`
- Create: `src/oms_hub/web/static/exam_passes.js`
- Modify: `tests/v2/test_lecture_generation_ui.py`
- Modify: `tests/v2/test_ui_design_standard.py`
- Modify: `src/oms_hub/web/templates/exam_passes.html`
- Modify: `src/oms_hub/web/static/app.css`
- Modify: `src/oms_hub/web/templates/base.html`

**Interfaces:**
- Consumes: Task 1's exam-date endpoint and route context `exam_date` / `exam_date_conflict`.
- Produces: `localDateAtEight("YYYY-MM-DD") -> Date`; `countdownParts(examDate, now) -> {days, hours, state}`; page initializer that saves the modal date, updates the card, and refreshes the countdown once per minute.

- [ ] **Step 1: Write failing countdown and rendered-page tests**

Use literal expected values around the 8:00 AM boundary and assert the card precedes the existing lecture table.

```javascript
assert.deepEqual(
  examPasses.countdownParts("2026-09-02", new Date(2026, 7, 31, 9, 0)),
  { days: 1, hours: 23, state: "future" },
);
assert.deepEqual(
  examPasses.countdownParts("2026-08-31", new Date(2026, 7, 31, 8, 1)),
  { days: 0, hours: 0, state: "exam-day-reached" },
);
```

- [ ] **Step 2: Run exam-page tests and verify RED**

Run: `node --test tests/js/exam_passes.test.js && .venv/bin/pytest -q tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py`

Expected: failures because the module, card, dialog, and rendered date context do not exist.

- [ ] **Step 3: Implement the approved mock with shared components**

Insert the card before `data-exam-pass-overview`; use a semantic `dl`, static `Target · 8:00 AM`, visible date/status, and a Set/Change button wired to the shared modal shell. On first open, seed the native date field with `localDateValue()`; save by PATCH with CSRF; update numbers with the existing `.t-number` reflow; keep ticking numbers out of live regions. Add only page-local responsive styles and bump the existing static asset version.

- [ ] **Step 4: Run exam-page tests and verify GREEN**

Run: `node --test tests/js/exam_passes.test.js && .venv/bin/pytest -q tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py`

Expected: all tests pass with exact 8:00 AM math, no UTC date-only parsing, correct modal semantics, and preserved table behavior.

---

### Task 4: Integrated verification and browser acceptance

**Files:**
- Verify only: all files changed by Tasks 1–3.

**Interfaces:**
- Consumes: completed backend and frontend behavior.
- Produces: local evidence only; no push or deployment.

- [ ] **Step 1: Run focused and broad automated checks**

```bash
.venv/bin/pytest -q tests/v2/test_pass_tracker.py tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py
node --test tests/js/lecture.test.js tests/js/exam_passes.test.js
.venv/bin/ruff check src/oms_hub/repositories.py src/oms_hub/web/routes.py src/oms_hub/web/schemas.py tests/v2/test_pass_tracker.py tests/v2/test_lecture_generation_ui.py
.venv/bin/mypy src/oms_hub/repositories.py src/oms_hub/web/routes.py src/oms_hub/web/schemas.py
```

- [ ] **Step 2: Run one bounded desktop/mobile browser pass**

Start the local Study Hub against temporary sample data, then verify: first date-modal open defaults to today's browser-local date; a saved future exam counts to 8:00 AM; checking a new pass stamps today's date; changing that date persists after reload; invalid date/API errors preserve prior UI state; desktop and 390px layouts have no page-level overflow.

- [ ] **Step 3: Run final design and security checks**

Run the Impeccable detector once on the changed template/CSS/JavaScript targets. Confirm all state-changing requests carry CSRF, Pydantic validates dates, SQLAlchemy statements are parameterized, no new dependency or secret exists, and no countdown tick uses `aria-live`.

- [ ] **Step 4: Request independent review**

Give the reviewer the exact diff and focused/broad/browser evidence. Resolve only concrete findings, rerun affected checks, and report the final local tree without pushing or deploying.
