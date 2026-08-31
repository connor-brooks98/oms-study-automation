# Task 3: Exam countdown and exam-date dialog

## RED

- Test-only commit: `0ff051dc7f3d2fc0ca2f101f701d78cebcad1454` (`test: cover exam countdown pass date UI`).
- `node --test tests/js/exam_passes.test.js` failed as expected because `src/oms_hub/web/static/exam_passes.js` did not exist.
- `.venv/bin/pytest -q tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py` failed as expected because the countdown card, date dialog, and shared-component markup did not exist.

## GREEN

- Added page-local `exam_passes.js` with local-calendar 8:00 AM construction, countdown state handling, CSRF PATCH save, first-open date seeding, safe native-picker attempt, and one-minute refresh.
- Added the card before the pass overview, semantic metrics, authored clock SVGs, a shared-shell modal, and only scoped responsive CSS. The live region is limited to save/error feedback; ticking metrics are not announced.
- Bumped the shared static asset version to `20260831.1`.

## Verification

- `node --test tests/js/exam_passes.test.js`: 3 passed.
- `node --check src/oms_hub/web/static/exam_passes.js`: passed.
- `.venv/bin/pytest -q tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py`: 44 passed.

## Fix Round 2

### RED

- Added a rendered-template uniqueness regression and changed the JS fixture to expose only the visible date-label hook.
- `node --test tests/js/exam_passes.test.js` produced 3 passing tests and 1 expected failure: the label remained empty because the initializer still queried the storage hook.
- `.venv/bin/pytest -q tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py` produced the expected failure: two elements matched `[data-exam-date]`.

### GREEN

- Kept `data-exam-date` exclusively on the page storage article, renamed the visible element to `data-exam-date-label`, and updated the initializer selector.
- `node --test tests/js/exam_passes.test.js`: 4 passed.
- `.venv/bin/pytest -q tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py`: 44 passed.
- `git diff --check`: passed.
- `git diff --check`: passed.

## Files changed

- `tests/js/exam_passes.test.js`
- `tests/v2/test_lecture_generation_ui.py`
- `tests/v2/test_ui_design_standard.py`
- `src/oms_hub/web/static/exam_passes.js`
- `src/oms_hub/web/templates/exam_passes.html`
- `src/oms_hub/web/static/app.css`
- `src/oms_hub/web/templates/base.html`

## Self-review and concerns

- Reviewed the final diff for local-date parsing, endpoint scope, CSRF, card placement, modal-shell reuse, motion reuse, and live-region scope. The test fixture now calls the initializer cleanup so its minute interval cannot keep Node alive.
- No known implementation concerns. Browser-level visual and native-date-picker acceptance remain outside these focused automated tests.

## Fix Round 1

### RED

- Added the preloaded-date regression case to `tests/js/exam_passes.test.js`.
- `node --test tests/js/exam_passes.test.js` produced 3 passing tests and 1 expected failure: opening a saved exam left the date input blank instead of `2026-09-02` (and did not synchronize the dialog title).

### GREEN

- Every modal open now assigns `input.value = page.dataset.examDate || localDateValue(now())`, so cancelled edits cannot survive the next open.
- Added `data-exam-date-dialog-title` and synchronizes it to `Set exam date` or `Change exam date` on open. Native `showPicker()` behavior and the missing-date default are unchanged.
- `node --test tests/js/exam_passes.test.js`: 4 passed.
- `.venv/bin/pytest -q tests/v2/test_lecture_generation_ui.py tests/v2/test_ui_design_standard.py`: 44 passed.
