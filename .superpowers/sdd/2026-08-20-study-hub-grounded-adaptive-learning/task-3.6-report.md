# Task 3.6 implementation report — scoped Ask persistence

## Status

`DONE_WITH_CONCERNS`: the repository and focused tests are implemented and green within
the four owned files. Central ORM models, migration/version bump, app registration,
routes, dependencies, flags, schemas, shared contracts, and Ask v2 remain held and
unapplied as required by the Task 3.6 boundary.

## Identity and scope

- Worktree: `/Users/connor/Developer/worktrees/oms-study-automation-grounded-learning/sol3`
- Branch: `sol3/ask-backend`
- Exact base: `1c9f12117bae791086732f198a5fc7268fa566d0`
- Base tree: `170b1f7e042cd1bab00d24108c3dbe1d066d8f15`
- Original code/test commit: `c1090db59d1f6e8760e3b509617ec1bf8a7c280b` / tree
  `d04ed9c1116dc3ddaf8b7da471c43f7a7edbc811`.
- Quality fix code/test commit: `99b07d6552b28ead09a3bc9f8227c34918d28854` / tree
  `efbe8bff461ed54be7c50f3e8dcfb0aef20131e6`.
- Duplicate-revision fix code/test commit: `2f401796a9bb3cc2dd1c84273cb0ea65dd4fac35`
  / tree `2507ce27be864c0f96a1eaebb7110f5e8bffe6c9`.
- Final Sol RED test checkpoint: `d33f936355848fb19ab683249095818e96647163` / tree
  `bcdd98c234ff18daf261dbc3684cce047007fb8f`.
- Final Sol corruption/rollback fix code/test commit: `1c01665a4d34616bf38e0286caacb9a9db4f1efc`
  / tree `1243beb7beea13a7506de7cbf99cef2c5646a1c1`.
- Required code subject: `feat: persist scoped Ask conversations and retrieval traces`
- Prior documentation commit: `8b24c7e93ac83b6eec70418ea011decef3618221` / tree
  `57dabe1e4f7e3e2e8817885c3fdfe2740e79c2ac`.
- Terra fix-round documentation commit: `SELF`; tree: `SELF_TREE`.
- Resolve `SELF` and `SELF_TREE` from the containing commit after creation with
  `git rev-parse HEAD` and `git rev-parse 'HEAD^{tree}'`; the placeholders are
  intentionally self-referential and cannot contain their own final identities.
- Review-record documentation commit: `SELF`; tree: `SELF_TREE`.

Only these runtime/test files were changed:

- `src/oms_hub/ask/repository.py`
- `tests/ask/test_repository.py`

The handoff and this report are the separately owned documentation files. No central
model/migration/app/registration/shared-contract/schema/route/flag/dependency file was
modified.

## Implementation

`AskRepository` uses `Database.session()` for every operation. Its private SQLAlchemy
metadata is intentionally local because central model registration and migrations are
held for Sol-0. Constructing the repository idempotently creates these logical tables in
isolated tests:

- `ask_threads`: actor owner, accepted mode, canonical JSON scope/context, timestamps,
  and a database-side message sequence counter.
- `ask_messages`: actor owner, role/content, per-thread sequence, and creation time.
- `retrieval_runs`: immutable source snapshot hash, provider request ID, prompt/schema/model
  versions, required bounded non-empty string validation outcome, expected evidence-link
  cardinality, and timestamp.
- `retrieval_evidence`: the single canonical retrieval-run representation of paired ordinal,
  opaque evidence ID, and source revision ID links.

The repository consumes the accepted Task 3.1 `AskMode`, `AskThread`, `AskMessage`,
`AskPageContext`, `QuizPageContext`, and frozen `RetrievalScope` without changing them.
It returns immutable `AskThreadView` and `RetrievalRun` dataclasses for the repository
surface. Thread reads and writes require the actor; missing and unauthorized IDs both
raise `KeyError`. Exact actor-plus-scope listing prevents implicit cross-scope selection.
Quiz context is fixed at thread creation; every append to a quiz thread must provide an
explicit equal `QuizPageContext`. Message sequence is allocated by atomic database-side
counter update; retrieval creation time plus stable ID provides deterministic ordering.

