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
- Code/test commit: `25e6f8cd60c0221dd8fd136f3ab277230655652b`
- Code/test tree: `a4fe57ffe96f54f7e20793b1129c92b406ba9a25`
- Required code subject: `feat: persist scoped Ask conversations and retrieval traces`
- Documentation commit: pending; resolved after the separate docs commit.

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

- `ask_threads`: actor owner, accepted mode, canonical JSON scope/context, and timestamps.
- `ask_messages`: actor owner, role/content, per-thread sequence, and creation time.
- `retrieval_runs`: immutable source snapshot hash, evidence/source-revision ID arrays,
  provider request ID, prompt/schema/model versions, validation outcome, and timestamp.
- `retrieval_evidence`: retrieval-run links containing ordinal, evidence ID, and source
  revision ID only.

The repository consumes the accepted Task 3.1 `AskMode`, `AskThread`, `AskMessage`,
`AskPageContext`, `QuizPageContext`, and frozen `RetrievalScope` without changing them.
It returns immutable `AskThreadView` and `RetrievalRun` dataclasses for the repository
surface. Thread reads and writes require the actor; missing and unauthorized IDs both
raise `KeyError`. Exact actor-plus-scope listing prevents implicit cross-scope selection.
Quiz context is fixed at thread creation and an explicitly supplied append context must
match it. Message sequence and retrieval creation time plus stable ID provide
deterministic ordering.

The only retention surface is `delete_threads_before(actor_id, before)`. Deletion removes
owned messages, retrieval runs, and retrieval links explicitly. It never queries or
deletes canonical evidence/source tables, and no scheduler or retention policy was added.
Retrieval runs have no update method, and no raw evidence field exists in either local
retrieval table. The source snapshot, evidence IDs, source revision IDs, provider request
ID, prompt/schema/model versions, and validation outcome are round-tripped unchanged.

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

## Required verification evidence

Focused repository, affected Ask, and contracts:

```text
PYTHONPATH=$PWD/src:$PWD uv run --extra dev --extra document-processing --extra pdf-inspection \
  python -m pytest tests/ask/test_repository.py tests/ask/test_models.py \
  tests/ask/test_intent.py tests/ask/test_leakage.py tests/contracts -q
```

Result: `140 passed` (`7` repository, `16` models, `60` intent, `16` leakage, `41`
contracts).

Ruff:

```text
uv run --with ruff --no-project ruff check \
  src/oms_hub/ask/repository.py tests/ask/test_repository.py
```

Result: passed.

Source mypy:

```text
PYTHONPATH=$PWD/src:$PWD uv run --extra dev --extra document-processing --extra pdf-inspection mypy src
```

Result: passed, `181` source files. The first dev-only attempt reported two missing
optional parser modules; rerunning with the declared `document-processing` and
`pdf-inspection` extras passed and is the judged result.

Task-test mypy:

```text
MYPYPATH=$PWD/src PYTHONPATH=$PWD/src:$PWD \
  uv run --extra dev --extra document-processing --extra pdf-inspection \
  mypy tests/ask/test_repository.py
```

Result: passed, one file.

Isolation/deletion/immutability/adversarial checks are executable in the focused tests:
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
separate integration patch. The proposal includes migration/idempotence, central-table,
app-state, and no-route/no-v2 integration tests. None of those edits were applied here.

The local mappings are therefore a deliberate testable boundary adapter, not a second
production schema. The `KeyError` equivalence for missing and unauthorized IDs is a
deliberate ownership-oracle defense. Ask v2 remains proposal-only; Tasks 3.2, 3.4, 3.5,
3.7, and 3.8, integration, merge, push, deploy, and production/Anki work were not
started.
