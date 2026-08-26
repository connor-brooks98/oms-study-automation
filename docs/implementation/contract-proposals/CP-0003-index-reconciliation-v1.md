# CP-0003: Index reconciliation, lifecycle, and usage contract v1

Status: proposed by Program Sol-0 for exact-commit Sol-2 owner/consumer review.

## Scope

This contract unblocks Task 2.7 without adding a public schema or central
database migration. It consumes the approved CP-0002 v2 indexing view and the
Task 2.6 one-to-many `ProviderDocument` identity. It does not authorize live
provider calls, Task 2.8, feature activation, or dollar estimates.

## Remote identity and read-only observation

Every provider import must round-trip these bounded metadata values in addition
to the five CP-0002 scope fields:

```text
input_key: ProviderDocument.input_key
input_kind: ProviderDocument.input_kind
input_sha256: ProviderDocument.input_sha256
```

`input_key` and `input_kind` use the existing Task 2.6 validators;
`input_sha256` is the exact lowercase SHA-256 already verified before upload.
A missing or invalid value makes the remote document unmatchable; it must be
reported, never coerced to `pptx`.

The provider adapter must expose a pure remote snapshot operation. It may call
the provider list API and map responses into immutable observations, but it
must not call any indexing repository method or mutate local/provider state.
The existing mutating `list_documents()` behavior may remain only as a
compatibility wrapper outside reconciliation; Task 2.7 must not use it.

Reconciliation matches documents only by the exact tuple:

```text
(provider store identity, source_revision_id, input_key)
```

It additionally compares `input_kind` and `input_sha256`. Duplicate remote
tuples, invalid metadata, missing local/remote sides, stale source revisions,
and missing stores are report findings. The default operation is read-only;
repair requires explicit `apply=true`.

## Delete and rebuild lifecycle

The existing `(store_id, source_revision_id)` `IndexJob` is the sole revision
lease and fence. A delete/rebuild operation must first claim that lease; a
second worker or route request cannot mutate the revision while the lease is
valid.

Under the lease, enumerate every local provider document for the exact store
and revision in `input_key` order. Delete each remote document idempotently and
mark that local input `deleted` only after provider success or provider
not-found. A crash leaves remaining inputs non-deleted; a retry resumes them.
The revision job becomes `deleted` only after every input is deleted.

Permanent delete stops in `deleted`. Rebuild performs the same complete delete,
then transitions the same revision job through the existing legal retry path
to `not_indexed`; normal Task 2.6 indexing reconstructs every current input.
Rebuild always targets the caller-supplied store ID. Routes that accept only a
revision ID must resolve exactly one current store generation for its accepted
scope; zero or multiple matches fail closed. No historical generation is
silently selected.

## Health and usage

Task 2.7 health is derived from configuration plus local stores, documents, and
jobs. It must not probe a live provider unless an explicitly authorized live
operation is requested.

Version 1 usage is deliberately limited to the durable
`ProviderDocument.input_byte_count` already recorded per input. Reports may
aggregate these bytes by store/revision/input kind. Token counts are `None`
when the provider supplies none; they are not estimated. Query usage remains
unavailable until a provider response exposes versioned usage metadata and a
separate approved opaque event contract exists. Dollar cost is always `None`
unless a later versioned pricing table is configured. The legacy numeric
`StudyUsageModel` is forbidden.

## Ownership and acceptance

Sol-2 may modify its indexing/provider implementation and focused tests needed
to implement this contract, including repository and worker methods adjacent to
the Task 2.7 files. Central migrations, app wiring, public activation, and
shared schemas remain Sol-0 owned and are not required by version 1.

Acceptance requires committed RED/GREEN evidence proving:

1. remote snapshotting performs zero repository writes and rejects missing or
   invalid per-input metadata;
2. multimodal inputs reconcile independently without PPTX collapse;
3. dry-run reports deterministic findings and mutates nothing;
4. delete/rebuild lease-fences the whole revision, deletes every input,
   resumes after partial failure, and selects no ambiguous store generation;
5. health is offline by default and usage reports durable bytes without token
   or dollar invention; and
6. Task 2.5/2.6 worker recovery and compatibility tests remain green.

Independent Terra specification and quality reviews must approve the exact
Task 2.7 candidate before its handoff is complete.