The only retention surface is `delete_threads_before(actor_id, before)`. It accepts only
strict timezone-aware `datetime`/ISO values and normalizes the cutoff to UTC. It validates
every owned thread timestamp as a strict UTC instant before selecting or deleting, so a
malformed persisted value rolls back the whole operation. Deletion removes owned messages,
retrieval runs, and retrieval links explicitly. It never queries or deletes canonical
evidence/source tables, and no scheduler or retention policy was added. Retrieval runs
have no update method, link rows are the sole persisted evidence/source-revision
representation, and reads require stored expected cardinality plus contiguous ordinals.
Opaque IDs, one-to-one pairing, pinned thread source scope, bounded provenance fields, and
decoded persisted values are validated fail closed. A default empty thread revision tuple
represents a broad scope and does not itself reject bounded provenance.

## TDD evidence

The tests were written before the repository module:

```text
PYTHONPATH=$PWD/src:$PWD uv run --with pytest --with sqlalchemy --with pydantic \
  --no-project python -m pytest tests/ask/test_repository.py -q
```

RED: exit `1` during collection with
`ModuleNotFoundError: No module named 'oms_hub.ask.repository'`.

After the minimum implementation, the focused suite was GREEN with `7 passed`. During
that first green run, a stored pre-submit quiz context round-tripped with optional
answer-bearing `null` fields and was rejected by the accepted Task 3.1 validator. The
minimal correction serialized pre-submit context with `exclude_none=True`; the final
focused suite remained `7 passed`.

## Terra specification fix round 1

Terra reviewed candidate `a7978df8477865eee3d0498f1988bd47ae4dd47a` / tree
`c277a425bab24819139c19428d1e6c5952c99c83` and required two corrections:

1. A stored `QuizPageContext` could be appended to without a page context, so both
   append methods must require an explicit equal `QuizPageContext` on every quiz-thread
   append; a different question context must remain rejected.
2. `validation_outcome: object` allowed raw evidence dictionaries to be serialized;
   the repository must require and persist only a non-empty string outcome.

The RED correction tests were added before the runtime fix:

```text
PYTHONPATH=$PWD/src:$PWD uv run --extra dev --extra document-processing --extra pdf-inspection \
  python -m pytest tests/ask/test_repository.py -q
```

Result: `2 failed, 7 passed`; failures were omitted quiz context and unconstrained
mapping/empty validation outcome.

The minimum code/test correction is commit
`c1090db59d1f6e8760e3b509617ec1bf8a7c280b` / tree
`d04ed9c1116dc3ddaf8b7da471c43f7a7edbc811` (`fix: harden scoped Ask persistence`). It
uses one shared append guard for user and assistant messages, stores a plain
`validation_outcome` text column, and rejects non-string/empty outcomes at the boundary.

Terra scoped specification re-review at `b5fd5ca5da4302dafc993b38d352b4829e1e9708` /
tree `b4e15f08438663036bbc06fdce20cd001c2ec87c`: **CHANGES REQUIRED**, solely for the
remaining documentation identity Minor. The two Important behavioral findings were
addressed in the code/test fix; this new docs-only correction replaces the pending
identity with `SELF`/`SELF_TREE` and addresses that Minor. A fresh Terra re-review remains
pending; no approval is claimed here.

## Terra quality fix round 1

Terra quality/reliability/security review at `5c8a35d99679fcc4242ad6e49e6c137142d4dd45`
/ tree `334edd81e55a34f622c4b6cdbb66d9ce297c8b09` returned **CHANGES REQUIRED** for four
Important findings:

1. Concurrent appends raced on `MAX(sequence)` and could fail on the unique sequence
   constraint.
2. Retention accepted arbitrary strings such as `zzzz`, allowing lexical over-deletion;
   cutoffs needed strict timezone-aware ISO parsing and UTC normalization.
3. Retrieval provenance accepted unbounded/private IDs, unpaired evidence/revision lists,
   out-of-scope revisions, and insufficiently bounded provenance fields.
