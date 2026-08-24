# CP-0001: candidate question-v2 contract

## Requesting task

- Requesting task: Task 5.3 bounded question-v2 boundary correction, fresh Luna
  implementation.
- Authorized base: `4f5c687edffbabb08321515c3e505e41d3ee3888` (tree
  `cd12cb0896a66986dd4d7bcd9bb7902836128079`).
- Contract owner: Sol-0, under the post-Gate-1 contract-change protocol.
- Producing workstream: Sol-5 board-question models.
- Gate-1 record: `artifacts/acceptance/grounded-learning/gate-1.json`, state
  `open`.
- Gate-1 contract tag target: `studyhub-grounded-contracts-v1`, annotated tag
  object `a2636bbeb84d2143685c3555e9a3f74ccb8380d0`, peeled target
  `60a5f3ec873f982bca14d3507d719eb9927a8f1a`.

Question-v2 activation is proposed only. This correction does not activate a
new shared contract, change routes or flags, or make any provider call.

Activation status: `PENDING_RENEWED_REVIEWS`. The owned boundary correction is
implemented and the candidate remains unapplied; renewed Terra, Sol-6, Sol-10,
and Sol-0 review fields are reset to `PENDING`.

## Current frozen contract

The current tracked `schemas/question-v1.json` is the Gate-1 reserved snapshot.
It remains fail-closed and is not the active question model contract.

- Source of truth: `82e22d7:schemas/question-v1.json`.
- Exact UTF-8 byte count: `293`.
- Exact SHA-256: `968449d9dca8da71a28658360fe6a2d8e61cf35e49c5d8a9ab6e7a4564e7eb9d`.
- Exact bytes:

```json
{
  "$id": "question-v1.json",
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "description": "Reserved contract namespace; no wire instances are valid until the owning domain contract is implemented.",
  "not": {},
  "title": "Study Hub question contract v1 \u2014 reserved"
}
```

The correction verifies byte identity with
`git diff --exit-code 82e22d7:schemas/question-v1.json schemas/question-v1.json`.

## Proposed versioned change

Propose a future active `question-v2.json` snapshot generated from the existing
isolated question models:

```text
TypeAdapter(BoardQuestionDraft | QuestionValidationResult | QuestionVersion).json_schema()
schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
schema["$id"] = "question-v2.json"
json.dumps(schema, indent=2, sort_keys=True) + "\n"
```

The candidate keeps the reviewed model union, adds the required immutable
`question_version_id`, and enforces `schema_version` as the literal
`question-v2`; the versioned `$id` is the boundary identity. It is generated
in memory in `tests/questions/test_models.py` and is not written to
`schemas/question-v2.json` by this correction.

The candidate is exactly:

- UTF-8 byte count: `7,557`.
- SHA-256: `a3de074e74d19066078cb4eb857a5b29a5d78db19edced24310ad6637b1778d6`.
- Determinism evidence: two independent `_candidate_v2_schema_bytes()` calls
  each run `json_schema()`, apply `$schema`/`$id`, and sort-serialize a fresh
  candidate; their bytes are equal, and the focused test asserts the byte count
  and SHA-256.
- Model source: `src/oms_hub/questions/models.py`, using the existing
  `BoardQuestionDraft`, `QuestionValidationResult`, and `QuestionVersion`.
- Authority boundary source: `src/oms_hub/questions/resolution.py`, exposing
  one frozen `QuestionResolution`, `QuestionResolutionProvider` protocol, and
  fail-closed `resolve_question_version` adapter.
- Candidate test: `tests/questions/test_models.py::test_question_schema_candidate_v2_is_deterministic_and_v1_snapshot_is_frozen`.

No candidate bytes are treated as an active wire contract until this proposal
has the required owner approvals and the shared exporter/test/snapshot changes
land together.

## Why a local adapter is not activation

The owned `resolve_question_version` adapter proves the narrow local boundary:
one canonical ID must receive an exact, approved, nonstale, verifiable
resolution with approved objectives and a source snapshot hash. It intentionally
does not establish the shared contract's producer/consumer registration. The
central exporter, schema snapshot set, and consumer-facing version registry
remain on the reserved v1 namespace while this candidate is local. The
post-Gate-1 protocol therefore still requires a versioned proposal, Sol-0
approval, and one affected consuming-Sol approval before a canonical shared
artifact changes.

## Producer and consumer impact

### Current correction

- Producer: Sol-5 question models now require an immutable canonical
  `question_version_id` and literal `schema_version=question-v2`.
- Authority boundary: the abstract `QuestionResolutionProvider` and adapter
  validate exact ID, approved objectives, source hash, approved/nonstale/
  verifiable state; no runtime provider or repository is introduced.
- Consumers: no runtime consumer, route, feature flag, provider, persistence,
  or integration path consumes question-v2.
- Schema exporter: unchanged and continues to emit the reserved question-v1
  namespace; its central snapshot test is green because the frozen v1 bytes are
  restored.
- Task 5.4 and Task 5.2: remain blocked on Source Trust Gate 2A / Task 1.8 and
  are not changed by this proposal.

