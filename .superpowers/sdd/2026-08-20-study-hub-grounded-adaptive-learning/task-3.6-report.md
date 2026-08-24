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
- Initial implementation code/test commit: `25e6f8cd60c0221dd8fd136f3ab277230655652b` / tree
  `a4fe57ffe96f54f7e20793b1129c92b406ba9a25`.
- Specification correction code/test commit: `c1090db59d1f6e8760e3b509617ec1bf8a7c280b` / tree
  `d04ed9c1116dc3ddaf8b7da471c43f7a7edbc811`.
- Quality fix code/test commit: `99b07d6552b28ead09a3bc9f8227c34918d28854` / tree
  `efbe8bff461ed54be7c50f3e8dcfb0aef20131e6`.
- Duplicate-revision fix code/test commit: `2f401796a9bb3cc2dd1c84273cb0ea65dd4fac35`
  / tree `2507ce27be864c0f96a1eaebb7110f5e8bffe6c9`.
- Final Sol RED test checkpoint: `d33f936355848fb19ab683249095818e96647163` / tree
  `bcdd98c234ff18daf261dbc3684cce047007fb8f`.
- Final Sol corruption/rollback fix code/test commit: `1c01665a4d34616bf38e0286caacb9a9db4f1efc`
  / tree `1243beb7beea13a7506de7cbf99cef2c5646a1c1`.
- Final-quality RED test checkpoint: `bab30253970e17cc8522c245387dcea7f77f3064` / tree
  `1ec711a79a45a47d067df6b52c918292e323f842`.
- Final-quality actor/ID fix code commit: `6e655590f077c4889988fda5c6e027cd00e74cc3`
  / tree `8b6eb3f8e83343d62f1d599e45d304755bccb425`.
- Privacy RED test checkpoint: `cf0fbb18f38f932b22879efb0b38d03010a8ecdb` / tree
  `d21fb32f49ca955355fed8af8d9fe7b372bbabd6`.
- Privacy provenance fix code commit: `03b04a560239367b5c5a2b1bb594678d11caa4bc`
  / tree `82476512461a673fd0315885ac24bfcf7911fc62`.
- Canonicalization RED test checkpoint: `ca23b4341c8be32e13c491971a325b4d62797db0`
  / tree `5bd8026f4a571cd5bcb736cf23ad09ae37656ee2`.
- Canonicalization fix code commit: `03f89122d73a22139f32ae21b1685c1ad82d2f99`
  / tree `0284cdd384cb5aa165e97c3fc3f12a5e876f5598`.
- Final message-history/page-context RED test checkpoint: `0487e498bd5ae0507f9cd55ce42e3c6797bc2413`
  / tree `2adc6a6c472a39c75a20c075237676753f2b3b9f`.
- Final message-history/page-context fix code/test commit: `40da1eba3f0dea3c0acee0611288a25bd55d775a`
  / tree `987ffa2ab2510aabe6ddd214157db96d2a42e761`.
- Final message-history/page-context type-check fix code commit: `fdbb6c2aa3ba99afeec8dd50c221dc373d63dc5d`
  / tree `f53d17d98fd4ee660bda770c780f47237f602d2f`.
- Required code subject: `feat: persist scoped Ask conversations and retrieval traces`
- Prior documentation commit: `b9110923b349f19b7606cff62eb0104c8ece3b95` / tree
  `63115e089f3f9bb69be3224bbf31150b8cf96132`.
- Historical Terra fix-round docs commit identity: `SELF`; tree: `SELF_TREE`.
  Resolve this pair only from the containing historical Terra-fix docs commit with
  `git rev-parse HEAD` and `git rev-parse 'HEAD^{tree}'`; it does not identify later
  documentation commits.
- Historical review-record docs commit identity: `SELF`; tree: `SELF_TREE`.
  Resolve this pair only from the containing historical review-record docs commit;
  it does not identify this or later documentation commits.
- Final-quality docs commit: `SELF`; tree: `SELF_TREE`.
  Resolve this pair only from the containing final-quality docs commit after creation;
  do not substitute any historical `SELF` identity here.