4. Duplicated evidence/revision JSON could diverge from link rows, and corrupted persisted
   values were not validated before return.

RED coverage was added before the correction:

```text
PYTHONPATH=$PWD/src:$PWD .venv/bin/pytest tests/ask/test_repository.py -q
```

Result: `5 failed, 8 passed`, covering all four findings. The quality fix is
`99b07d6552b28ead09a3bc9f8227c34918d28854` / tree
`efbe8bff461ed54be7c50f3e8dcfb0aef20131e6` (`fix: harden Ask persistence concurrency and provenance`).
It uses an atomic `UPDATE ... RETURNING` thread counter, strict aware cutoff parsing,
bounded opaque provenance IDs with one-to-one scope-checked links, and link-only
retrieval reconstruction with fail-closed persisted-value validation. A Terra quality
re-review remains pending; no approval is claimed here.

## Terra quality fix round 2

Quality re-review of `99b07d6552b28ead09a3bc9f8227c34918d28854` / tree
`efbe8bff461ed54be7c50f3e8dcfb0aef20131e6` returned **CHANGES REQUIRED** for the
duplicate-revision Important. EvidenceRef semantics permit distinct evidence IDs to
share one source revision, so rejecting duplicate `source_revision_ids` was too strict.
The controller ruling is also explicit: Task 3.6 lacks the blocked Source Trust/context
index needed to prove that a revision belongs to a broader course/exam/lecture scope.
When `thread.scope.source_revision_ids` is empty, this repository accepts bounded paired
provenance but cannot claim source-trust membership; that validation remains a held
Task 3.2/3.7 integration prerequisite.

RED coverage was added before the correction:

```text
PYTHONPATH=$PWD/src:$PWD .venv/bin/pytest tests/ask/test_repository.py -q
```

Result: `2 failed, 13 passed`; both failures were duplicate source revisions in paired
provenance, including the empty revision-scope case. The correction is
`2f401796a9bb3cc2dd1c84273cb0ea65dd4fac35` / tree
`2507ce27be864c0f96a1eaebb7110f5e8bffe6c9` (`fix: allow shared source revisions in Ask traces`).
It allows repeated source-revision IDs while preserving unique evidence IDs, positional
pairing, opaque validation, and pinned-scope membership checks. Fresh quality re-review
is pending; no approval is claimed.

## Final Terra review record

- Terra specification review: **APPROVED** at `5c8a35d99679fcc4242ad6e49e6c137142d4dd45` /
  tree `334edd81e55a34f622c4b6cdbb66d9ce297c8b09`. The historical specification
  **CHANGES REQUIRED** rounds above remain preserved.
- Terra quality/reliability/security review: **APPROVED** at
  `8b24c7e93ac83b6eec70418ea011decef3618221` / tree
  `57dabe1e4f7e3e2e8817885c3fdfe2740e79c2ac`, with the accepted ruling that an empty
  revision tuple requires held Task 3.2/3.7 effective Source Trust validation.
- Fresh Workstream Sol final review remains **PENDING**; this record does not claim
  Task 3.6 completion.

## Final Sol fix wave

Workstream Sol final review of `cb4c49a3118f825fb23bc58caefd322718bd1c35` / tree
`7bacfe5c19a7f3322e7f98e8a8b60097ff4fd8a3` returned **FIX_FIRST** for two final
fail-closed persistence gaps:

1. Retention compared raw `ask_threads.created_at` strings in SQL, so a malformed
   persisted timestamp could bypass validation or allow partial deletion. All owned
   thread timestamps must be parsed as strict timezone-aware UTC instants before any
   retention decision; malformed data must roll back the entire operation.
2. Reads inferred retrieval-link cardinality from surviving rows, so terminal link loss
   could silently truncate provenance. `retrieval_runs.expected_evidence_count` must be
   persisted and reads must require exact count plus contiguous zero-based ordinals;
   persisted retrieval-run and other used/returned timestamps must fail closed too.

