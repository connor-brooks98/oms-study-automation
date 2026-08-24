# CP-0001: Knowledge domain models and `knowledge-v2.json`

Status: proposed and unapplied. This document is not approval or activation.

## Current contract

`schemas/knowledge-v1.json` is the tag-frozen v1 snapshot. It currently covers
the provider wire contracts `RetrievalScope`, `EvidenceRef`,
`RetrievalRequest`, `RetrievalResult`, and `ProviderHealth`, together with
`AuthorityClass` and `TruthMode`. The exporter and this snapshot are immutable
on the Task 1.2 branch.

## Proposed additive v2 contents

Generate a new `knowledge-v2.json` from the frozen slotted dataclasses in
`oms_hub.knowledge.models` plus the existing provider contracts:

- `SourceRevisionState`: `staged`, `normalizing`, `ready`, `stale`, `failed`,
  `retired`.
- `EvidenceLocatorKind`: `page`, `slide`, `speaker_note`,
  `transcript_segment`, `section`, `figure`, `table`, `article_page`.
- `KnowledgeSource`: `source_document_id`, `authority_class`.
- `SourceRevision`: `source_document_id`, canonical `source_revision_id`,
  `file_sha256`, and `state`.
- `EvidenceLocator`: `kind` and `value`.
- `EvidenceUnit`: its stable identity, source revision, authority and course /
  exam / lecture scope, locator, normalized text, optional image asset,
  content hash, source priority, creation timestamp, and optional retirement
  timestamp.

`EvidenceUnit.supports_medical_claims` is a derived Python property and is not
a wire field. `SourceRevision.revision_id` is a read-only Task 1.3 compatibility
property and is also not a wire field. Runtime validation requires `course_id`
for `course_material` evidence; JSON Schema generation should represent that
conditional requirement at the proposed boundary.

The exporter change, when authorized, should construct a deterministic
`TypeAdapter` union containing the existing v1 wire models and these additive
domain models, set `$id` to `knowledge-v2.json`, and write only the new file.
It must not rewrite `knowledge-v1.json`.

## Impact

Producers are Task 1.2 model construction, Task 1.3 persistence, and Task 1.4
normalization. Consumers are the source-trust repository/backfill, provider
indexing and citation mapping, Ask retrieval/citation validation, board
question evidence packets, and approved literature registration. No existing
v1 producer or consumer needs to change for this proposal.

## Compatibility and migration

The v1 snapshot remains byte-for-byte stable and continues to serve existing
provider clients. v2 is an additive namespace; it does not reinterpret v1
instances and no v1 records require backfill for this task. A future activation
may dual-read v1 and v2 during the migration window, create v2 source/evidence
records through the owning repository, and switch consumers only after their
contract tests pass. Rollback is disabling v2 consumers and retaining the
immutable v1 snapshot; no destructive down-migration is proposed.

## Required review before activation

Sol-0 must review the additive exporter/snapshot boundary and shared-schema
activation. Every consuming owner must review its model and field usage before
activation: Sol-1 (repository/backfill), Sol-2 (provider indexing), Sol-3 (Ask
retrieval), Sol-5 (question evidence), and Sol-8 (journal evidence). This
proposal makes no claim that those reviews or activation have occurred.

## Exact proposed tests

1. Preserve the existing `tests/contracts/test_schema_exports.py` byte and
   reproducibility assertions for v1.
2. Add a v2 snapshot test that runs the exporter twice, compares both outputs
   with the committed v2 snapshot, and rejects workspace paths or private
   data.
3. Assert every enum value above, the exact required/optional field sets,
   conditional course scope, and exclusion of the two derived properties.
4. Round-trip one hand-derived instance of every v2 model through its
   `TypeAdapter` and assert that enum values serialize as their strings.
5. Run the provider contract, knowledge, and safe broad Python lanes before
   activation; require Sol-0 and all consuming-owner reviews listed above.
