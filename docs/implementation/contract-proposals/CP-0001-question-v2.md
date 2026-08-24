# CP-0001: candidate question-v2 contract

## Requesting task

- Requesting task: Task 5.3 governance correction, fresh Luna implementation.
- Authorized base: `fa0ca5c0d9fce5366d1e724e5c9017069ea0dccc` (tree
  `554bcfb450c8666116ea9b340c81f559987b9e68`).
- Contract owner: Sol-0, under the post-Gate-1 contract-change protocol.
- Producing workstream: Sol-5 board-question models.
- Gate-1 record: `artifacts/acceptance/grounded-learning/gate-1.json`, state
  `open`.
- Gate-1 contract tag target: `studyhub-grounded-contracts-v1`, annotated tag
  object `a2636bbeb84d2143685c3555e9a3f74ccb8380d0`, peeled target
  `60a5f3ec873f982bca14d3507d719eb9927a8f1a`.

Question-v2 activation is proposed only. This correction does not activate a
new shared contract, change routes or flags, or make any provider call.

Activation status: `BLOCKED_ON_CONSUMING_OWNER_APPROVAL`. The frozen-v1
restoration and isolated governance correction are complete, but the candidate
is not activation-ready because both reviewed consuming owners returned
`CHANGES REQUIRED` and no consuming-owner approval exists.

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

The candidate keeps the already reviewed model union and its validation rules;
the versioned `$id` is the boundary identity. It is generated in memory in
`tests/questions/test_models.py` and is not written to `schemas/question-v2.json`
by this correction.

The candidate is exactly:

- UTF-8 byte count: `7,379`.
- SHA-256: `0f535c43fc1de3eadc61970f615370d3b23bc1046c7bef5f7bdeb01419a8294d`.
- Determinism evidence: two independent `_candidate_v2_schema_bytes()` calls
  each run `json_schema()`, apply `$schema`/`$id`, and sort-serialize a fresh
  candidate; their bytes are equal, and the focused test asserts the byte count
  and SHA-256.
- Model source: `src/oms_hub/questions/models.py`, using the existing
  `BoardQuestionDraft`, `QuestionValidationResult`, and `QuestionVersion`.
- Candidate test: `tests/questions/test_models.py::test_question_schema_candidate_v2_is_deterministic_and_v1_snapshot_is_frozen`.

No candidate bytes are treated as an active wire contract until this proposal
has the required owner approvals and the shared exporter/test/snapshot changes
land together.

## Why a local adapter is insufficient

A local adapter or test-only schema wrapper can prove that the isolated Pydantic
models serialize deterministically, but cannot establish the shared contract's
identity for producers and consumers. It would leave the central exporter,
schema snapshot set, and consumer-facing version registry on the reserved v1
namespace while a separate local copy claims v2. That is duplicate authority
and would permit producer/consumer drift. The post-Gate-1 protocol therefore
requires a versioned proposal, Sol-0 approval, and one affected consuming-Sol
approval before a canonical shared artifact changes.

## Producer and consumer impact

### Current correction

- Producer: existing Sol-5 question models remain isolated and unchanged.
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
   - Materialize the exact 7,379-byte candidate only after approval and the
     exporter/test changes are ready.
   - Verify SHA-256 `0f535c43fc1de3eadc61970f615370d3b23bc1046c7bef5f7bdeb01419a8294d`.

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

These prerequisites require authorized model/resolver/product work outside this
governance-only correction. Until they are implemented and reviewed, the
candidate remains isolated and unapplied.

## Exact verification

Correction evidence required and recorded in the handoff/report:

```text
PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src:$PWD \
  python -m pytest tests/questions/test_models.py --override-ini addopts= -q
34 passed

PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src:$PWD \
  python -m pytest tests/contracts/test_schema_exports.py --override-ini addopts= -q
2 passed

PATH=/tmp/studyhub-task01-venv/bin:$PATH PYTHONPATH=$PWD/src:$PWD \
  python -m pytest tests/contracts tests/providers tests/features --override-ini addopts= -q
96 passed

PATH=/tmp/studyhub-task01-venv/bin:$PATH ruff check src tests scripts
All checks passed!

PATH=/tmp/studyhub-task01-venv/bin:$PATH mypy src
Success: no issues found in 180 source files

PATH=/tmp/studyhub-task01-venv/bin:$PATH MYPYPATH=$PWD/src \
  mypy tests/questions/test_models.py
Success: no issues found in 1 source file

git diff --exit-code 82e22d7:schemas/question-v1.json schemas/question-v1.json
exit 0; no output
sha256sum schemas/question-v1.json
968449d9dca8da71a28658360fe6a2d8e61cf35e49c5d8a9ab6e7a4564e7eb9d
```

Future activation must additionally rerun the exporter snapshot suite, all
affected contract/provider/fixture/features tests, Ruff, source and task-test
typing, `git diff --check`, and the repository's owned-scope and safety scans.

## Review and approval fields

The completed verdicts and evidence are recorded below. Remaining `PENDING`
fields are intentionally limited to Sol-0 contract-owner review; no approval is
inferred from the focused GREEN result.