RED coverage was added first in `d33f936355848fb19ab683249095818e96647163` / tree
`bcdd98c234ff18daf261dbc3684cce047007fb8f`: `3 failed, 15 passed`, covering malformed
retention rollback, terminal link loss, and corrupted retrieval-run timestamps. The
GREEN correction is `1c01665a4d34616bf38e0286caacb9a9db4f1efc` / tree
`1243beb7beea13a7506de7cbf99cef2c5646a1c1` (`fix: fail closed on Ask persistence
corruption`). Both Terra exact-revision re-reviews of this final candidate—specification
and quality/reliability/security—remain **PENDING**; no prospective approval is claimed.
The earlier Terra approvals above apply only to their named prior revisions.

## Required verification evidence

Focused repository, affected Ask, and contracts:

```text
PATH=$PWD/.venv/bin:$PATH PYTHONPATH=$PWD/src:$PWD \
  .venv/bin/pytest tests/ask/test_repository.py tests/ask/test_models.py \
  tests/ask/test_intent.py tests/ask/test_leakage.py tests/contracts -q
```

Result: `151 passed` (`18` repository, `16` models, `60` intent, `16` leakage, `41`
contracts), including the final Sol corruption, rollback, and link-cardinality tests.

Ruff:

```text
.venv/bin/ruff check \
  src/oms_hub/ask/repository.py tests/ask/test_repository.py
```

Result: passed.

Source mypy:

```text
.venv/bin/mypy src
```

Result: passed, `181` source files. The first dev-only attempt reported two missing
optional parser modules; rerunning with the declared `document-processing` and
`pdf-inspection` extras passed and is the judged result.

Task-test mypy:

```text
MYPYPATH=$PWD/src PYTHONPATH=$PWD/src:$PWD .venv/bin/mypy tests/ask/test_repository.py
```

Result: passed, one file.

Isolation, deletion, immutability, concurrency, strict-retention, provenance-scope,
corruption, rollback, link-cardinality, and adversarial checks are executable in the
focused tests:
actor-filtered thread listing/read/write/delete, different quiz-question rejection,
ordered append-only messages, complete provenance round-trip, absent raw evidence
columns, canonical-evidence survival after deletion, actor-only retention, and missing
thread fail-closed behavior. `git diff --cached --check` passed before the code/test
commit. The staged code/test scope was exactly the two owned files. A source-only secret
scan found no credential, provider-secret, logger/print, private-content, or raw-evidence
handling. No provider/network call, production/private-data/Anki mutation, native
Windows acceptance, or broad PyMuPDF lane was run.

## Held integration and concerns

The exact held central proposal is recorded in
[`docs/implementation/handoffs/3.6.md`](../../../docs/implementation/handoffs/3.6.md). In brief,
Sol-0 must add central equivalents of the four local mappings in `src/oms_hub/models.py`,
add the next additive migration/version in `src/oms_hub/migrations.py` (current version
22; proposed 23), register `AskRepository(database)` in the existing repository-state
block of `src/oms_hub/app.py`, and switch the repository to the central mappings in a
separate integration patch. The proposal now uses a non-null plain-text
`validation_outcome` column rather than a JSON/object payload; it also adds the
`ask_threads.message_sequence` counter, removes duplicated retrieval evidence/revision
JSON columns, makes `retrieval_evidence.source_revision_id` non-null, and persists
`retrieval_runs.expected_evidence_count` for exact contiguous-link validation. It includes
migration/idempotence, central-table, app-state, atomic-concurrency, strict-retention
rollback on malformed stored timestamps, terminal-link loss, persisted-timestamp
corruption, provenance-scope, and no-route/no-v2 integration tests. None of those edits
were applied here. The service/context integration proposal must additionally test that,
when a thread's revision tuple is empty, each resolved `EvidenceRef` revision is checked
against the effective Source Trust course/exam/lecture scope before
`record_retrieval_run`; Task 3.6 cannot claim this blocked index membership check.

The local mappings are therefore a deliberate testable boundary adapter, not a second
production schema. The `KeyError` equivalence for missing and unauthorized IDs is a
deliberate ownership-oracle defense. Ask v2 remains proposal-only; Tasks 3.2, 3.4, 3.5,
3.7, and 3.8, integration, merge, push, deploy, and production/Anki work were not
started.
