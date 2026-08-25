# CP-0002: Legacy slide revision adapter contract

Status: proposed and unapplied. This document is not approval, activation, or
authorization to implement Task 1.6.

## Request

- Requesting task: Task 1.6, Sol-1.
- Required decider: Program Sol-0.
- Required consuming review: Sol-2 under the contract-change protocol because
  this adapter produces the scope and source inputs used by provider indexing.
- Consuming review status: **BLOCK**. Sol-2 requires StoreKey-safe scope IDs, a
  knowledge-owned indexing input view, and durable supersession before approval.
- Blocked tasks: 1.6, then 1.7 and Source Trust Gate 2A (1.8).

Task 1.6 must adapt existing immutable slide revisions without changing or
copying their canonical files. The current types expose enough data for a
minimal adapter, but they do not name one canonical mapping from legacy
revision identity, hashes, and catalog scope into Source Trust. Sol-0 must
freeze that mapping before Sol-1 creates durable IDs.

## Current contracts and evidence

The existing `StudyRevision` read model exposes numeric `id`, per-upload
`upload_item_id`, numeric `lecture_id`, `source_sha256`, `derived_sha256`,
immutable/canonical paths, `state`, and `current`. It exposes no separate
logical-source ID or knowledge-to-legacy crosswalk.

Existing read APIs are sufficient when composed:

- `IngestionRepository.get_study_revision(int)` reads one revision.
- `CatalogRepository.list_lectures()` enumerates catalog lectures.
- `IngestionRepository.list_current_revisions(lecture_id)` reads the current
  revisions for one lecture.
- `CatalogRepository.get_lecture(lecture_id)` supplies `subject`,
  `exam_number`, and `lecture_number`.

Existing artifact callers use two different identifiers. Anki source passages
call `StudyRevision.upload_item_id` the `artifact_id`, while the authenticated
artifact service and `/artifacts/{revision_id}/{role}` route resolve the
numeric `StudyRevision.id`. Source Trust currently stores only
`source_document_id`, `source_revision_id`, one file hash, and state, so Task
1.7 needs a deterministic reversible adapter identity and must hide that
legacy crosswalk behind a knowledge-owned read boundary.

The slide pipeline's `last_document` is process-local, and its persisted shadow
report deliberately omits source text. `LegacyPptxProcessor` version 1 is the
existing local deterministic PowerPoint parser that returns slide, speaker-note,
table, and image provenance without network access or source-file copying.

## Proposed ruling

Sol-0 should approve the following exact adapter contract for legacy current
slide revisions.

### 1. Eligible legacy revision

A revision is eligible only when all of these are true:

```text
kind == UploadKind.SLIDES
state == "current"
current is True
id is a positive integer
immutable_source_path is an existing file
sha256_file(immutable_source_path) == source_sha256
derived_sha256 is present
canonical_derived_path is present
```

Missing catalog metadata, source bytes, a source checksum match, or PDF
metadata fails that revision without any Source Trust write. The adapter never
repairs, copies, promotes, retires, or otherwise mutates the legacy revision or
its files.

### 2. Legacy-to-knowledge identity

Map one legacy immutable study revision to one compatibility knowledge source:

```text
source_document_id = "legacy-study-revision:" + str(StudyRevision.id)
file_sha256         = StudyRevision.source_sha256
source_revision_id  = source_revision_id(source_document_id, file_sha256)
authority_class     = course_material
state               = ready after the exact evidence set is persisted
```

The prefix makes the identity reversible and namespace-safe. Only Task 1.7's
knowledge service may decode the exact
`legacy-study-revision:<positive base-10 integer>` form and use that integer
with the existing authenticated `ArtifactService`. Sol-2 and every later
consumer treat `source_document_id` and artifact identities as opaque and must
not decode legacy IDs, treat `upload_item_id` as a preview identifier, or query
the ingestion/catalog tables.

This is intentionally a one-to-one compatibility mapping. A replacement deck
receives a new compatibility `source_document_id` instead of becoming another
knowledge revision under the same logical source. Sol-0 must explicitly accept
this bounded deviation from the ideal logical-source grouping, or reject this
proposal and own an additive durable crosswalk before Task 1.6 starts.

### 3. Hash meaning

Use `source_sha256`, not `derived_sha256`, as `SourceRevision.file_sha256`.
Normalization reads the PowerPoint and its speaker notes; notes can change
without changing rendered PDF content. The PDF remains the visual preview and
continues to be validated by the legacy artifact service against
`derived_sha256`. Do not combine or re-hash the two checksums because the
frozen Source Trust ID helper accepts one existing file checksum.

