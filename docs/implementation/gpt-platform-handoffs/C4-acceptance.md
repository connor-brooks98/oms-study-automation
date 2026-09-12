# C4 local implementation receipt

Status: owned implementation complete; O's shared application integration and independent acceptance are separate. No live model/provider, account, vendor, Anki, paid API, deployment, push, or service restart was performed.

## Exact implementation

Apply in order (O already integrated the first two slices):

- `fc8a20d383240a5a793e0e04d2331aa9beef4429`: accepted native blocks and immutable mixed-publication sessions. Tree `b4a24ddf7b9f3592eaa6af125c67c14d9d8bfda3`.
- `7cf915721fb62f266fa09f3aa7988008d173450e`: final course/exam/reviewed-topic eligibility checked inside existing `BEGIN IMMEDIATE` creation transaction; exclude publications missing scope. Tree `fa1bffa9fc326a275e96eac6ed2d46503c0bd248`.
- `e8f389e41a5bf041b6bfc664d2f8951d218fcac8`: authenticated block/review routes and forms; frozen grounded suggestions, explicit review, recovery, tests and exact player patch. Tree `57f82f8f4c84de82372a912c768951878fcac17a`.

Prerequisites already supplied and independently reviewed by O: schema40 `8367d8cd64d80d311cbae42eab2133783cd1f1b0`; Q native projection `70119d4f953e17cfb8367eac19ca26e200405116`; accepted C3 correction in `fd6922f2996cf58c2965f4f21616b0ce2631476b`.

## Behavior and trust boundaries

Blocks resolve current accepted native publications and complete media. Results-only vendor rows cannot become playable; Q ready metadata supplies only accepted topic labels. Course/exam selection is explicit; no inferred scope. Empty scope is excluded. Legacy lecture scope normalization belongs to O's existing catalog-backed callback, without payload changes.

Deterministic order is unseen, then lower sample accuracy, fewer graded attempts, stable key. Counts are shown; this is a study heuristic. Exact duplicate candidates collapse; contradictory metadata fails. Session rows retain token/version/full canonical publication digest/original question ID; delivery IDs are unique across publications. Creating a block checks eligibility again while holding the existing writer lock. Changing scope or removing an accepted topic between catalog selection and creation creates no session. Resume retains the original references, and stale/deleted/missing-media sources fail closed. Previous C3 staged-answer/receipt behavior remains.

Manual review and suggestions use Q's real immutable native projection and hash-checked `set_topics`, never a second question or attempt store. Suggestions freeze official taxonomy IDs, source text/objective excerpts, digest, media hashes, selected model, and question reference. Shared B client lifecycle and raw response are durable; raw output is not included in HTTP views. Unknown taxonomy IDs, duplicate tags, missing evidence quotes, changed source, wrong lifecycle, and invalid output cannot become accepted analytics. Every accepted model label requires a separate explicit checked selection. Source quote membership is enforced; semantic relevance and model confidence are not independently calibrated.

Pending/running requests can be interrupted; startup reconciliation marks only the configured owner's pending/running rows interrupted without replay. Completed requests return their saved output. Failed/interrupted work cannot dispatch again. Provider-supplied reset time is preserved; no reset time is invented. Model absence leaves manual review and blocks available. All mutations use existing owner/CSRF checks; private views use no-store.

## O-owned application/player integration

```python
from oms_hub.study_progress.blocks import BlockService
from oms_hub.study_progress.tags import TopicService

app.state.study_block_service = BlockService(
    sessions=app.state.study_session_service,
    bank=app.state.question_bank,
    publications_for=owner_accessible_native_publications,
)
app.state.study_topic_service = TopicService(
    blocks=app.state.study_block_service,
    media_root=resolved.data_dir.resolve(),
    client=app.state.codex_session,
    model=lambda: app.state.codex_model,
)
# Startup, after managed-process reconciliation, before serving requests:
app.state.study_topic_service.interrupt_pending(study_owner)
```

`owner_accessible_native_publications(owner)` must enforce the fixed private owner and current accepted native publication access. Reuse existing catalog normalization for legacy lecture scope; do not infer standalone scope. Media root is trusted server configuration, never request data. Existing session publication/media callbacks remain authoritative.

Apply `C4-player-source-label.patch` to the shared player and its existing test file. SHA256: `e430493b651d1e5e5f6974a8c4713dd7574763c3f193bf8c99aa70ad824bca9e`. It adds a plain-text source label only in personal-session mode. C did not edit shared app/player/models/repository files; prerequisite Q/schema changes were O-approved commits.

## Verification

Using `/Users/connor/Developer/oms-study-automation/.venv/bin/` and `/opt/homebrew/bin/node`:

```sh
PYTHONPATH=src python -m pytest tests/study_progress tests/question_bank -q -o addopts=''
ruff check src/oms_hub/study_progress tests/study_progress
PYTHONPATH=src mypy --follow-imports=silent src/oms_hub/study_progress
node --test tests/js/*.test.js
```

- C/Q focused: **193 passed**.
- Ruff: all checks passed. Mypy: all seven progress modules passed.
- Existing JavaScript: **283 passed**.
- Exact player patch applied to isolated copies of actual player/test: **43 passed**, including source-label plain-text/public-mode isolation.
- `git diff --check`: passed.

`C4-browser-check.py` uses a temporary database, existing fixture app/TestClient, fake shared client, and Chrome. Every browser request is intercepted; external requests are aborted. It does not start a server or use a real provider. Run from the repository root with the patched player path (omit argument after O integrates the patch):

```sh
PYTHONPATH=src:. python docs/implementation/gpt-platform-handoffs/C4-browser-check.py /path/to/patched/public_quiz.js
```

Browser PASS: explicit cumulative preview/start, source labels, server grading, immutable receipt restore and next-unanswered resume, manual review/filter, prepare without dispatch, fake-only generate and separate accept, unavailable model, 390px mobile overflow check, zero JavaScript errors. Visual inspection verified desktop preview/review and mobile controls; existing `sh-check`/`sh-select` styles used. Screenshots are local synthetic evidence, not a live app acceptance claim:

- `/tmp/c4-block-preview.png` — SHA256 `2807f9811f85d0f205b9ef443e5f8825c8c9b6ed85ecf52b3d74310209e797ae`
- `/tmp/c4-player-source.png` — SHA256 `af38c8c6b953a79b0b55fa6cde9c6e672ccea96d2ada83d118acff0eb6ffd377`
- `/tmp/c4-pending-review.png` — SHA256 `6b49298035b05199dc824ad708dfbcdd9cd3a42e74a8d0dd484a862f76a1e3a6`
- `/tmp/c4-block-mobile.png` — SHA256 `822eb950ce568c5306cf733bca6411ac824abd00d0eec9af34e3c528526aae5f`

O's shared app wiring, final independent review, full project CI and any future live activation remain O-owned. C5 and other features are outside this handoff.