- Privacy-fix docs commit: `SELF`; tree: `SELF_TREE`.
  Resolve this pair only from the containing privacy-fix docs commit after creation;
  do not substitute any historical `SELF` identity here.
- Canonicalization-fix docs commit: `SELF`; tree: `SELF_TREE`.
  Resolve this pair only from the containing canonicalization-fix docs commit after
  creation; do not substitute any historical `SELF` identity here.
- Final review-record docs commit: `SELF`; tree: `SELF_TREE`.
  Resolve this pair only from the containing final review-record docs commit after
  creation; it is not the identity of any historical code or review commit.
- Final message-history/page-context docs commit: `SELF`; tree: `SELF_TREE`.
  Resolve this pair only from the containing message-history/page-context docs commit
  after creation; it does not identify the code/test or review commits.
- Final message-history/page-context type-check docs commit: `SELF`; tree: `SELF_TREE`.
  Resolve this pair only from the containing type-check docs commit after creation; it
  does not identify the code/test or review commits.

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
  a database-side message sequence counter, and unique `(thread_id, actor_id)` parent key.
- `ask_messages`: actor owner, role/content, per-thread sequence, creation time, and
  composite `(thread_id, actor_id)` foreign key to the parent thread.
- `retrieval_runs`: immutable source snapshot hash, a deterministic
  `sha256:<64 lowercase hex>` provider reference derived from caller input, prompt/schema/
  model versions, a required validation status code, expected evidence-link cardinality,
  timestamp, and the same composite parent foreign key. Caller provider text is never
  persisted; validation outcomes are limited to `valid`, `invalid`, `rejected`,
  `insufficient`, or `error`.
- `retrieval_evidence`: the single canonical retrieval-run representation of paired ordinal,
  opaque evidence ID, and source revision ID links.

The repository consumes the accepted Task 3.1 `AskMode`, `AskThread`, `AskMessage`,
`AskPageContext`, `QuizPageContext`, and frozen `RetrievalScope` without changing them.
It returns immutable `AskThreadView` and `RetrievalRun` dataclasses for the repository
surface. Thread reads and writes require the actor; after parent authorization, reads load
all child rows and fail closed if any persisted child actor differs from the owner. Missing
and unauthorized IDs both raise `KeyError`. Exact actor-plus-scope listing prevents
implicit cross-scope selection. Quiz context is fixed at thread creation; every append to
a quiz thread must provide an explicit equal `QuizPageContext`. Message sequence is
allocated by atomic database-side counter update; retrieval creation time plus stable ID
provides deterministic ordering. Thread/message/retrieval IDs are bounded opaque values
matching `String(200)`, actor IDs are bounded to `String(320)` without rejecting
email-like identities, evidence/revision IDs honor `String(300)`, and provider request IDs
honor `String(500)`; persisted identities are revalidated on reconstruction.

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
Persisted `RetrievalScope` JSON must contain exactly its five canonical keys and equal the
repository's deterministic serialization on read; injected `raw_evidence` or other keys
fail closed. Persisted page context continues through Pydantic `extra="forbid"` validation.

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

## Final quality fix wave

Workstream Sol quality review of `2661427a81ee5f5ae340aabad10527928fc62a2b` / tree
`580e1fe3f1445c56126adc37ecb309c511377d3e` returned **CHANGES REQUIRED** for three
final-quality findings:

1. Reads filtered child rows by both `thread_id` and actor, so a corrupted or multiwriter
   child with a different actor could be silently omitted. Reads now load all children
   after parent authorization and fail closed; local and held central schemas bind
   `(thread_id, actor_id)` through a parent uniqueness constraint and composite FKs.
2. Thread/message IDs were not consistently bounded at every persistence boundary, actor
   IDs used the wrong bound, and evidence/revision IDs were capped below their `String(300)`
   columns. The fix validates opaque IDs at 200, actors at 320, evidence/revisions at 300,
   provider requests at 500, and persisted identities on reconstruction while allowing
   email-like actor IDs.
3. Documentation lineage conflated the initial implementation with the specification
   correction and left historical `SELF` placeholders ambiguously scoped.