### 4. Bounded StoreKey-safe scope identifiers

Define the scope IDs with Python standard-library normalization, slugging, and
a 96-bit SHA-256 suffix:

```python
canonical_subject = " ".join(
    unicodedata.normalize("NFKC", LectureModel.subject).casefold().split()
)
slug = re.sub(r"[^a-z0-9]+", "-", canonical_subject).strip("-")
bounded_slug = (slug[:74].rstrip("-") or "course")

course_digest = sha256(canonical_subject.encode("utf-8")).hexdigest()[:24]
exam_seed = f"{canonical_subject}\0exam\0{LectureModel.exam_number}"
lecture_seed = (
    f"{canonical_subject}\0exam\0{LectureModel.exam_number}"
    f"\0lecture\0{LectureModel.lecture_number}"
)

course_id = f"{bounded_slug}-{course_digest}"
exam_id = (
    f"exam-{LectureModel.exam_number}-"
    f"{sha256(exam_seed.encode('utf-8')).hexdigest()[:24]}"
)
lecture_id = (
    f"lecture-{LectureModel.lecture_number}-"
    f"{sha256(lecture_seed.encode('utf-8')).hexdigest()[:24]}"
)
```

Reject a blank `canonical_subject`; require exam and lecture numbers in the
positive signed-64-bit range. `course_id` is at most 99 characters, while the
numeric IDs are at most 52 characters. All three match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`, contain no StoreKey namespace delimiter,
and include a digest of their complete natural-key input so truncated or
punctuation-colliding slugs remain distinct. NFKC/case/whitespace-equivalent
subjects intentionally share one course identity.

The required consumer contract is:

```python
key = StoreKey.course(course_id, exam_id)
assert StoreKey.parse(key.value) == key
assert key.course_id == course_id
assert key.exam_id == exam_id
```

Because `StoreKey` is currently owned on the unmerged Sol-2 branch, Sol-1 must
not copy or import it. Sol-1 tests the frozen bounds/grammar/formulas locally;
Sol-2 or Sol-0 runs the exact round-trip test above when the accepted branches
are composed for Gate 2A.

### 5. Read and enumeration paths

Single-revision backfill parses the public string input as a canonical positive
base-10 integer, calls `get_study_revision`, then `get_lecture`, and applies the
eligibility rules above.

Batch backfill calls `list_lectures`, composes `list_current_revisions` for each
lecture, filters to eligible slide candidates, globally sorts by numeric
`StudyRevision.id`, and applies `limit` after sorting. No new list API or query
is added to the ingestion repository.

### 6. Evidence extraction and locators

Validate the immutable PowerPoint checksum, construct `SourceSnapshot` with
the compatibility source ID and source checksum, and parse it with
`LegacyPptxProcessor` version 1. Feed the returned `ParsedDocument` into Task
1.4's `normalize_course_revision` with the identifiers above.

The resulting slide, speaker-note, table, and figure locators are authoritative
for this adapter. Do not consume `SlidePipeline.last_document` or its redacted
shadow report, and do not invoke OCR or a live/model-backed vision fallback.
An image-only slide may therefore produce no trusted text evidence.

For Task 1.7 visual preview, a PowerPoint slide locator `N` maps to PDF page
`N`. Gate 2A must prove this one-slide-per-page assumption against the existing
conversion fixture before accepting citation previews.

### 7. Knowledge-owned indexing input view

Task 1.7 must expose this internal, read-only service boundary:

```python
KnowledgeService.resolve_index_input(
    source_revision_id: str,
) -> IndexInputView
```

`IndexInputView` is a frozen Python view owned in
`src/oms_hub/knowledge/service.py`, not a web/wire schema. It contains exactly:

```text
source_document_id: str                 # opaque to consumers
source_revision_id: str                 # preserved sr_ identity
revision_state: SourceRevisionState
authority_class: AuthorityClass         # course_material for this adapter
course_id: str
exam_id: str
lecture_id: str
pptx: CanonicalInputArtifact
pdf: CanonicalInputArtifact
evidence_units: tuple[EvidenceUnit, ...]
assets: tuple[IndexAssetView, ...]
```

Each frozen `CanonicalInputArtifact` contains opaque `artifact_id`, `role`,
verified local `path`, `sha256`, and `media_type`. The identities are
`{source_revision_id}:pptx` and `{source_revision_id}:pdf`; the PPTX checksum is
the preserved `source_sha256`, and the PDF checksum is the legacy
`derived_sha256`. Each frozen `IndexAssetView` contains the namespaced asset
ID, verified path when one exists, media type, SHA-256, and canonical locator.
Assets are sorted by namespaced asset ID.

The service resolves the legacy crosswalk internally, obtains both artifacts
through the existing authenticated/private artifact boundary, reparses the
PPTX with the frozen parser, and requires the recomputed evidence units to
equal the stored normalized evidence exactly. A missing file, checksum
mismatch, evidence mismatch, authority/scope mismatch, ambiguous asset, or
unsupported revision state fails closed without mutation.

The view may report `ready`, `stale`, or `retired` so Sol-2 can reconcile an
existing provider document, but only `ready` is eligible for a new upload or
import. Sol-2 consumes this view only; it never parses `source_document_id`,
accesses a numeric legacy revision ID, calls `IngestionRepository` or
`CatalogRepository`, or reconstructs scope IDs from catalog data.

### 8. Durable supersession, retry, completion, and reporting

Parse and validate the complete candidate before the first Source Trust write.
A revision counts as `already_present` only when the deterministic revision and
its exact deterministic evidence set already exist. A revision record with
missing evidence is resumed/repaired on retry and is not counted as complete.

Replacement activation is one Source Trust repository transaction keyed by
`(authority_class, course_id, exam_id, lecture_id)`:

1. Create or re-read the deterministic source and replacement revision in
   `normalizing`; it is not indexable.
2. Insert or verify the exact deterministic evidence set.
3. Change every other `ready` course-material revision in that exact scope to
   `stale`.
4. Change the replacement from `normalizing` to `ready` as the transaction's
   final state change, then commit.

Any failure rolls back the entire activation, leaving the predecessor `ready`
and the replacement non-indexable. After commit, the predecessor is durably
`stale` before any consumer can resolve the replacement as `ready`. Exact
retries are no-ops. Task 1.6 does not retire the predecessor; Task 1.7's
`mark_dependents_stale` flow may retire it only after provider/artifact
dependencies are preserved and marked stale. Stale/retired evidence remains
available for audit and cannot enter a new provider upload.

Sol-1 may add a narrow transaction method and scoped query to
`KnowledgeRepository` to enforce this sequence using the existing
`source_revisions.state` and evidence scope columns. No new table, column, or
central migration is proposed. Concurrent multi-process activation remains
fail-closed and must not create two `ready` revisions for one scope; if the
existing transaction boundary cannot prove that invariant on supported
databases, implementation stops and requests a Sol-0-owned uniqueness schema
change instead of weakening supersession.

Batch order and report fields are fixed:

```text
examined
created
already_present
failed
failure_ids
```

`failure_ids` contains canonical decimal legacy revision IDs in processing
order. One failure does not roll back or duplicate earlier completed revisions.
Dry-run performs all reads, parsing, checksum validation, and deterministic ID
calculation but makes no Source Trust or legacy write and prints no source text.

### 9. Ownership and activation

If approved, Task 1.6 may touch only:

```text
src/oms_hub/knowledge/backfill.py
src/oms_hub/knowledge/repository.py
tests/knowledge/test_backfill.py
tests/knowledge/test_repository.py
docs/implementation/handoffs/1.6.md
```

The repository changes are limited to the atomic supersession operation and
its scoped read; no schema DDL changes. Task 1.7 owns
`src/oms_hub/knowledge/service.py` and its focused tests for
`resolve_index_input`. Both tasks consume the existing ingestion, catalog,
parser, normalization, ID, and artifact contracts read-only. They do not edit
the ingestion repository, central schema, shared schemas/exporter, startup,
route wiring, settings, feature flags, dependencies, migrations, plan, or
manifest. Program Sol-0 continues to own activation and integration.

### 10. Shared-contract and schema evaluation

These amendments do **not** require a Sol-0-owned JSON schema, provider
contract, central database schema, migration, or wiring change if Sol-0 accepts
CP-0002 as the reviewed cross-workstream contract:

- the bounded IDs are ordinary values in existing scope fields;
- `IndexInputView` is an internal frozen knowledge-service view, not a public
  route or serialized wire contract;
- `ready`, `stale`, and `retired` already exist in `SourceRevisionState` and the
  persisted source-revision state column; and
- evidence authority/scope and retirement metadata already exist.

The interface is still a post-Gate-1 cross-workstream contract, so Sol-0 and
Sol-2 must approve its exact shape and acceptance tests before implementation.
Central activation of `KnowledgeRepository` remains separately HELD under the
Task 1.3 proposal.

A Sol-0-owned shared schema change becomes required only if implementation
cannot enforce one `ready` revision per scope transactionally with existing
columns, or if Sol-0 requires persisted artifact/asset crosswalk fields rather
than the knowledge-owned read view. In either case Task 1.6 remains blocked
until that additive schema, migration, compatibility, and rollback contract is
approved.

## Alternatives rejected

### Use `upload_item_id` as `source_document_id`

Rejected because it is per upload and opaque to the authenticated artifact
service, which resolves numeric study revision IDs. It would not remove Task
1.7's crosswalk ambiguity.

### Use one logical source ID such as `{lecture_id}:slides`

Semantically preferable, but rejected for the minimal adapter because the
current Source Trust revision has no durable legacy artifact ID. Once a legacy
revision is superseded, current-only reads cannot reliably recover its private
preview. Additive crosswalk persistence is the upgrade path if Sol-0 requires
logical grouping now.

### Use numeric `StudyRevision.id` without a namespace

Rejected because an unqualified string can collide with future source families
and does not identify how Task 1.7 should decode it.

### Let Sol-2 decode the compatibility source ID or query ingestion

Rejected because it couples provider indexing to Sol-1's legacy adapter and
central ingestion tables. The knowledge service owns resolution and gives
Sol-2 opaque, verified canonical inputs.

### Use `derived_sha256` or a combined PPTX/PDF digest

`derived_sha256` misses speaker-note-only changes. A combined digest invents a
new hash contract and cannot be validated as the checksum of one canonical
file. Existing artifact preview already validates the PDF separately.

### Use raw or simply normalized subject text for scope

Raw text violates StoreKey grammar for spaces, slashes, colons, and Unicode.
Slugging or truncating without a digest creates collisions. Numeric lecture IDs
are database-instance identifiers rather than the catalog's natural course,
exam, and lecture key. The bounded slug plus full-input digest preserves safe
display hints and collision resistance.

### Reuse `SlidePipeline.last_document` or the shadow report

`last_document` disappears with the process, while the persisted report omits
source text. Neither can backfill recovered historical revisions.

### Re-run the configured Anydoc/shadow mode

Rejected for the compatibility backfill because environment/configuration can
change parser selection and therefore durable evidence IDs. The existing local
legacy parser is the smallest deterministic baseline. A later parser upgrade
must create a new source/evidence revision rather than silently rewrite IDs.

### Add a new ingestion list API, crosswalk table, or shared field now

Rejected as unnecessary for the proposed one-to-one adapter and outside the
Task 1.6 file set. If Sol-0 rejects the compatibility identity, an additive
crosswalk is required before implementation and must be separately specified,
reviewed, and owned.

### Put `IndexInputView` in provider contracts or a public JSON schema

Rejected because it is an internal local-file service boundary containing
verified paths and normalized domain objects, not a browser/provider wire
payload. CP-0002 plus Sol-0/Sol-2 approval freezes the cross-workstream shape
without changing central schemas.

### Mark predecessors stale after provider upload starts

Rejected because both revisions could become indexable concurrently and stale
provider work could win a race. Supersession commits in Source Trust before
the replacement is exposed as `ready`.

## Compatibility risks

- One legacy revision per knowledge source does not group replacement decks
  under one logical source. Existing IDs remain deterministic, but a future
  grouping migration must preserve them as aliases/history.
- NFKC/case/whitespace-equivalent subjects intentionally share a course ID.
  Other subjects use a 96-bit digest of the complete normalized input; an
  observed digest collision must fail closed and require a versioned ID scheme.
- `LegacyPptxProcessor` v1 does not invent OCR or image descriptions. Image-only
  slides can have no trusted text evidence and must produce an actionable
  warning, not model-generated authority.
- PDF page equals PowerPoint slide is valid only if the current converter keeps
  one slide per PDF page. A failed fixture proof blocks Task 1.7 preview
  acceptance and requires a locator crosswalk proposal.
- Source Trust persistence uses separate repository calls. Prevalidation and
  the new atomic activation boundary must eliminate partial supersession;
  trusted consumers remain disabled until Gate 2A verifies it.
- `resolve_index_input` reparses the immutable PPTX to recover normalized
  assets and verify stored evidence. That is deterministic but not free; cache
  only after measurement, keyed by `source_revision_id` and verified hashes.
- The existing schema has no database uniqueness constraint for one `ready`
  revision per scope. The transaction must prove the invariant on every
  supported database or stop for a Sol-0-owned schema change.
- Once Sol-0 accepts and data is backfilled, changing any identity or scope
  formula would orphan durable references. A replacement ruling must be a new
  version, not an in-place reinterpretation.

## Exact acceptance tests

1. A current slide revision maps to the exact compatibility source ID,
   source-PPTX hash, deterministic source revision ID, course authority, and
   ready state.
2. Non-canonical string IDs, missing revisions, non-slide revisions,
   non-current revisions, missing catalog rows/files/PDF metadata, and checksum
   mismatches fail with zero writes.
3. Course, exam, and lecture IDs match the exact NFKC/slug/digest formulas,
   bounds, and StoreKey grammar. Blank subjects and non-positive/out-of-range
   numbers fail closed; long and punctuation-colliding subjects with different
   canonical inputs produce different IDs.
4. On the composed Sol-1/Sol-2 candidate,
   `StoreKey.parse(StoreKey.course(course_id, exam_id).value)` equals the
   original key and preserves both exact IDs.
5. The same PowerPoint parsed twice with `LegacyPptxProcessor` v1 produces the
   same ordered locators, evidence IDs, content hashes, and text.
6. Slide text and speaker notes remain distinct; tables stay single evidence
   units; image-only slides add no generated medical claim text.
7. Task 1.7 resolves both canonical files, hashes, revision state, course
   authority, exact scope IDs, stored evidence units, and deterministic assets
   through `resolve_index_input(source_revision_id)`; slide locator `3`
   previews PDF page `3` on the existing conversion fixture.
8. A Sol-2 consumer test receives only `IndexInputView`, successfully builds
   its StoreKey and provider input, and contains no import/call of ingestion or
   catalog repositories and no parsing of `legacy-study-revision:`.
9. `resolve_index_input` fails closed on nonmatching files, hashes, evidence,
   authority/scope, assets, or unsupported state and performs zero writes.
10. Before/after legacy revision fields and source/PDF file hashes are identical
   after real and dry-run adapter calls.
11. Repeated backfill creates no duplicates and reports the second complete run
   as `already_present`.
12. Atomic replacement activation leaves the predecessor `ready` when any
    insert/evidence/state step fails; on success it commits the predecessor as
    `stale` and the replacement as the only `ready` revision in that exact
    scope. The replacement cannot resolve as indexable before that commit.
13. Concurrent/repeated activation cannot produce two `ready` revisions for
    one course-material scope. Stale/retired views are reconcilable but rejected
    for new provider upload/import.
14. A failure in one batch candidate preserves prior completions, continues to
    later candidates, and returns ordered counts and `failure_ids`.
15. Batch enumeration is globally ordered by numeric legacy revision ID and
    applies `limit` after filtering.
16. Dry-run returns/prints IDs, counts, and warnings only; it writes nothing,
    logs no source text/private paths, and performs no network/provider call.
17. Focused Task 1.6/1.7 tests, all `tests/knowledge`, the composed StoreKey
    contract test, contracts/providers, lint,
    types, safe broad Python, and baseline JavaScript tests pass under the
    existing documented exclusions.

## Exact Sol-0 ruling required

Program Sol-0 must choose one of these outcomes in writing:

1. **APPROVE AMENDED CP-0002 AS WRITTEN** — accept sections 1-10, including the
   bounded scope-ID formulas, StoreKey round-trip gate, knowledge-owned
   `IndexInputView`, opaque consumer boundary, and atomic stale-before-ready
   supersession; preserve the existing `sr_` identity, `course_material`
   authority, and `source_sha256` meaning; record Sol-2's consuming approval;
   declare that these internal additions require no shared JSON/provider schema
   or central migration/wiring change; and authorize Sol-1 to implement Task
   1.6 using the five Task 1.6 files listed above, followed by Task 1.7's owned
   service/view files after Task 1.6 completes; or
2. **REQUIRE A SHARED SCHEMA CHANGE** — keep Task 1.6 blocked and have Sol-0
   own the smallest additive uniqueness/crosswalk schema plus migration,
   compatibility, and rollback semantics needed to guarantee supersession or
   persisted artifact/asset resolution before returning implementation
   authority to Sol-1.

The exact evaluation in this proposal recommends outcome 1: no Sol-0-owned
shared schema/contract file change is needed, but explicit Sol-0 approval and a
Sol-2 re-review changing **BLOCK** to **APPROVED** are mandatory. Silence,
partial approval, or approval omitting the StoreKey, index-view, or
supersession clauses does not unblock Task 1.6.