### Future activation

- Producer: the central exporter would add the approved question-v2 model union
  as an active schema with `$id` `question-v2.json`; future question producers
  would record `schema_version=question-v2` when emitting that version.
- Consumers: central schema checks and any approved downstream question
  producer/consumer must validate the v2 artifact by its `$id` and exact bytes.
  A consuming Sol must confirm it can handle the version before activation;
  neither reviewed consuming Sol has approved the candidate.
- No existing consumer is silently re-pointed from v1 to v2. The inactive
  reserved v1 snapshot remains available as historical Gate-1 evidence.

## Compatibility and migration

- This is an additive, explicitly versioned contract change; it does not
  repurpose `question-v1.json`.
- The frozen v1 snapshot remains byte-identical and fail-closed.
- There are no accepted persisted question-v1 wire instances to migrate because
  v1 was reserved and inactive. Any disposable local candidate output must be
  regenerated under the approved v2 identity rather than migrated in place.
- Activation must land the versioned exporter registration, central test
  expectations, and `schemas/question-v2.json` snapshot as one reviewed
  contract change. Existing knowledge-v1, ask-v1, mastery-v1, practice-v1,
  and journal-v1 behavior remains unchanged.
- No database migration, route change, feature-flag change, provider change,
  prompt rollout, or runtime behavior change is authorized by this proposal.

## Exact future targets

These are targets for a later approved Sol-0 integration change, not files
modified by this correction:

1. `scripts/export_grounded_contract_schemas.py`
   - Import the three existing question models.
   - Add their `TypeAdapter` union to `_active_schemas()`.
   - Set the generated `$schema` and `$id` exactly as in the candidate
     generation reference above, with `$id` `question-v2.json`.
   - Keep `question-v1.json` in `_RESERVED` with its Gate-1 reserved payload;
     do not rewrite the frozen v1 snapshot.
   - Preserve all other active and reserved schema outputs byte-for-byte.
2. `tests/contracts/test_schema_exports.py`
   - Add `question-v2.json` to the active snapshot names.
   - Keep `question-v1.json` in the reserved-name assertions.
   - Assert the exact candidate bytes/hash, repeated-export byte equality, and
     fail-closed v1 payload.
3. `schemas/question-v2.json`
   - Materialize the exact 7,557-byte candidate only after approval and the
     exporter/test changes are ready.
   - Verify SHA-256
     `a3de074e74d19066078cb4eb857a5b29a5d78db19edced24310ad6637b1778d6`.

No exporter, central test, or future snapshot target is changed in this lane.

## Activation prerequisites from consuming reviews

No question-v2 activation, exporter registration, or snapshot materialization
may proceed until all of the following are satisfied by authorized owners:

- Introduce and use one canonical immutable `question_version_id` and resolver
  across question, mastery, practice, session, blueprint, and historical
  mappings; no consumer may reconstruct identity from mutable fields.
- Enforce `schema_version=question-v2` at every v2 consumer boundary and reject
  v1 or unknown versions; there is no v1 fallback.
- Resolve objectives and source authority through an authoritative resolver that
  proves the objective is approved and the `source_snapshot_hash` is approved,
  current, and verifiable.
- Fail closed for absent, stale, unapproved, or unverifiable objective/source
  resolution, with consumer tests proving those failures.
- Sol-10 practice/session/blueprint consumers must add tests for strict
  shortfall behavior, timed behavior, immutable historical question IDs, and
  rollback that preserves persisted historical session/mapping readability.
- The approved, nonstale inventory authority must be explicit for Sol-10
  consumers; the Sol-6 mastery resolver remains a separate required condition.

The model and abstract resolver boundary are implemented by this correction;
consumer adoption, inventory authority, historical-session behavior, and the
remaining product tests require authorized Sol-6/Sol-10 work outside this lane.
Until those consumers and the shared exporter activation are reviewed, the
candidate remains isolated and unapplied.

## Exact verification

Correction evidence required and recorded in the handoff/report:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src:$PWD \
  python -m pytest tests/questions/test_models.py tests/questions/test_resolution.py \
  --override-ini addopts= -q
51 passed

PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src:$PWD \
  python -m pytest tests/contracts/test_schema_exports.py --override-ini addopts= -q
2 passed

PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src:$PWD \
  python -m pytest tests/contracts tests/providers tests/features --override-ini addopts= -q
96 passed

PATH=/tmp/studyhub-task01-venv/bin:$PATH ruff check src tests scripts
All checks passed!

PATH=/tmp/studyhub-task01-venv/bin:$PATH mypy src
Success: no issues found in 181 source files

PATH=/tmp/studyhub-task01-venv/bin:$PATH MYPYPATH=$PWD/src \
  mypy tests/questions/test_models.py tests/questions/test_resolution.py
Success: no issues found in 2 source files