RED coverage was added first in `bab30253970e17cc8522c245387dcea7f77f3064` / tree
`1ec711a79a45a47d067df6b52c918292e323f842`: `7 failed, 18 passed`, covering child-actor
omission, composite-FK rejection, actor/ID boundaries, provider-column limits, and
corrupted message IDs. The GREEN code correction is
`6e655590f077c4889988fda5c6e027cd00e74cc3` / tree
`8b6eb3f8e83343d62f1d599e45d304755bccb425` (`fix: enforce Ask child ownership and ID
boundaries`). Both Terra exact-revision re-reviews remain **PENDING**; no approval is
claimed for this candidate.

## Privacy fix wave

Fresh Terra specification and quality reviews of `0c0f2b0b3013cad18a8f8b708f6df442d8dbb0e6`
/ tree `b4fbad9076080d611ed377f87d45f5b0c32fd546` both returned **CHANGES REQUIRED** for
the same Important privacy finding: bounded `provider_request_id` and `validation_outcome`
fields still accepted arbitrary prose/private medical excerpts.

RED coverage was added first in `cf0fbb18f38f932b22879efb0b38d03010a8ecdb` / tree
`d21fb32f49ca955355fed8af8d9fe7b372bbabd6`: `4 failed, 26 passed`, covering private
medical prose on both write paths and persisted readback. The GREEN correction is
`03b04a560239367b5c5a2b1bb594678d11caa4bc` / tree
`82476512461a673fd0315885ac24bfcf7911fc62` (`fix: constrain Ask provenance privacy
fields`). It applies the 500-character opaque-ID validator to provider request IDs and
accepts only the defined validation statuses `valid`, `invalid`, `rejected`,
`insufficient`, and `error`, including persisted reconstruction. Both Terra exact-revision
re-reviews remain **PENDING**; no prospective approval is claimed.

## Final privacy/canonicalization fix wave

Fresh Terra specification and quality reviews of `414d5e8bb59a999d2e60727680d34acb4144aad1`
/ tree `9c11b6510b7eb2c090d01ec87bfa1d340e61a6fd` both returned **CHANGES REQUIRED** for
two Important canonicalization gaps:

1. A delimiter-form provider value such as `patient.has.chest.pain` satisfied the opaque
   grammar and round-tripped unchanged. Provider values are now transformed to a
   deterministic `sha256:<64 lowercase hex>` reference before persistence; reads reject
   any value that is not exactly that derived grammar, preserving correlation without
   storing caller content.
2. Persisted `scope_json` ignored unknown keys such as `raw_evidence`. Reads now require
   the exact five-key canonical `RetrievalScope` shape and a byte-for-byte canonical
   serialization round-trip. Page context retains Pydantic `extra="forbid"` behavior.

RED coverage was added first in `ca23b4341c8be32e13c491971a325b4d62797db0` / tree
`5bd8026f4a571cd5bcb736cf23ad09ae37656ee2`: `5 failed, 27 passed`, covering provider
hashing/readback grammar, scope extra-key corruption, and page-context extra-key handling.
The GREEN correction is `03f89122d73a22139f32ae21b1685c1ad82d2f99` / tree
`0284cdd384cb5aa165e97c3fc3f12a5e876f5598` (`fix: canonicalize Ask provenance and
scope`). Both Terra exact-revision re-reviews remain **PENDING**; no prospective approval
is claimed.

## Final exact-revision Terra review record

- Fresh Terra specification review: **APPROVED** for exact candidate
  `e965dce0b953673bd46ae768a78801d8e46d38a5` / tree
  `95e97d84f557d6ca64d11e11c87a1728e17a58f8`; no findings.
- Fresh Terra quality/reliability/security review (a separate review): **APPROVED**
  for the same exact candidate and tree; no findings.
- The affected verification evidence records `165 passed`; the held central
  integration proposal remains candidate-only and unapplied.
- Fresh Workstream Sol final review remains **PENDING**; this record does not claim
  Task 3.6 completion.

## Final Sol message-history/page-context fix wave