| Required review | Reviewer | Result | Evidence commit/tree | Status |
| --- | --- | --- | --- | --- |
| Terra specification review | `/root/task_5_3_governance_terra_spec` | APPROVED | `9605b08fe60455a32a32d5b66e2e737d07af3adc` / `38c1aec10f459514d4d52f55566f7bb79aab6c49` | APPROVED |
| Terra quality review | `/root/task_5_3_governance_terra_quality` | APPROVED after scoped re-review; initial CHANGES REQUIRED for non-independent generation | `02797a90c2665cf85480abe667ccfa9c3a57e166` / `2eaf22a3b835aaead6f1082a256620c85985fa25`; focused direct test 1 passed | APPROVED |
| Workstream Sol review | `/root` | `OWNED_CORRECTION_PASS / BLOCKED_ON_CONSUMING_OWNER_APPROVAL` | `302fae99b304ee0249a853eba2e0349d16d9acd8` / `441040a16d9b14602aa4dc5db7c18bf16c74bfa8` | PASS; disposition blocked |
| Sol-0 contract-owner review | PENDING | PENDING | PENDING | PENDING |

### Consuming-owner approval (required)

- Consuming-owner approval: **NONE**. The required approval outcome is blocked.
- Sol-6 reviewer: CHANGES REQUIRED on `02797a90c2665cf85480abe667ccfa9c3a57e166`
  / tree `2eaf22a3b835aaead6f1082a256620c85985fa25`; Sol-5 and Sol-6 worktrees
  were clean and unchanged during review.
- Sol-6 blockers: canonical immutable `question_version_id`; enforce
  `schema_version=question-v2`; authoritative approved objective and
  `source_snapshot_hash` resolver; fail-closed tests for absent, stale,
  unapproved, or unverifiable resolution; no v1 fallback.
- Sol-10 reviewer: CHANGES REQUIRED on `02797a90c2665cf85480abe667ccfa9c3a57e166`
  / tree `2eaf22a3b835aaead6f1082a256620c85985fa25`; Sol-10 inspected
  practice/session/blueprint interfaces, and its pre-existing dirty Error
  Notebook files/tests were unchanged by review.
- Sol-10 blockers: canonical `question_version_id`/resolver; enforce v2 and
  reject v1/unknown; approved and nonstale inventory authority; consumer tests
  for strict shortfall, timed behavior, immutable historical IDs; rollback must
  preserve persisted historical session/mapping readability. Sol-6's mastery
  resolver remains a separate condition.
- Scope confirmed: no approval exists for candidate activation, Task 5.2/5.4,
  runtime routes, provider calls, or production changes.
- Final Workstream Sol review: `/root` returned
  `OWNED_CORRECTION_PASS / BLOCKED_ON_CONSUMING_OWNER_APPROVAL` on candidate
  `302fae99b304ee0249a853eba2e0349d16d9acd8` / tree
  `441040a16d9b14602aa4dc5db7c18bf16c74bfa8`. The owned governance correction
  passed: frozen v1, independent v2 candidate, untouched exporter/central/
  model/shared/product surfaces, proposal/handoff scope, Terra evidence, and
  clean worktree were all confirmed. The final disposition remains blocked by
  the consuming-owner conflicts above.

## Conflict analysis

- The original Task 5.3 implementation directly replaced the Gate-1
  `question-v1.json` snapshot with generated model output. That conflicts with
  the frozen Gate-1 bytes and the post-Gate-1 §3.4/§12.2 proposal requirement.
- The original direct question-v1 edit is explicitly **superseded and not
  accepted shared state**. It is not a migration source and must not be
  reintroduced through the exporter.
- The isolated models and their direct validation tests remain complete. The
  direct test now proves a candidate v2 in memory while independently asserting
  the frozen v1 hash; it does not treat the tracked v1 snapshot as active.
- The Sol-0-owned exporter and central schema tests are intentionally untouched.
  Restoring v1 removes their prior mismatch; v2 activation waits for this
  proposal's approvals and a later integration commit.
- No conflict exists with Task 5.1's recipe adapter, but no Task 5.1 files are
  changed. Task 5.2 and Task 5.4 remain outside scope and blocked by Gate 2A.
- Consuming-owner conflict: both Sol-6 and Sol-10 require canonical immutable
  question identity, strict v2 enforcement, approved/nonstale authority
  resolution, fail-closed consumer tests, and no v1 fallback. Sol-10 additionally
  requires historical-session readability across rollback and strict practice
  shortfall/timed tests. These are real contract conflicts, not documentation
  preferences, and cannot be fixed inside this lane without unauthorized
  model/resolver/product changes.
- Therefore the candidate is not activation-ready and the required consuming
  owner approval is absent. Sol-0 contract-owner review remains `PENDING`.
- Any later final-review evidence commit containing only governance bookkeeping
  is external evidence and does not change the reviewed candidate identity
  `302fae99b304ee0249a853eba2e0349d16d9acd8` / tree
  `441040a16d9b14602aa4dc5db7c18bf16c74bfa8`.

## Rollback

- Before activation: reject or defer this proposal and keep the Gate-1 v1
  snapshot plus the isolated models; delete only any disposable v2 candidate
  output if a later lane created one.
- After activation: revert the single reviewed exporter/test/v2-snapshot
  activation change as a unit, retaining the v1 snapshot unchanged. Do not
  rewrite v1 to the generated candidate.
- No database, provider, route, feature-flag, production, Anki, or external
  state rollback is required because none is changed by this proposal.
