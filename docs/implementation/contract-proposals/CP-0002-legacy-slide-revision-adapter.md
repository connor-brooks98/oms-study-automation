# CP-0002: Legacy slide revision adapter contract

Status: proposed and unapplied. This document is not approval, activation, or
authorization to implement Task 1.6.

## Request

- Requesting task: Task 1.6, Sol-1.
- Required decider: Program Sol-0.
- Required consuming review: one consuming Sol under the contract-change
  protocol; Sol-3 is recommended because evidence identity and preview
  resolution are direct Ask/citation inputs.
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
1.7 needs a deterministic reversible adapter identity rather than an implicit
lookup.

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

The prefix makes the identity reversible and namespace-safe. Task 1.7 decodes
only the exact `legacy-study-revision:<positive base-10 integer>` form and uses
that integer with the existing authenticated `ArtifactService` and PDF role.
It must not treat `upload_item_id` as a preview identifier.

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

### 4. Deterministic scope identifiers

Define:

```text
course_key = " ".join(LectureModel.subject.casefold().split())
course_id  = course_key
exam_id    = f"{course_key}:exam:{LectureModel.exam_number}"
lecture_id = f"{exam_id}:lecture:{LectureModel.lecture_number}"
```

Reject a blank normalized `course_key` or non-positive exam/lecture number.
This matches the repository's established case-folded, collapsed-whitespace
subject-key behavior and uses the catalog's natural uniqueness tuple rather
than database row IDs.

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

### 7. Retry, completion, and reporting

Parse and validate the complete candidate before the first Source Trust write.
A revision counts as `already_present` only when the deterministic revision and
its exact deterministic evidence set already exist. A revision record with
missing evidence is resumed/repaired on retry and is not counted as complete.

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

### 8. Ownership and activation

If approved, Task 1.6 may create only:

```text
src/oms_hub/knowledge/backfill.py
tests/knowledge/test_backfill.py
docs/implementation/handoffs/1.6.md
```

It consumes the existing repository, parser, normalization, ID, and artifact
contracts read-only. It does not edit the ingestion repository, central schema,
shared schemas/exporter, startup, route wiring, settings, feature flags,
dependencies, migrations, plan, or manifest. Program Sol-0 continues to own
activation and integration.

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

### Use `derived_sha256` or a combined PPTX/PDF digest

`derived_sha256` misses speaker-note-only changes. A combined digest invents a
new hash contract and cannot be validated as the checksum of one canonical
file. Existing artifact preview already validates the PDF separately.

### Use raw subject text or numeric lecture row IDs for scope

Raw text preserves accidental case/whitespace differences. Numeric lecture IDs
are database-instance identifiers rather than the catalog's natural course,
exam, and lecture key.

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

## Compatibility risks

- One legacy revision per knowledge source does not group replacement decks
  under one logical source. Existing IDs remain deterministic, but a future
  grouping migration must preserve them as aliases/history.
- The established subject normalization can collapse two legacy subjects that
  differ only by case or repeated whitespace. The legacy catalog already uses
  the same normalized-key behavior in several consumers; acceptance must detect
  conflicting scopes rather than merge evidence silently.
- `LegacyPptxProcessor` v1 does not invent OCR or image descriptions. Image-only
  slides can have no trusted text evidence and must produce an actionable
  warning, not model-generated authority.
- PDF page equals PowerPoint slide is valid only if the current converter keeps
  one slide per PDF page. A failed fixture proof blocks Task 1.7 preview
  acceptance and requires a locator crosswalk proposal.
- Source Trust persistence uses separate repository calls. Prevalidation and
  deterministic retry repair the expected interruption window, but trusted
  consumers must remain disabled until Gate 2A verifies complete evidence.
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
3. Course, exam, and lecture IDs match the exact formulas above, including case
   and whitespace normalization; blank/non-positive inputs fail closed.
4. The same PowerPoint parsed twice with `LegacyPptxProcessor` v1 produces the
   same ordered locators, evidence IDs, content hashes, and text.
5. Slide text and speaker notes remain distinct; tables stay single evidence
   units; image-only slides add no generated medical claim text.
6. `legacy-study-revision:42` reverses to artifact revision `42`; Task 1.7
   resolves its PDF through `ArtifactService`, and slide locator `3` previews
   page `3` on the existing conversion fixture.
7. Before/after legacy revision fields and source/PDF file hashes are identical
   after real and dry-run adapter calls.
8. Repeated backfill creates no duplicates and reports the second complete run
   as `already_present`.
9. A failure after revision creation but before complete evidence is repaired
   by retry and is not misreported as already complete.
10. A failure in one batch candidate preserves prior completions, continues to
    later candidates, and returns ordered counts and `failure_ids`.
11. Batch enumeration is globally ordered by numeric legacy revision ID and
    applies `limit` after filtering.
12. Dry-run returns/prints IDs, counts, and warnings only; it writes nothing,
    logs no source text/private paths, and performs no network/provider call.
13. Focused Task 1.6 tests, all `tests/knowledge`, contracts/providers, lint,
    types, safe broad Python, and baseline JavaScript tests pass under the
    existing documented exclusions.

## Exact Sol-0 ruling required

Program Sol-0 must choose one of these outcomes in writing:

1. **APPROVE CP-0002 AS WRITTEN** — accept sections 1-8, explicitly accept the
   one-to-one compatibility source identity and Task 1.7 decoding rule, record
   the required consuming-Sol approval, declare that no shared schema/contract
   change is required, and authorize Sol-1 to implement Task 1.6 from the clean
   recovered head using only its three listed files; or
2. **REJECT THE COMPATIBILITY IDENTITY** — keep Task 1.6 blocked and have Sol-0
   own an additive durable legacy-artifact crosswalk contract/table/field,
   including migration, historical preview, and rollback semantics, before
   returning implementation authority to Sol-1.

Silence, partial approval, or approval that omits the preview crosswalk and
hash/scope formulas does not unblock Task 1.6.