Workstream Sol final review of `238e8371d8219ebdf32c95362b98a9313808a4e7` / tree
`43d6e3b7ce4bb2f02012c4825702899e9bc65133` returned **FIX_FIRST** for two fail-closed
persistence gaps:

1. `get_thread` did not validate the stored `ask_threads.message_sequence` counter
   against all child rows. Reads now require a nonnegative integer counter, an exact
   child count, and ordered child sequences exactly `1..message_sequence`; missing,
   duplicate, noncontiguous, negative, or otherwise corrupt sequences fail closed.
2. Page-context reads accepted duplicate JSON keys and noncanonical formatting after
   strict Pydantic parsing. Reads now require the exact repository serializer byte-for-
   byte, while retaining `extra="forbid"` for hidden content.

RED coverage was added first in `0487e498bd5ae0507f9cd55ce42e3c6797bc2413` / tree
`2adc6a6c472a39c75a20c075237676753f2b3b9f`: six focused regressions failed, covering
missing/noncontiguous/corrupt message history and duplicate-key/noncanonical page
context JSON. The GREEN behavior correction is `40da1eba3f0dea3c0acee0611288a25bd55d775a`
/ tree `987ffa2ab2510aabe6ddd214157db96d2a42e761` (`fix: validate Ask message history
and page context`). The source/task-test mypy follow-up is
`fdbb6c2aa3ba99afeec8dd50c221dc373d63dc5d` / tree
`f53d17d98fd4ee660bda770c780f47237f602d2f` (`fix: type Ask persistence validation`).
Fresh exact-revision Terra specification and quality reviews, and fresh Workstream Sol
re-review of the resulting candidate, remain **PENDING**; no prospective approval is
claimed.

## Required verification evidence

Focused repository, affected Ask, and contracts:

```text
PATH=$PWD/.venv/bin:$PATH PYTHONPATH=$PWD/src:$PWD \
  .venv/bin/pytest tests/ask/test_repository.py tests/ask/test_models.py \
  tests/ask/test_intent.py tests/ask/test_leakage.py tests/contracts -q
```

Result: `171 passed` (`38` repository, `16` models, `60` intent, `16` leakage, `41`
contracts), including the final Sol corruption, rollback, link-cardinality, actor,
ID-boundary, privacy/status, canonicalization, message-history, and page-context tests.

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
focused tests: actor-filtered thread listing/read/write/delete, all-child ownership
validation, composite-FK rejection, bounded opaque ID/provider-column checks, and
persisted-ID corruption are covered alongside:
different quiz-question rejection,
ordered append-only messages, complete provenance reconstruction with hashed provider
references, absent raw evidence
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
`retrieval_runs.expected_evidence_count` for exact contiguous-link validation. It binds
child `thread_id` and `actor_id` through composite parent uniqueness/FKs and requires
all-child reads to fail closed on actor mismatch. It includes migration/idempotence,
central-table, app-state, atomic-concurrency, strict-retention rollback on malformed
stored timestamps, terminal-link loss, persisted-timestamp/ID corruption, message-history
counter/count/sequence validation, canonical page-context serialization, provider and
actor-column boundary, opaque provider-ID, validation-status, privacy, provenance-scope,
and no-route/no-v2 integration tests. `validation_outcome` remains non-null `TEXT` but is
limited to `{valid, invalid, rejected, insufficient, error}`; `provider_request_id` stores
only `sha256:<64 lowercase hex>` derived references from caller input, never raw provider
text. Scope persistence requires the exact five canonical keys and deterministic
serialization; page context remains `extra="forbid"`. None of those edits were applied
here. The service/context integration proposal must additionally test that,
when a thread's revision tuple is empty, each resolved `EvidenceRef` revision is checked
against the effective Source Trust course/exam/lecture scope before
`record_retrieval_run`; Task 3.6 cannot claim this blocked index membership check.

The local mappings are therefore a deliberate testable boundary adapter, not a second
production schema. The `KeyError` equivalence for missing and unauthorized IDs is a
deliberate ownership-oracle defense. Ask v2 remains proposal-only; Tasks 3.2, 3.4, 3.5,
3.7, and 3.8, integration, merge, push, deploy, and production/Anki work were not
started.
