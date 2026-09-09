# Released-library navigation and smooth ordering — 2026-09-08

Implemented in `codex/library-navigation-and-reorder`, based on production/main `59b4a645c0c623556bc1115cdc2dc378e4cfc3f2`. This follow-up has not been pushed, integrated, or deployed. No NUC files, processes, runtime, or Gate 2B evidence were changed.

## Result

The Quiz Builder's **Manage released libraries** control now opens an existing-style native chooser with **Quizzes** and **Practice Questions**. Each private manager also has an obvious page-level switch between those libraries. Existing public links, management routes, filtering, authentication, and CSRF boundaries are preserved.

The private managers use locally vendored [SortableJS 1.15.7](https://github.com/SortableJS/Sortable/tree/1.15.7), under its MIT license, for vertical dragging. Rows shift during movement; a raised preview follows the pointer, with a placeholder and automatic scrolling. Pointer/touch dragging and Arrow Up/Down work within the same exam. Reduced-motion preferences disable Sortable animation. Escape, pointer cancellation, and legacy touch cancellation restore the starting order without saving.

A changed drop immediately sends one `PATCH /api/published-quizzes/{token}/order` with `{ordered_tokens: [...]}`. Keyboard moves use that same path. The server derives the current canonical subject/exam/library section from the selected publication, requires an exact duplicate-free permutation of every active member, and writes contiguous order values in one transaction. SQLite acquires `BEGIN IMMEDIATE` before reading. Invalid/mixed bodies return 422; stale/incomplete/foreign membership returns 409; an unknown publication returns 404. The old `{direction: "up" | "down"}` API and response remain compatible with already-open older pages.

All buttons and form controls in the exam list are locked while saving, preserving their previous disabled states. Keyboard input during an active pointer drag cannot start another request. Success validates and applies the authoritative returned order in place; there is no success reload. On failure, the previous visible order is restored when possible and the UI clearly says persistence could not be confirmed and asks the user to refresh. It never claims a server rollback or automatically retries a mutation.

Two valid concurrent full-order writes with unchanged membership serialize with last-writer-wins behavior. This change does not introduce multi-user revision conflict detection. Newly published, moved, or removed membership is checked inside the write transaction and cannot be silently omitted by a stale request.

## Verification

- 125 Python tests passed across repository, public routes, Quiz Builder routes, and UI design-standard coverage.
- Final full JavaScript suite: 248 passed; focused library suite: 33 passed.
- Ruff, MyPy for both changed Python source files, JavaScript syntax, and `git diff --check` passed.
- Independent Sol backend review approved the current source, including rollback, canonical-scope isolation, CSRF/auth, legacy compatibility, and 25 forced concurrent-writer rounds.
- Independent Sol frontend/navigation review approved after correcting keyboard-during-drag and legacy touch-cancellation edges.
- Final isolated Chrome checks used the real app routes and repository through a fresh TestClient per request, synthetic quizzes, a temporary SQLite database, and no worker lifespan or provider access. These runs supersede earlier shared-client output: the fixture had leaked cookies between isolated browser contexts, and its loose success-text assertion also matched failure guidance. The corrected checks require the exact success message and persistence after reload; all four real reorder requests returned HTTP 200. They verified the library chooser, practice filtering/navigation, visible neighbor movement before drop, zero saves while moving, one request on drop, persisted order after page reload, keyboard persistence, failed-save restoration, reduced motion, pending controls, no-op drops, Escape, automatic scrolling, mobile touch movement, native touch cancellation, and blocked keyboard saves during dragging. No browser JavaScript errors were observed.

Runnable checks from the worktree with the project environment:

```sh
PYTHONPATH=src /Users/connor/Developer/oms-study-automation/.venv/bin/python -m pytest -q tests/study_generation/test_repository.py tests/v2/test_public_quiz_routes.py tests/v2/test_quiz_builder_routes.py tests/v2/test_ui_design_standard.py
node --test tests/js/*.test.js
```

The worktree reuses `/Users/connor/Developer/oms-study-automation/.venv/bin/python`; it has no new dependency environment. Browser fixture scripts, JSON results, request evidence, and screenshots remain under `tmp/library-preview/`. They are local acceptance artifacts, not production files. The fixture uses only localhost port 4567 and temporary synthetic data; it is not the NUC Hub.

## Release and coordination

This change needs no schema migration, Python dependency update, Node build, or Cloudflare configuration change. The pinned vendor asset and license/provenance files must ship with the templates and JavaScript. Existing content-addressed library asset URLs refresh the changed JS/CSS after deployment; the vendor filename pins its version. Sortable is loaded only in management mode.

The main orchestrator is **Audit Sol 1–10 handoff state** (`01a079db-cc3b-7e82-934a-d6906e46c517`), with SSH access via `nuc`. Preserve all task28/C5/private/control/diagnostic exclusions, especially `task28-c5-compact-d2c3afbf-v2`, its partial predecessor, both native roots, and the shared old runtime. Do not interrupt or change a runtime while its authorized C5 proof is pending. This UI work supplies no Gate 2B acceptance and authorizes no provider-proof retry.

Before a later authorized production rollout, verify current main/NUC identities and proof ownership, use the existing guarded source-only release and backup process, and verify native readiness, exact commit/tree, one owned listener, workers, vendor availability, and both manager pages. Do not reorder real quiz data merely to repeat the synthetic browser proof.