git diff --exit-code 82e22d7:schemas/question-v1.json schemas/question-v1.json
exit 0; no output
sha256sum schemas/question-v1.json
968449d9dca8da71a28658360fe6a2d8e61cf35e49c5d8a9ab6e7a4564e7eb9d
```

Future activation must additionally rerun the exporter snapshot suite, all
affected contract/provider/fixture/features tests, Ruff, source and task-test
typing, `git diff --check`, and the repository's owned-scope and safety scans.

## Review and approval fields

This correction changes the reviewed candidate. Renewed review fields are reset
to `PENDING`; historical findings below remain context and are not approvals for
this candidate. No approval is inferred from the focused GREEN result.

| Required review | Reviewer | Result | Evidence commit/tree | Status |
| --- | --- | --- | --- | --- |
| Terra specification review | PENDING | PENDING | PENDING | PENDING |
| Terra quality review | PENDING | PENDING | PENDING | PENDING |
| Workstream Sol review | PENDING | PENDING | PENDING | PENDING |
| Sol-6 consuming-owner review | PENDING | PENDING | PENDING | PENDING |
| Sol-10 consuming-owner review | PENDING | PENDING | PENDING | PENDING |
| Sol-0 contract-owner review | PENDING | PENDING | PENDING | PENDING |

### Historical findings retained as resolved-target context

- Prior Terra specification review: APPROVED on
  `9605b08fe60455a32a32d5b66e2e737d07af3adc` / tree
  `38c1aec10f459514d4d52f55566f7bb79aab6c49`.
- Prior Terra quality review: APPROVED after the independent-generation fix on
  `02797a90c2665cf85480abe667ccfa9c3a57e166` / tree
  `2eaf22a3b835aaead6f1082a256620c85985fa25`.
- Prior Sol-6 and Sol-10 findings were CHANGES REQUIRED on that prior candidate:
  canonical immutable identity, strict v2/no-v1 fallback, authoritative
  approved/nonstale/verifiable objective/source resolution, and consumer tests.
  This correction addresses the owned model and abstract resolver target; the
  renewed consuming reviews must confirm their own adoption and remaining
  product boundaries.
- Prior Workstream Sol disposition was
  `OWNED_CORRECTION_PASS / BLOCKED_ON_CONSUMING_OWNER_APPROVAL` on
  `302fae99b304ee0249a853eba2e0349d16d9acd8` / tree
  `441040a16d9b14602aa4dc5db7c18bf16c74bfa8`; it is historical only.
- Consuming-owner approval for this candidate is **NONE** until renewed Sol-6,
  Sol-10, and Sol-0 reviews complete. Candidate activation, Task 5.2/5.4,
  routes, provider calls, production, and Anki remain unauthorized.

## Conflict analysis

- The original Task 5.3 implementation directly replaced the Gate-1
  `question-v1.json` snapshot with generated model output. That conflicts with
  the frozen Gate-1 bytes and the post-Gate-1 §3.4/§12.2 proposal requirement.
- The original direct question-v1 edit is explicitly **superseded and not
  accepted shared state**. It is not a migration source and must not be
  reintroduced through the exporter.
- The isolated models and their direct validation tests remain complete. The
  direct test now proves a candidate v2 in memory, including the canonical
  immutable ID and literal version, while independently asserting the frozen
  v1 hash; it does not treat the tracked v1 snapshot as active.
- The owned resolver is deliberately abstract: it validates one provider
  result and has no source-trust repository, persistence, route, mastery,
  practice, provider, retry, or consumer implementation.
- The Sol-0-owned exporter and central schema tests are intentionally untouched.
  Restoring v1 removes their prior mismatch; v2 activation waits for this
  proposal's approvals and a later integration commit.
- No conflict exists with Task 5.1's recipe adapter, but no Task 5.1 files are
  changed. Task 5.2 and Task 5.4 remain outside scope and blocked by Gate 2A.
- Renewed consuming-owner review remains required: Sol-6 and Sol-10 must confirm
  canonical identity/resolver use at their own boundaries, approved/nonstale
  inventory behavior, fail-closed consumer tests, and historical-session
  rollback guarantees. Sol-10 additionally owns strict practice shortfall and
  timed behavior. Those consumer/product behaviors remain outside this lane.
- Therefore the candidate is not activation-ready and all renewed approvals are
  `PENDING`; Sol-0 contract-owner review remains `PENDING`.
- The pre-fix expanded candidate was
  `2826d49000c79d22fab5823f8c7ff5ee7699f32c` / tree
  `0c8b1b85fef38ec76c0918e17b7b7160cbb70450`. It is correction history, not
  the renewed review identity. Renewed reviews evaluate the post-fix head
  recorded in the final handoff/report after this correction commit.

## Rollback

- Before activation: reject or defer this proposal and keep the Gate-1 v1
  snapshot plus the isolated models; delete only any disposable v2 candidate
  output if a later lane created one.
- After activation: revert the single reviewed exporter/test/v2-snapshot
  activation change as a unit, retaining the v1 snapshot unchanged. Do not
  rewrite v1 to the generated candidate.
- No database, provider, route, feature-flag, production, Anki, or external
  state rollback is required because none is changed by this proposal.
